"""One running incident, and everything the API needs to drive it.

The session holds the emissions produced so far so the SSE stream can replay
them in order to a reconnecting tablet. It is not the record -- the incident log
is -- but it is the ordered, already-persisted view the stream reads from, which
is what makes a resumed stream show what the original one sent rather than a
fresh render that might differ.

Late data never delays earlier output. Each stage produces a new emission and
appends it; nothing here waits on a source before emitting what it already has.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any, Final

from firstdue.agents.fleet import FleetRunner
from firstdue.container import Container
from firstdue.domain.briefs import BriefEmission
from firstdue.domain.conflicts import ConflictResolution, ConflictStatus
from firstdue.domain.enums import Classification, PolicyAction, SourceType
from firstdue.domain.facts import StructuralFact, natural_fact_id
from firstdue.domain.identity import IncidentGrant
from firstdue.domain.keys import Keys
from firstdue.domain.profiles import ProfileEvent, ProfileEventType, ProfileSnapshot
from firstdue.domain.values import TextValue
from firstdue.errors import NotFoundError, StaleVersionError, ValidationError
from firstdue.extraction.coercion import coerce_value
from firstdue.incident.controller import IncidentController, OpenIncidentResult
from firstdue.incident.fusion import SensorFusion, ThermalFrame
from firstdue.incident.reconciler import NarrativeChunk, Reconciler
from firstdue.incident.recorder import IncidentRecorder
from firstdue.incident.resources import ResourceAgent, ResourceOutcome
from firstdue.incident.timer import truss_time_window
from firstdue.observability.logging import get_logger
from firstdue.observability.metrics import METRICS
from firstdue.ports.runtime import AgentInput, AgentOutcome, Grant
from firstdue.services.grants import GrantService

logger = get_logger(__name__)

IC_AGENT: Final[str] = "incident-controller"


def _one(payload: AgentInput, key: str) -> str:
    """Read one identifier from an agent input.

    ``AgentInput.ids`` allows a tuple, because some handoffs carry several fact
    ids. The incident handlers each work on exactly one incident, and reading a
    tuple as though it were a string is the kind of mistake that only shows up
    on a fireground.
    """
    value = payload.ids[key]
    if isinstance(value, tuple):
        if len(value) != 1:
            raise ValidationError(
                "this handler works on exactly one identifier",
                details={"key": key, "count": str(len(value))},
            )
        return value[0]
    return value


class IncidentSession:
    """The incident loop's live state, per process."""

    def __init__(self, container: Container) -> None:
        self._container = container
        self.recorder = IncidentRecorder(
            incident_log=container.incident_log,
            write_actions=container.write_actions,
            audit=container.audit,
            clock=container.clock,
            ids=container.ids,
            rms=container.write_targets.get("department-rms"),
        )
        self.controller = IncidentController(
            incidents=container.incidents,
            profiles=container.profiles,
            snapshots=container.snapshots,
            grants=GrantService(
                grants=container.grants,
                audit=container.audit,
                clock=container.clock,
                ids=container.ids,
            ),
            recorder=self.recorder,
            city=container.city,
            clock=container.clock,
            ids=container.ids,
            bus=container.bus,
        )
        self.reconciler = Reconciler(
            clock=container.clock, ids=container.ids, model=container.model
        )
        self.fusion = SensorFusion(vision=container.vision, ids=container.ids)
        self.resources = ResourceAgent(
            policy=container.policy,
            approvals=container.approvals,
            write_actions=container.write_actions,
            target=container.write_targets["agency-notifications"],
            audit=container.audit,
            clock=container.clock,
            ids=container.ids,
        )
        #: Persisted emissions, in order, per incident. What the stream replays.
        self._emissions: dict[str, list[BriefEmission]] = {}
        self._grants: dict[str, IncidentGrant] = {}
        # Work staged for a runtime handler, keyed by the correlation id of the
        # run that will pick it up. An AgentInput carries identifiers, never
        # payloads, so a thermal frame cannot travel inside one.
        self._pending_frames: dict[str, ThermalFrame] = {}
        #: Raw imagery staged for the fusion agent's own extraction path.
        self._pending_imagery: dict[str, dict[str, Any]] = {}
        self._pending_requests: dict[str, dict[str, Any]] = {}
        # Where a handler leaves its typed result for the caller. An
        # AgentOutcome carries identifiers; the route still wants the object.
        self._last_thermal: dict[str, dict[str, Any]] = {}
        self._last_resource: dict[str, ResourceOutcome] = {}

        # The incident loop runs through the same runtime the slow loop does.
        # Its agents are *not* given standing grants -- incident authority is
        # bound to one incident, one address, and one responding agency, and it
        # is revoked at close -- so every run here passes the incident grant
        # explicitly and the runner refuses without one.
        self.fleet = FleetRunner(
            runtime=container.runtime,
            registry=container.registry,
            grants=GrantService(
                grants=container.grants,
                clock=container.clock,
                ids=container.ids,
                audit=container.audit,
            ),
            runs=container.runs,
            clock=container.clock,
            ids=container.ids,
            only_agent=container.settings.firstdue_agent,
        )
        self.fleet.register_all(
            {
                "brief-reconciler": self._reconciler_handler,
                "sensor-fusion": self._fusion_handler,
                "agency-notifier": self._notifier_handler,
                "incident-recorder": self._recorder_handler,
            }
        )

    # ------------------------------------------------------- runtime handlers
    #
    # Each is registered against the descriptor it implements. They are thin on
    # purpose: the work already lives in the agent objects, and what the
    # runtime adds is the grant check, the deadline, the terminal state, and
    # the durable run record naming the pinned version that produced it.

    async def _reconciler_handler(self, payload: AgentInput, _grant: Grant) -> AgentOutcome:
        emission = await self.emit_enriched(_one(payload, "incident_id"))
        return AgentOutcome(emitted_event_ids=(emission.emission_id,))

    async def _fusion_handler(self, payload: AgentInput, _grant: Grant) -> AgentOutcome:
        """Registering a thermal frame amends the brief and appends to the log.

        Two entry points converge here: a pre-extracted frame from a ground
        station, and raw imagery the agent extracts itself. The second is the
        autonomous path, and it produces the first.
        """
        incident_id = _one(payload, "incident_id")

        staged = self._pending_imagery.pop(payload.correlation_id, None)
        if staged is not None:
            result = await self.analyze_imagery(incident_id, **staged)
            self._last_thermal[incident_id] = result
            return AgentOutcome()

        frame = self._pending_frames.pop(payload.correlation_id, None)
        if frame is None:  # pragma: no cover - the caller always stages one
            return AgentOutcome()
        result = await self.register_thermal(incident_id, frame)
        self._last_thermal[incident_id] = result
        return AgentOutcome()

    async def _notifier_handler(self, payload: AgentInput, _grant: Grant) -> AgentOutcome:
        """Telling an agency is autonomous; cutting their gas needs a chief."""
        request = self._pending_requests.pop(payload.correlation_id, None)
        if request is None:  # pragma: no cover - the caller always stages one
            return AgentOutcome()
        incident_id = _one(payload, "incident_id")
        outcome = await self.request_resource(incident_id, **request)
        self._last_resource[incident_id] = outcome
        return AgentOutcome(
            write_action_ids=tuple(ref for ref in (outcome.external_ref,) if ref),
            policy_decision_ids=(outcome.decision_id,),
        )

    async def _recorder_handler(self, payload: AgentInput, _grant: Grant) -> AgentOutcome:
        """Drain buffered log entries into the records system."""
        result = await self.recorder.flush_to_rms(incident_id=_one(payload, "incident_id"))
        # The count is what the recorder reports; the entry ids stay in the log
        # where they belong. A run record naming them would duplicate the log.
        return AgentOutcome(
            write_action_ids=(f"rms-flush:{result.flushed}",) if result.flushed else ()
        )

    # ------------------------------------------------------------ emissions

    async def emit_instant(self, opened: OpenIncidentResult) -> BriefEmission:
        """Build and persist the instant brief. No model call on this path."""
        truss = None
        truss_fact = opened.snapshot.facts.get(Keys.LIGHTWEIGHT_TRUSS)
        if truss_fact is not None and truss_fact.value.is_known:
            truss = truss_time_window(
                dispatched_at=opened.incident.dispatched_at,
                now=self._container.clock.now(),
                fact_id=truss_fact.fact_id,
            )

        emission = self.reconciler.instant(
            opened.snapshot,
            incident_id=opened.incident.incident_id,
            collapse_zone_m=(
                opened.snapshot.geometry.collapse_zone_radius_m
                if opened.snapshot.geometry
                else None
            ),
            truss_window=truss,
        )
        self._grants[opened.incident.incident_id] = opened.grant
        return await self._persist(emission)

    async def run_enrichment(self, incident_id: str, *, correlation_id: str) -> BriefEmission:
        """Enrich through the runtime, under the incident's own grant.

        The work is unchanged. What the runtime adds is the grant check before
        it, the descriptor's latency target around it, and a durable run record
        naming the pinned version that produced the emission -- which is what a
        NIOSH investigation asks for and what a direct call never wrote.
        """
        grant = await self._require_grant(incident_id)
        await self.fleet.run(
            "brief-reconciler",
            correlation_id=correlation_id,
            ids={"incident_id": incident_id},
            grant=grant,
        )
        emission = self.latest(incident_id)
        if emission is None:  # pragma: no cover - enrichment always emits one
            raise NotFoundError("enrichment produced no emission", details={"id": incident_id})
        return emission

    async def run_thermal_registration(
        self, incident_id: str, frame: ThermalFrame, *, correlation_id: str
    ) -> dict[str, Any]:
        """Register a thermal frame through the runtime."""
        grant = await self._require_grant(incident_id)
        self._pending_frames[correlation_id] = frame
        try:
            await self.fleet.run(
                "sensor-fusion",
                correlation_id=correlation_id,
                ids={"incident_id": incident_id},
                grant=grant,
            )
        finally:
            self._pending_frames.pop(correlation_id, None)
        return self._last_thermal.pop(incident_id, {})

    async def run_frame_analysis(
        self,
        incident_id: str,
        *,
        image: bytes,
        mime_type: str,
        camera_bearing_deg: float,
        source: str,
        correlation_id: str,
    ) -> dict[str, Any]:
        """Run the imagery path through the runtime, like any other agent work."""
        grant = await self._require_grant(incident_id)
        self._pending_imagery[correlation_id] = {
            "image": image,
            "mime_type": mime_type,
            "camera_bearing_deg": camera_bearing_deg,
            "source": source,
        }
        try:
            await self.fleet.run(
                "sensor-fusion",
                correlation_id=correlation_id,
                ids={"incident_id": incident_id},
                grant=grant,
            )
        finally:
            self._pending_imagery.pop(correlation_id, None)
        return self._last_thermal.pop(incident_id, {})

    async def run_resource_request(
        self,
        incident_id: str,
        *,
        correlation_id: str,
        kind_id: str,
        detail: str,
        approval_id: str | None,
    ) -> ResourceOutcome:
        """Ask for a resource through the runtime."""
        grant = await self._require_grant(incident_id)
        self._pending_requests[correlation_id] = {
            "kind_id": kind_id,
            "detail": detail,
            "approval_id": approval_id,
        }
        try:
            await self.fleet.run(
                "agency-notifier",
                correlation_id=correlation_id,
                ids={"incident_id": incident_id},
                grant=grant,
            )
        finally:
            self._pending_requests.pop(correlation_id, None)
        outcome = self._last_resource.pop(incident_id, None)
        if outcome is None:  # pragma: no cover - the handler always sets one
            raise NotFoundError("resource request produced no outcome", details={"id": incident_id})
        return outcome

    async def emit_enriched(self, incident_id: str) -> BriefEmission:
        previous = self.latest(incident_id)
        if previous is None:
            raise NotFoundError("no brief to enrich", details={"incident_id": incident_id})
        incident = await self._require_incident(incident_id)
        snapshot = await self._container.snapshots.get(incident.profile_snapshot_id)
        if snapshot is None:
            raise NotFoundError("profile snapshot is missing", details={"incident_id": incident_id})
        emission = await self.reconciler.enriched(previous, snapshot)
        return await self._persist(emission)

    async def require_enrichable(self, incident_id: str) -> tuple[BriefEmission, ProfileSnapshot]:
        """Resolve everything enrichment needs, or raise before anything streams.

        Called by the route *before* the response begins. An error raised
        inside a streaming generator has already had ``200 OK`` and the SSE
        content type written to the socket, so it cannot become an error
        envelope -- the connection just breaks. Prerequisites are resolved here
        so an unknown incident is an ordinary 404.
        """
        previous = self.latest(incident_id)
        if previous is None:
            raise NotFoundError("no brief to enrich", details={"incident_id": incident_id})
        incident = await self._require_incident(incident_id)
        snapshot = await self._container.snapshots.get(incident.profile_snapshot_id)
        if snapshot is None:
            raise NotFoundError("profile snapshot is missing", details={"incident_id": incident_id})
        return previous, snapshot

    async def emit_enriched_streaming(
        self, incident_id: str, prepared: tuple[BriefEmission, ProfileSnapshot] | None = None
    ) -> AsyncIterator[NarrativeChunk | BriefEmission]:
        """Enrich, streaming the prose as it composes.

        The chunks pass straight through -- they are provisional prose and the
        log does not store them. The emission at the end goes through the same
        ``_persist`` every other emission does, so what the record holds is
        unchanged by the fact that the prose was watched being written.
        """
        previous, snapshot = prepared or await self.require_enrichable(incident_id)

        async for item in self.reconciler.enriched_streaming(previous, snapshot):
            if isinstance(item, BriefEmission):
                yield await self._persist(item)
            else:
                yield item

    async def emit_amendment(self, incident_id: str, **kwargs: Any) -> BriefEmission:
        previous = self.latest(incident_id)
        if previous is None:
            raise NotFoundError("no brief to amend", details={"incident_id": incident_id})
        return await self._persist(self.reconciler.amendment(previous, **kwargs))

    async def _persist(self, emission: BriefEmission) -> BriefEmission:
        """Write to the log, then hold the persisted copy. Never the reverse."""
        stored = await self.recorder.record_emission(emission)
        self._emissions.setdefault(stored.incident_id, []).append(stored)
        return stored

    def latest(self, incident_id: str) -> BriefEmission | None:
        versions = self._emissions.get(incident_id)
        return versions[-1] if versions else None

    def emissions_after(self, incident_id: str, version: int) -> Sequence[BriefEmission]:
        """Everything after a version, in order. What a reconnect replays."""
        return [e for e in self._emissions.get(incident_id, []) if e.version > version]

    def forget(self, incident_id: str) -> None:
        self._emissions.pop(incident_id, None)
        self._grants.pop(incident_id, None)

    # -------------------------------------------------------------- the 360

    async def resolve(
        self,
        incident_id: str,
        *,
        conflict_id: str,
        observed_value: str,
        resolved_by: str,
        note: str,
    ) -> dict[str, Any]:
        """An IC settled a disagreement by looking at the building.

        Writes a live-observation fact, closes the conflict, bumps
        ``profile_version``, records it in the log, and emits a marked
        amendment. All five, because the resolution is worth nothing if the
        brief still shows the disagreement.
        """
        incident = await self._require_incident(incident_id)
        profile = await self._container.profiles.get(incident.address_id)
        if profile is None:
            raise NotFoundError(
                "no profile for this incident", details={"incident_id": incident_id}
            )

        conflict = next((c for c in profile.conflicts if c.conflict_id == conflict_id), None)
        if conflict is None:
            raise NotFoundError("conflict not found", details={"conflict_id": conflict_id})
        if conflict.status is not ConflictStatus.OPEN:
            raise NotFoundError(
                "conflict is already resolved", details={"conflict_id": conflict_id}
            )

        now = self._container.clock.now()
        value = coerce_value(conflict.canonical_key, observed_value) or TextValue(
            text=observed_value[:2000]
        )
        fact = StructuralFact(
            fact_id=natural_fact_id(
                address_id=profile.address_id,
                canonical_key=conflict.canonical_key,
                source_ref=f"ic-resolution/{incident_id}/{conflict_id}",
                observed_at=now,
                rendered_value=value.render(),
            ),
            address_id=profile.address_id,
            canonical_key=conflict.canonical_key,
            value=value,
            # A live observation outranks every filed record, by tier.
            source_type=SourceType.IC_RESOLUTION,
            source_ref=f"ic-resolution/{incident_id}/{conflict_id}",
            source_snapshot_id=f"ic:{incident_id}:{conflict_id}",
            observed_at=now,
            ingested_at=now,
            confidence=0.97,
            classification=Classification.PUBLIC,
            produced_by_agent=IC_AGENT,
        )
        await self._container.facts.append(fact)

        resolution = ConflictResolution(
            resolved_at=now,
            resolving_record_id=incident_id,
            resolving_fact_id=fact.fact_id,
            resolved_by=resolved_by,
            note=note or f"Settled on scene during the 360 at incident {incident_id}.",
        )
        await self._container.conflicts.resolve(conflict_id, resolution)

        updated = profile.with_fact(
            fact,
            event=ProfileEvent(
                event_id=f"pevt_ic_{conflict_id.removeprefix('conflict_')}",
                sequence=profile.next_sequence,
                occurred_at=now,
                type=ProfileEventType.CONFLICT_RESOLVED,
                actor=resolved_by,
                summary=f"IC resolution: {conflict.canonical_key} observed as {value.render()}",
                canonical_keys=(conflict.canonical_key,),
                fact_ids=(fact.fact_id,),
                conflict_id=conflict_id,
            ),
        )
        updated = updated.model_copy(
            update={
                "conflicts": tuple(
                    c.resolve(resolution) if c.conflict_id == conflict_id else c
                    for c in updated.conflicts
                )
            }
        )
        try:
            saved = await self._container.profiles.save(
                updated, expected_version=profile.profile_version
            )
        except StaleVersionError:
            logger.info("ic_resolution_contended", extra={"incident_id": incident_id})
            saved = updated

        await self.recorder.record_resolution(
            incident_id,
            conflict_id=conflict_id,
            resolved_by=resolved_by,
            note=note,
            fact_id=fact.fact_id,
        )
        await self.recorder.record_observed_fact(
            incident_id,
            fact_id=fact.fact_id,
            canonical_key=conflict.canonical_key,
            source=str(SourceType.IC_RESOLUTION),
        )

        emission = await self.emit_amendment(
            incident_id,
            resolutions=(
                f"{conflict.canonical_key} observed as {value.render()} by {resolved_by}. "
                f"Both original records are retained.",
            ),
        )
        return {
            "conflict_id": conflict_id,
            "fact_id": fact.fact_id,
            "profile_version": saved.profile_version,
            "brief_version": emission.version,
            "resolved_by": resolved_by,
        }

    async def register_thermal(self, incident_id: str, frame: ThermalFrame) -> dict[str, Any]:
        """Register a frame and amend the brief with current coverage."""
        self.fusion.register(frame)
        now = self._container.clock.now()
        coverage = self.fusion.coverage(incident_id, now=now)
        voids = self.fusion.voids(incident_id, now=now)
        emission = await self.emit_amendment(incident_id, thermal=coverage, voids=voids)
        return {
            "frame_id": frame.frame_id,
            "face": str(frame.face),
            "brief_version": emission.version,
            "unscanned_faces": [str(c.face) for c in coverage if not c.scanned],
            "voids": len(voids),
        }

    async def analyze_imagery(
        self,
        incident_id: str,
        *,
        image: bytes,
        mime_type: str,
        camera_bearing_deg: float,
        source: str,
    ) -> dict[str, Any]:
        """Imagery in, amended brief and massing model out.

        The frame is resolved to a face using the footprint the **Geometry
        Watcher** measured during the slow loop, read off the profile snapshot
        this incident opened against. A refusal -- no profile, or a corner shot
        that resolves to no single wall -- is returned as a stated reason and
        amends nothing, because a frame on no wall is not coverage of any wall.
        """
        incident = await self._require_incident(incident_id)
        snapshot = await self._container.snapshots.get(incident.profile_snapshot_id)
        spec = snapshot.geometry if snapshot is not None else None
        now = self._container.clock.now()

        analysis = await self.fusion.analyze_frame(
            incident_id=incident_id,
            image=image,
            mime_type=mime_type,
            camera_bearing_deg=camera_bearing_deg,
            observed_at=now,
            spec=spec,
            source=source,
        )
        if analysis.rejected is not None:
            return {
                "registered": False,
                "reason": analysis.rejected.reason,
                "cold_start": analysis.rejected.cold_start,
                "model_ref": analysis.model_ref,
            }

        coverage = self.fusion.coverage(incident_id, now=now)
        voids = self.fusion.voids(incident_id, now=now)
        emission = await self.emit_amendment(incident_id, thermal=coverage, voids=voids)
        return {
            "registered": analysis.registered,
            "frame_id": analysis.frame.frame_id if analysis.frame else None,
            "face": str(analysis.frame.face) if analysis.frame else None,
            "observed_storeys": analysis.observed_storeys,
            "obstructions": [str(o) for o in analysis.obstructions],
            "unknowns": list(analysis.unknowns),
            "brief_version": emission.version,
            "unscanned_faces": [str(c.face) for c in coverage if not c.scanned],
            "voids": len(voids),
            "model_ref": analysis.model_ref,
        }

    # ----------------------------------------------------------- resources

    async def request_resource(
        self, incident_id: str, *, kind_id: str, detail: str, approval_id: str | None
    ) -> ResourceOutcome:
        grant = await self._require_grant(incident_id)
        incident = await self._require_incident(incident_id)
        outcome = await self.resources.request(
            kind_id,
            grant=grant,
            incident_id=incident_id,
            address_id=incident.address_id,
            detail=detail,
            approval_id=approval_id,
        )
        # Delivery rate, counted at the point of truth: a request the gateway
        # allowed either reached its target or did not. A commitment awaiting
        # approval is neither -- it was never sent, so it is not a delivery
        # failure and is not counted as one.
        if outcome.action is not PolicyAction.REQUIRE_APPROVAL:
            METRICS.record_notification(delivered=bool(outcome.sent and outcome.external_ref))

        if outcome.sent and outcome.external_ref:
            await self.recorder.record_notification(
                incident_id,
                target=kind_id,
                external_ref=outcome.external_ref,
                autonomous=approval_id is None,
            )
        return outcome

    async def approve(
        self, incident_id: str, approval_id: str, *, decided_by: str
    ) -> dict[str, Any]:
        """Grant a staged approval and execute the request it was holding."""
        from firstdue.domain.work import ApprovalStatus

        approval = await self._container.approvals.get(approval_id)
        if approval is None:
            raise NotFoundError("approval not found", details={"approval_id": approval_id})
        now = self._container.clock.now()
        granted = await self._container.approvals.save(
            approval.model_copy(
                update={
                    "status": ApprovalStatus.GRANTED,
                    "decided_at": now,
                    "decided_by": decided_by,
                }
            )
        )
        await self.recorder.record_approval(
            incident_id,
            approval_id=approval_id,
            decided_by=decided_by,
            threshold=str(approval.threshold),
        )

        kind_id = approval_id.rsplit("_", 1)[-1]
        outcome = await self.request_resource(
            incident_id, kind_id=kind_id, detail="", approval_id=approval_id
        )
        return {
            "approval_id": granted.approval_id,
            "decided_by": decided_by,
            "executed": outcome.sent,
            "external_ref": outcome.external_ref,
            "action": str(outcome.action),
        }

    # ------------------------------------------------------------ internals

    async def _require_incident(self, incident_id: str) -> Any:
        incident = await self._container.incidents.get(incident_id)
        if incident is None:
            raise NotFoundError("incident not found", details={"incident_id": incident_id})
        return incident

    async def _require_grant(self, incident_id: str) -> IncidentGrant:
        grant = self._grants.get(incident_id)
        if grant is not None:
            return grant
        incident = await self._require_incident(incident_id)
        stored = await self._container.grants.get_incident_grant(incident.grant_id)
        if stored is None:
            raise NotFoundError("incident grant not found", details={"incident_id": incident_id})
        self._grants[incident_id] = stored
        return stored


class SessionRegistry:
    """One session per process. Held on the app, not in a module global."""

    def __init__(self) -> None:
        self._session: IncidentSession | None = None

    def for_container(self, container: Container) -> IncidentSession:
        if self._session is None:
            self._session = IncidentSession(container)
        return self._session

    def forget(self, incident_id: str) -> None:
        if self._session is not None:
            self._session.forget(incident_id)


_REGISTRIES: dict[int, SessionRegistry] = {}


def sessions(container: Container) -> SessionRegistry:
    """The registry for this container. Keyed by identity, not by a global."""
    return _REGISTRIES.setdefault(id(container), SessionRegistry())


def get_session(container: Container) -> IncidentSession:
    return sessions(container).for_container(container)
