"""The Incident Controller: opening, running, and closing an incident.

Opening does five things, in this order, and the order is the design:

1. **Mint an incident grant** -- bound to this incident, this address, this
   jurisdiction, this responding agency, with a TTL. Nothing reads anything
   before there is authority to.
2. **Read exactly one profile snapshot** -- the entire interface between the
   slow loop and the incident loop. One read, not a query per attribute.
3. **Record the snapshot id on the incident** -- so the brief replays against
   exactly the state it was built from.
4. **Emit ``incident.opened``** -- identifiers only, as every envelope is.
5. **Start the elapsed clock** -- from the CAD dispatch time, not from when this
   process happened to get the message.

If there is no profile -- new construction, outside the district -- the incident
opens anyway, marked ``cold_start``, and the brief says the structural
attributes are unknown. That is the honest failure mode, and it is not an error.

Closing revokes the grant and seals the log. Both, always: authority ends when
the incident does, and a log that could still be appended to after close is a
log nobody can rely on.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from firstdue.domain.enums import BenchmarkType, Department
from firstdue.domain.events import EventEnvelope, Topic
from firstdue.domain.identity import IncidentGrant
from firstdue.domain.incidents import Benchmark, Incident, IncidentStatus
from firstdue.domain.logentries import AppendOnlyLog
from firstdue.domain.profiles import ProfileSnapshot
from firstdue.errors import NotFoundError, ValidationError
from firstdue.incident.recorder import IncidentRecorder, NerisDraft
from firstdue.observability.logging import get_logger
from firstdue.observability.tracing import incident_span
from firstdue.ports.bus import EventBus
from firstdue.ports.city import CityAdapter
from firstdue.ports.clock import Clock, IdGenerator
from firstdue.ports.repositories import (
    IncidentRepository,
    ProfileRepository,
    SnapshotRepository,
)
from firstdue.services.grants import GrantService

logger = get_logger(__name__)

AGENT_ID: Final[str] = "incident-controller"
#: An incident grant outlives a working fire and not much more.
DEFAULT_TTL: Final[timedelta] = timedelta(hours=12)


class OpenIncidentResult(BaseModel):
    """Everything opening an incident produced."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    incident: Incident
    grant: IncidentGrant
    snapshot: ProfileSnapshot
    #: True when nothing was on file. The brief says so on screen.
    cold_start: bool = False
    event_id: str = Field(min_length=1, max_length=120)

    @property
    def snapshot_id(self) -> str:
        return self.snapshot.snapshot_id


class CloseIncidentResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    incident: Incident
    grant_revoked_at: datetime | None = None
    log_sealed_at: datetime | None = None
    log_entries: int = Field(default=0, ge=0)
    neris_draft: NerisDraft | None = None
    #: Entries the records system had not taken yet. Buffered, never dropped.
    rms_still_buffered: int = Field(default=0, ge=0)


class IncidentController:
    """Opens, runs, and closes one incident at a time."""

    def __init__(
        self,
        *,
        incidents: IncidentRepository,
        profiles: ProfileRepository,
        snapshots: SnapshotRepository,
        grants: GrantService,
        recorder: IncidentRecorder,
        city: CityAdapter,
        clock: Clock,
        ids: IdGenerator,
        bus: EventBus | None = None,
        agent_version: str = "1.0.0",
    ) -> None:
        self._incidents = incidents
        self._profiles = profiles
        self._snapshots = snapshots
        self._grants = grants
        self._recorder = recorder
        self._city = city
        self._clock = clock
        self._ids = ids
        self._bus = bus
        self._agent_version = agent_version

    # --------------------------------------------------------------- open

    async def open(
        self,
        *,
        address: str,
        cad_ref: str,
        alarm_level: int = 1,
        dispatched_at: datetime | None = None,
        responding_agency_id: str = "sffd",
        mutual_aid_agreement_id: str | None = None,
        incident_id: str | None = None,
        correlation_id: str | None = None,
    ) -> OpenIncidentResult:
        """Open an incident from a CAD dispatch.

        Args:
            address: whatever CAD sent -- a display address or an address id.
                The city adapter normalises it; this method does not guess.
            cad_ref: the dispatch reference, recorded for reconciliation.
            dispatched_at: when CAD dispatched. The elapsed clock runs from
                here, not from when this process received the message, because
                the difference is queue time the commander already spent.
        """
        with incident_span(
            incident_id=incident_id or "pending",
            address_id="pending",
            cad_ref=cad_ref,
            alarm_level=alarm_level,
        ) as active:
            result = await self._open(
                address=address,
                cad_ref=cad_ref,
                alarm_level=alarm_level,
                dispatched_at=dispatched_at,
                responding_agency_id=responding_agency_id,
                mutual_aid_agreement_id=mutual_aid_agreement_id,
                incident_id=incident_id,
                correlation_id=correlation_id,
            )
            active.set_many(
                {
                    "incident.id": result.incident.incident_id,
                    "incident.address_id": result.incident.address_id,
                    "incident.cold_start": result.cold_start,
                    "incident.snapshot_id": result.snapshot.snapshot_id,
                    "incident.profile_version": result.snapshot.profile_version,
                }
            )
            return result

    async def _open(
        self,
        *,
        address: str,
        cad_ref: str,
        alarm_level: int,
        dispatched_at: datetime | None,
        responding_agency_id: str,
        mutual_aid_agreement_id: str | None,
        incident_id: str | None,
        correlation_id: str | None,
    ) -> OpenIncidentResult:
        normalized = self._city.normalize_address(address)
        if normalized is None:
            raise NotFoundError(
                "CAD address did not resolve to a known structure",
                details={"address": address[:120]},
            )

        now = self._clock.now()
        dispatch_time = dispatched_at or now
        resolved_incident_id = incident_id or self._ids.new_id("incident")
        correlation = correlation_id or self._ids.new_id("corr")

        existing = await self._incidents.get(resolved_incident_id)
        if existing is not None:
            raise ValidationError(
                "incident already open", details={"incident_id": resolved_incident_id}
            )

        # 1. Authority first. Nothing is read before there is a grant to read under.
        grant = await self._grants.mint_incident_grant(
            agent_id=AGENT_ID,
            incident_id=resolved_incident_id,
            address_id=normalized.address_id,
            jurisdiction_id=normalized.jurisdiction_id,
            responding_agency_id=responding_agency_id,
            alarm_level=alarm_level,
            department=Department.FIRE,
            mutual_aid_agreement_id=mutual_aid_agreement_id,
            ttl=DEFAULT_TTL,
            correlation_id=correlation,
        )

        # 2. One snapshot. The entire interface between the two loops.
        snapshot, cold_start = await self._snapshot_for(normalized.address_id, now=now)

        # 3. The snapshot id lands on the incident, for replay.
        incident = await self._incidents.create(
            Incident(
                incident_id=resolved_incident_id,
                address_id=normalized.address_id,
                district_id=normalized.district_id,
                cad_ref=cad_ref,
                alarm_level=alarm_level,
                jurisdiction_id=normalized.jurisdiction_id,
                responding_agency_id=responding_agency_id,
                grant_id=grant.grant_id,
                profile_snapshot_id=snapshot.snapshot_id,
                cold_start=cold_start,
                status=IncidentStatus.OPEN,
                dispatched_at=dispatch_time,
                opened_at=now,
            )
        )

        # 5. The elapsed clock starts at dispatch. Recorded as a benchmark so it
        # is in the log rather than only in memory.
        await self._record_benchmark(
            incident, BenchmarkType.DISPATCH, at=dispatch_time, recorded_by="cad"
        )

        # 4. Identifiers only, as every envelope is.
        event_id = await self._emit_opened(incident, correlation_id=correlation)

        logger.info(
            "incident_opened",
            extra={
                "incident_id": incident.incident_id,
                "address_id": incident.address_id,
                "cold_start": cold_start,
                "snapshot_id": snapshot.snapshot_id,
            },
        )
        return OpenIncidentResult(
            incident=incident,
            grant=grant,
            snapshot=snapshot,
            cold_start=cold_start,
            event_id=event_id,
        )

    async def _snapshot_for(
        self, address_id: str, *, now: datetime
    ) -> tuple[ProfileSnapshot, bool]:
        """Read one snapshot, or build the cold-start one.

        A cold start is not an error. It is a building nobody has filed anything
        about, and the brief must say that rather than implying the structure is
        unremarkable.
        """
        profile = await self._profiles.get(address_id)
        if profile is None:
            known = self._city.get_address(address_id)
            empty = ProfileSnapshot(
                address_id=address_id,
                district_id=known.district_id if known else "unknown",
                profile_version=0,
                snapshot_id=f"snap_cold_{address_id}",
                read_at=now,
            )
            stored_cold = await self._snapshots.put(empty)
            return stored_cold, True

        snapshot = await self._snapshots.put(profile.snapshot(read_at=now))
        return snapshot, snapshot.is_cold_start

    async def _emit_opened(self, incident: Incident, *, correlation_id: str) -> str:
        event_id = self._ids.new_id("evt")
        if self._bus is None:
            return event_id
        await self._bus.publish(
            EventEnvelope(
                event_id=event_id,
                topic=Topic.INCIDENT_OPENED,
                occurred_at=self._clock.now(),
                producer=AGENT_ID,
                producer_version=self._agent_version,
                correlation_id=correlation_id,
                ids={
                    "incident_id": incident.incident_id,
                    "address_id": incident.address_id,
                    "district_id": incident.district_id,
                    "profile_snapshot_id": incident.profile_snapshot_id,
                    "grant_id": incident.grant_id,
                },
                idempotency_key=self._ids.idempotency_key("incident.opened", incident.incident_id),
            )
        )
        return event_id

    # ---------------------------------------------------------- during

    def elapsed_seconds(self, incident: Incident) -> float:
        """Seconds since CAD dispatch. Monotonic within the incident."""
        return incident.elapsed_seconds(self._clock.now())

    async def record_benchmark(
        self, incident_id: str, benchmark_type: BenchmarkType, *, recorded_by: str
    ) -> Benchmark:
        """Timestamp something that happened. Clerical, never tactical."""
        incident = await self._require(incident_id)
        return await self._record_benchmark(
            incident, benchmark_type, at=self._clock.now(), recorded_by=recorded_by
        )

    async def _record_benchmark(
        self,
        incident: Incident,
        benchmark_type: BenchmarkType,
        *,
        at: datetime,
        recorded_by: str,
    ) -> Benchmark:
        mark = Benchmark(
            benchmark_id=self._ids.new_id("bench"),
            incident_id=incident.incident_id,
            type=benchmark_type,
            occurred_at=at,
            recorded_by=recorded_by,
        )
        await self._incidents.save(incident.with_benchmark(mark))
        await self._recorder.record_benchmark(mark)
        return mark

    # --------------------------------------------------------------- close

    async def close(
        self, incident_id: str, *, closed_by: str, correlation_id: str | None = None
    ) -> CloseIncidentResult:
        """Close the incident: revoke the grant, seal the log, draft the report.

        Both revocation and sealing happen, and neither depends on the other
        succeeding. Authority ends when the incident does.
        """
        incident = await self._require(incident_id)
        now = self._clock.now()

        closed = await self._incidents.save(incident.close(at=now))
        await self._record_benchmark(
            closed, BenchmarkType.INCIDENT_CLOSED, at=now, recorded_by=closed_by
        )

        revoked = await self._grants.revoke_for_incident(
            incident_id, incident.grant_id, correlation_id=correlation_id
        )

        # Try to drain the records system before sealing, but never block on it.
        flush = await self._recorder.flush_to_rms(incident_id=incident_id)
        draft = await self._recorder.neris_draft(closed)
        sealed: AppendOnlyLog = await self._recorder.seal(incident_id, at=now)

        logger.info(
            "incident_closed",
            extra={
                "incident_id": incident_id,
                "entries": len(sealed.entries),
                "rms_buffered": flush.still_buffered,
            },
        )
        return CloseIncidentResult(
            incident=closed,
            grant_revoked_at=revoked.revoked_at,
            log_sealed_at=sealed.sealed_at,
            log_entries=len(sealed.entries),
            neris_draft=draft,
            rms_still_buffered=flush.still_buffered,
        )

    async def _require(self, incident_id: str) -> Incident:
        incident = await self._incidents.get(incident_id)
        if incident is None:
            raise NotFoundError("incident not found", details={"incident_id": incident_id})
        return incident
