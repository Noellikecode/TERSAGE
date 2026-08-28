"""The incident API: open, brief, stream, resolve, approve, log, close.

The stream is the part with a real contract. Server-sent events, ordered by
brief version, and **every frame is in the incident log before it is sent** --
``require_persisted()`` raises otherwise, so the ordering is enforced by the type
rather than by this module remembering to await something first.

Reconnect is by ``Last-Event-ID``, which SSE gives us for free: a tablet that
lost signal reconnects, sends the last version it saw, and gets everything after
it in order. The replay reads the same emissions from the same log, so what a
reconnecting commander sees is what the original stream sent -- not a fresh
render that might differ.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sse_starlette.sse import EventSourceResponse

from firstdue.api.dependencies import (
    Caller,
    require_profile_write,
    require_read,
    require_referral_write,
)
from firstdue.api.routes.health import get_container
from firstdue.container import Container
from firstdue.domain.briefs import BriefEmission
from firstdue.domain.enums import BenchmarkType
from firstdue.errors import NotFoundError, ValidationError
from firstdue.incident.controller import IncidentController
from firstdue.incident.fusion import ThermalFrame
from firstdue.incident.intake import MAX_NARRATIVE_CHARS, IntakeChannel
from firstdue.incident.interceptor import InterceptResult
from firstdue.incident.reconciler import NarrativeChunk
from firstdue.incident.session import IncidentSession, get_session, sessions
from firstdue.observability.context import get_correlation_id
from firstdue.observability.logging import get_logger
from firstdue.observability.metrics import METRICS

logger = get_logger(__name__)

router = APIRouter(tags=["incident"])


# ------------------------------------------------------------------- models


class OpenIncidentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Whatever CAD sent: a display address or an address id. The city adapter
    #: normalises it; this endpoint does not guess.
    address: str = Field(min_length=1, max_length=200)
    cad_ref: str = Field(min_length=1, max_length=120)
    #: CAD's alarm level, and the only one that counts. A level the caller
    #: reported is recorded beside it and applied to nothing -- this number
    #: bounds the incident grant.
    alarm_level: int = Field(default=1, ge=1, le=5)
    dispatched_at: datetime | None = None
    responding_agency_id: str = Field(default="sffd", max_length=120)
    mutual_aid_agreement_id: str | None = Field(default=None, max_length=120)
    #: The 911 transcript or CAD narrative, if one came with the dispatch. It is
    #: read **after** the instant brief has been persisted and is in this
    #: response, so a slow or unavailable model costs the amendment and never
    #: the brief.
    intake_narrative: str | None = Field(default=None, max_length=MAX_NARRATIVE_CHARS)
    intake_channel: IntakeChannel = IntakeChannel.CALL_911


class IntakeRequest(BaseModel):
    """A narrative arriving after the dispatch: a callback, a CAD update."""

    model_config = ConfigDict(extra="forbid")

    narrative: str = Field(min_length=1, max_length=MAX_NARRATIVE_CHARS)
    channel: IntakeChannel = IntakeChannel.CALL_911
    #: What to cite the reported values against. Defaults to the incident id.
    source_ref: str | None = Field(default=None, max_length=200)


class ReportedLine(BaseModel):
    """One thing the narrative said, as the API renders it."""

    model_config = ConfigDict(extra="forbid")

    intake_key: str
    reported_value: str
    #: Where in the narrative it was read. A value nobody can trace back to the
    #: transcript is a claim, so the offsets travel with it.
    start_offset: int
    end_offset: int
    quoted_text: str


class HandoffLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_ref: str
    rule_ids: list[str]
    intake_keys: list[str]
    started: bool


class WithheldLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_ref: str
    rule_ids: list[str]
    missing_scopes: list[str]


class IntakeResponse(BaseModel):
    """What the intake was read as, and where it was routed.

    Deliberately explicit rather than a dump of the internal result: the
    difference between what was *reported* and what the fleet then *did* is the
    thing a console has to render distinctly, so the API states both.
    """

    model_config = ConfigDict(extra="forbid")

    incident_id: str
    channel: str
    source_ref: str
    #: False when the narrative was not read at all -- no model, screen down,
    #: response refused. Never an error: the brief already landed.
    accepted: bool
    rejection_reason: str | None = None
    model_ref: str
    screen: str
    screen_findings: list[str]
    #: True when the screen removed something in the narrative that tried to
    #: instruct the model.
    screened: bool
    reported: list[ReportedLine]
    unknowns: list[str]
    fired_rule_ids: list[str]
    #: Rules that fired and matched no catalogued incident agent. A stated gap.
    unmatched_rule_ids: list[str]
    woken: list[HandoffLine]
    #: Agents a rule matched that this incident's grant cannot cover.
    withheld: list[WithheldLine]
    #: The version of the marked amendment carrying the reported lines, if the
    #: narrative reported anything at all.
    brief_version: int | None = None


class OpenIncidentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: str
    address_id: str
    #: The street address a dispatcher would read aloud, from the city adapter.
    #: Reference data, not a fact: the municipality publishes it, nothing
    #: derives or merges it, and it never disagrees with itself.
    #:
    #: Sent alongside `address_id` rather than replacing it. The id is what
    #: every event, grant and log entry is keyed by, so a console that showed
    #: only the prose address could not be matched against the record it
    #: produced.
    address_display: str = ""
    profile_snapshot_id: str
    grant_id: str
    cold_start: bool
    dispatched_at: datetime
    elapsed_seconds: float
    #: The instant brief, already persisted. Version 1, no model call.
    brief: dict[str, Any]
    instant_brief_ms: float
    event_id: str
    #: Present when a narrative came with the dispatch. Everything in it landed
    #: *after* ``brief`` above, which is version 1 and model-free regardless.
    intake: IntakeResponse | None = None


class ResolutionRequest(BaseModel):
    """What an IC saw during the 360."""

    model_config = ConfigDict(extra="forbid")

    conflict_id: str = Field(min_length=1, max_length=120)
    observed_value: str = Field(min_length=1, max_length=200)
    resolved_by: str = Field(min_length=1, max_length=120)
    note: str = Field(default="", max_length=500)


class ThermalFrameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    face: str = Field(min_length=1, max_length=20)
    region_temps_c: list[float] = Field(min_length=1, max_length=64)
    coverage: float = Field(default=1.0, ge=0.0, le=1.0)
    source: str = Field(default="recorded", max_length=60)


class FrameAnalysisRequest(BaseModel):
    """Raw imagery for the fusion agent to read itself.

    Note what is **not** here: a face. The wall is resolved from the footprint
    the slow loop measured, using the camera bearing. A caller that could name
    the face could name it wrong, and a temperature on the wrong wall reads to
    an officer as coverage of a side nobody photographed.
    """

    model_config = ConfigDict(extra="forbid")

    #: Base64 frame. JPEG, PNG, or WebP.
    image_base64: str = Field(min_length=1, max_length=12_000_000)
    mime_type: str = Field(default="image/jpeg", max_length=40)
    #: Direction the lens points, degrees clockwise from north.
    camera_bearing_deg: float = Field(ge=0.0, lt=360.0)
    source: str = Field(default="recorded", max_length=60)


class ResourceRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind_id: str = Field(min_length=1, max_length=60)
    detail: str = Field(default="", max_length=300)
    #: Present when a human has already approved this exact request.
    approval_id: str | None = Field(default=None, max_length=120)


class CloseIncidentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    closed_by: str = Field(min_length=1, max_length=120)


def _controller(container: Container) -> IncidentController:
    return get_session(container).controller


def _intake_response(result: InterceptResult) -> IntakeResponse:
    """Render one intercept. Reported and routed, stated separately."""
    reading = result.reading
    started = set(result.woken_agent_ids)
    return IntakeResponse(
        incident_id=result.incident_id,
        channel=str(reading.channel),
        source_ref=reading.source_ref,
        accepted=reading.accepted,
        rejection_reason=reading.rejection_reason,
        model_ref=reading.model_ref,
        screen=reading.screen,
        screen_findings=list(reading.screen_findings),
        screened=reading.screened,
        reported=[
            ReportedLine(
                intake_key=item.intake_key,
                reported_value=item.raw_value,
                start_offset=item.span.start_offset,
                end_offset=item.span.end_offset,
                quoted_text=item.span.quoted_text,
            )
            for item in reading.items
        ],
        unknowns=list(reading.unknowns),
        fired_rule_ids=list(result.plan.fired_rule_ids),
        unmatched_rule_ids=list(result.plan.unmatched_rule_ids),
        woken=[
            HandoffLine(
                agent_ref=handoff.agent_ref,
                rule_ids=list(handoff.rule_ids),
                intake_keys=list(handoff.intake_keys),
                started=handoff.agent_id in started,
            )
            for handoff in result.plan.handoffs
        ],
        withheld=[
            WithheldLine(
                agent_ref=entry.agent_ref,
                rule_ids=list(entry.rule_ids),
                missing_scopes=list(entry.missing_scopes),
            )
            for entry in result.plan.withheld
        ],
        brief_version=result.emission.version if result.emission else None,
    )


async def _read_intake(
    session: IncidentSession,
    incident_id: str,
    *,
    narrative: str,
    channel: IntakeChannel,
    source_ref: str,
    container: Container,
) -> IntakeResponse:
    """Run one intake through the runtime and render it.

    Called only after the instant brief is persisted. A model that is slow,
    refusing, or unreachable produces an ``accepted=False`` response here and
    changes nothing about the brief that already landed.
    """
    result = await session.run_intake(
        incident_id,
        narrative=narrative,
        channel=channel,
        source_ref=source_ref,
        correlation_id=get_correlation_id() or container.ids.new_id("corr"),
    )
    return _intake_response(result)


# --------------------------------------------------------------------- open


@router.post(
    "/incidents",
    response_model=OpenIncidentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Open an incident from a CAD dispatch",
)
async def open_incident(
    request: OpenIncidentRequest,
    container: Annotated[Container, Depends(get_container)],
    caller: Annotated[Caller, Depends(require_profile_write)],
) -> OpenIncidentResponse:
    """Open an incident and return the instant brief.

    The brief in this response is already in the incident log -- it is persisted
    before it is returned, exactly as it is before it is streamed.

    A narrative sent with the dispatch is read **after** that, never before.
    ``brief`` is version 1 and model-free whatever the intake does, and the
    budget measured against ``instant_brief_budget_ms`` covers stage one alone,
    because stage one is the only thing with nothing to wait for.
    """
    session = get_session(container)
    opened = await session.controller.open(
        address=request.address,
        cad_ref=request.cad_ref,
        alarm_level=request.alarm_level,
        dispatched_at=request.dispatched_at,
        responding_agency_id=request.responding_agency_id,
        mutual_aid_agreement_id=request.mutual_aid_agreement_id,
        # The caller's id, when it sent one. Without this the incident mints a
        # fresh correlation id and the request that opened it can no longer be
        # joined to the audit trail or the trace -- which is the one question
        # anyone asks afterwards.
        correlation_id=get_correlation_id(),
    )

    started = time.perf_counter()
    emission = await session.emit_instant(opened)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    METRICS.record_time_to_first_line(elapsed_ms)

    if elapsed_ms > container.settings.instant_brief_budget_ms:
        # A defect, not a slow day. Logged as such.
        logger.error(
            "instant_brief_budget_exceeded",
            extra={
                "incident_id": opened.incident.incident_id,
                "elapsed_ms": round(elapsed_ms, 3),
                "budget_ms": container.settings.instant_brief_budget_ms,
            },
        )

    intake: IntakeResponse | None = None
    if request.intake_narrative:
        intake = await _read_intake(
            session,
            opened.incident.incident_id,
            narrative=request.intake_narrative,
            channel=request.intake_channel,
            source_ref=f"intake/{request.cad_ref}",
            container=container,
        )

    resolved = container.city.get_address(opened.incident.address_id)
    return OpenIncidentResponse(
        incident_id=opened.incident.incident_id,
        address_id=opened.incident.address_id,
        # Empty rather than a placeholder when the city cannot place the id: a
        # console that printed the id as if it were a street address would be
        # inventing a location, and the banner falls back to the id itself.
        address_display=resolved.display if resolved is not None else "",
        profile_snapshot_id=opened.snapshot_id,
        grant_id=opened.grant.grant_id,
        cold_start=opened.cold_start,
        dispatched_at=opened.incident.dispatched_at,
        elapsed_seconds=session.controller.elapsed_seconds(opened.incident),
        brief=emission.model_dump(mode="json"),
        instant_brief_ms=round(elapsed_ms, 3),
        event_id=opened.event_id,
        intake=intake,
    )


@router.post(
    "/incidents/{incident_id}/intake",
    response_model=IntakeResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Read a 911 or CAD narrative and route the incident",
)
async def read_intake(
    incident_id: str,
    request: IntakeRequest,
    container: Annotated[Container, Depends(get_container)],
    caller: Annotated[Caller, Depends(require_profile_write)],
) -> IntakeResponse:
    """Read a narrative that arrived after the dispatch.

    A callback, a second caller, a dispatcher updating the CAD comment. It goes
    through exactly the same path the dispatch narrative does: screened, read
    into a closed key set with every value bound to a span in the transcript,
    rendered as a **marked amendment** whose lines can never read as confirmed,
    and routed to the other incident agents by their declared capabilities.

    A write, not a read: it amends the brief and appends to the incident log.

    202 rather than 201, and deliberately so. Nothing here creates a resource of
    its own -- the narrative was accepted and what it produced is an amendment
    to a document that already exists.
    """
    session = get_session(container)
    return await _read_intake(
        session,
        incident_id,
        narrative=request.narrative,
        channel=request.channel,
        source_ref=request.source_ref or f"intake/{incident_id}",
        container=container,
    )


# -------------------------------------------------------------------- brief


@router.get(
    "/incidents/{incident_id}/brief",
    summary="The latest brief version",
)
async def latest_brief(
    incident_id: str,
    container: Annotated[Container, Depends(get_container)],
    caller: Annotated[Caller, Depends(require_read)],
) -> dict[str, Any]:
    session = get_session(container)
    emission = session.latest(incident_id)
    if emission is None:
        raise NotFoundError("no brief for this incident", details={"incident_id": incident_id})
    return emission.model_dump(mode="json")


@router.post(
    "/incidents/{incident_id}/brief/enrich",
    summary="Produce the enriched brief version",
)
async def enrich_brief(
    incident_id: str,
    container: Annotated[Container, Depends(get_container)],
    caller: Annotated[Caller, Depends(require_profile_write)],
) -> dict[str, Any]:
    """Add the model-composed stage.

    A model that is unavailable or returns something the contract rejects leaves
    the deterministic brief exactly as it was, and the emission says the
    narrative is unavailable.
    """
    session = get_session(container)
    started = time.perf_counter()
    emission = await session.run_enrichment(
        incident_id, correlation_id=get_correlation_id() or container.ids.new_id("corr")
    )
    METRICS.record_enriched_latency((time.perf_counter() - started) * 1000.0)
    return emission.model_dump(mode="json")


def _frame(emission: BriefEmission) -> dict[str, str]:
    """One SSE frame. The version is the event id, which is what resume uses."""
    return {
        "event": "brief",
        "id": str(emission.version),
        "data": json.dumps(emission.model_dump(mode="json"), sort_keys=True),
    }


def _narrative_frame(chunk: NarrativeChunk) -> dict[str, str]:
    """One provisional prose frame.

    It carries **no event id**. Event ids are what ``Last-Event-ID`` resumes
    from, and resuming from a chunk would replay half a sentence as though it
    were a brief version. A reconnecting tablet resumes at the last emission it
    saw and hears the narrative again from the top, which is correct: the prose
    is a rendering of an emission, not a record of its own.
    """
    return {
        "event": "narrative",
        "data": json.dumps(chunk.model_dump(mode="json"), sort_keys=True),
    }


@router.get(
    "/incidents/{incident_id}/brief/stream-enriched",
    summary="Enrich the brief, streaming the prose as it composes",
)
async def stream_enriched(
    incident_id: str,
    container: Annotated[Container, Depends(get_container)],
    caller: Annotated[Caller, Depends(require_read)],
) -> EventSourceResponse:
    """Compose the enriched brief and stream the prose token by token.

    Two frame types, and the difference between them is the whole contract:

    * ``narrative`` frames are **provisional**. They carry prose being written,
      no facts, no version id, and nothing the incident log stores. A consumer
      must be able to discard them.
    * the final ``brief`` frame is the persisted emission. It has a version, a
      content hash, and a place in the record, and it arrives only after
      ``require_persisted()`` has passed.

    If the composition is refused or times out, the stream still ends with a
    ``brief`` frame -- one whose narrative is absent and marked unavailable.
    There is no path where provisional prose is left standing on a screen with
    nothing authoritative behind it.
    """
    session = get_session(container)
    # Resolved before the response begins: an error raised inside the generator
    # has already sent 200 OK and cannot become an error envelope.
    prepared = await session.require_enrichable(incident_id)

    async def frames() -> AsyncIterator[dict[str, str]]:
        async for item in session.emit_enriched_streaming(incident_id, prepared):
            if isinstance(item, BriefEmission):
                # The same gate every other frame passes.
                yield _frame(item.require_persisted())
            else:
                yield _narrative_frame(item)

    return EventSourceResponse(frames())


@router.get(
    "/incidents/{incident_id}/stream",
    summary="Stream brief versions over SSE",
)
async def stream_brief(
    incident_id: str,
    container: Annotated[Container, Depends(get_container)],
    caller: Annotated[Caller, Depends(require_read)],
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    after_version: Annotated[int | None, Query(ge=0)] = None,
) -> EventSourceResponse:
    """Stream every brief version, in order, resumable.

    A reconnecting tablet sends ``Last-Event-ID`` and receives everything after
    it. The frames come from the emissions the log already holds, so a resumed
    stream shows what the original one sent rather than a fresh render.

    Every frame calls ``require_persisted()`` before it is yielded. An emission
    that somehow reached this point unpersisted raises rather than being shown.
    """
    session = get_session(container)
    resume_from = _resume_point(last_event_id, after_version)

    async def frames() -> AsyncIterator[dict[str, str]]:
        for emission in session.emissions_after(incident_id, resume_from):
            # The gate. Nothing reaches a commander that is not in the record.
            yield _frame(emission.require_persisted())

    return EventSourceResponse(frames())


@router.get(
    "/incidents/{incident_id}/log/stream",
    summary="Stream incident log entries over SSE",
)
async def stream_incident_log(
    incident_id: str,
    container: Annotated[Container, Depends(get_container)],
    caller: Annotated[Caller, Depends(require_read)],
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    after_sequence: Annotated[int | None, Query(ge=0)] = None,
) -> EventSourceResponse:
    """Every entry the incident has recorded, in order, resumable.

    **The same record, pushed rather than polled.** ``GET /log`` already returns
    this; it is a document, fetched when somebody asks. A commander watching the
    fleet work wants the entries as they land, and the log is the right thing to
    stream because it is monotonic and gapless -- ``sequence`` is a resume point
    that cannot skip and cannot repeat, which is exactly what ``Last-Event-ID``
    needs and what a version number on a brief already gives the other stream.

    ``agent_versions`` travels with each frame, which the document endpoint does
    not send. It is how a console attributes an entry to the agent that produced
    it: without it every card would have to be inferred from the entry type, and
    two agents that write the same type would be indistinguishable.

    Carries what the log carries and nothing else -- ids, keys, counts and
    reasons. The intake entry names attributes and outcomes, never the caller's
    words; the focus entry carries references, never values. A stream that
    widened either would be a second, looser copy of the log.
    """
    resume_from = _resume_point(last_event_id, after_sequence)
    log = await container.incident_log.get_log(incident_id)

    async def frames() -> AsyncIterator[dict[str, str]]:
        for entry in log.entries:
            if entry.sequence <= resume_from:
                continue
            yield {
                "event": "entry",
                "id": str(entry.sequence),
                "data": json.dumps(
                    {
                        "sequence": entry.sequence,
                        "entry_type": str(entry.entry_type),
                        "occurred_at": entry.occurred_at.isoformat(),
                        "agent_versions": entry.agent_versions,
                        "content_hash": entry.content_hash,
                        "content": entry.content,
                    }
                ),
            }

    return EventSourceResponse(frames())


def _resume_point(last_event_id: str | None, after_version: int | None) -> int:
    """Where to resume from. The header wins; the query is for testing."""
    if last_event_id:
        try:
            return int(last_event_id)
        except ValueError:
            # A client sending a malformed resume point gets the whole stream
            # rather than an error: showing the brief again is always safe.
            return 0
    return after_version or 0


# --------------------------------------------------------------- the 360


@router.post(
    "/incidents/{incident_id}/resolutions",
    status_code=status.HTTP_201_CREATED,
    summary="Record an IC resolution during the 360",
)
async def resolve_conflict(
    incident_id: str,
    request: ResolutionRequest,
    container: Annotated[Container, Depends(get_container)],
    caller: Annotated[Caller, Depends(require_profile_write)],
) -> dict[str, Any]:
    """Settle a disagreement by looking at the building.

    An IC walking the 360 is a human observation, so it can close a conflict
    that no newer document could. It writes a live-observation fact, resolves
    the conflict, bumps ``profile_version``, and produces a marked amendment.
    """
    session = get_session(container)
    result = await session.resolve(
        incident_id,
        conflict_id=request.conflict_id,
        observed_value=request.observed_value,
        resolved_by=request.resolved_by,
        note=request.note,
    )
    return result


@router.post(
    "/incidents/{incident_id}/thermal",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Register a thermal frame to a face",
)
async def register_thermal(
    incident_id: str,
    request: ThermalFrameRequest,
    container: Annotated[Container, Depends(get_container)],
    # A write, not a read: registering a frame amends the brief and appends to
    # the incident log. Reading geometry is not permission to change it.
    caller: Annotated[Caller, Depends(require_profile_write)],
) -> dict[str, Any]:
    """Register recorded or synthetic thermal footage to one face.

    Never presented as a live flight. Faces with no current frame stay
    UNSCANNED, and every reading carries the caveat that thermal imaging
    measures surface temperature and cannot see through walls.
    """
    from firstdue.domain.enums import FaceLabel

    try:
        face = FaceLabel(request.face.upper())
    except ValueError as exc:
        raise ValidationError("unknown face label", details={"face": request.face[:20]}) from exc

    frame = ThermalFrame(
        frame_id=container.ids.new_id("frame"),
        incident_id=incident_id,
        face=face,
        observed_at=container.clock.now(),
        region_temps_c=tuple(request.region_temps_c),
        coverage=request.coverage,
        source=request.source,
    )
    session = get_session(container)
    return await session.run_thermal_registration(
        incident_id, frame, correlation_id=get_correlation_id() or container.ids.new_id("corr")
    )


@router.post(
    "/incidents/{incident_id}/frames",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Analyse a frame: imagery in, thermal and massing model out",
)
async def analyze_frame(
    incident_id: str,
    request: FrameAnalysisRequest,
    container: Annotated[Container, Depends(get_container)],
    caller: Annotated[Caller, Depends(require_profile_write)],
) -> dict[str, Any]:
    """The autonomous imagery path.

    The agent resolves the face from the slow loop's footprint, reads the frame
    with Gemini, turns the observations into a registered thermal frame, and
    amends the brief. It refuses rather than guesses: no pre-incident geometry,
    or a bearing that resolves to no single wall, comes back with a stated
    reason and changes nothing.
    """
    import base64
    import binascii

    try:
        image = base64.b64decode(request.image_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValidationError("image_base64 is not valid base64") from exc

    session = get_session(container)
    return await session.run_frame_analysis(
        incident_id,
        image=image,
        mime_type=request.mime_type,
        camera_bearing_deg=request.camera_bearing_deg,
        source=request.source,
        correlation_id=get_correlation_id() or container.ids.new_id("corr"),
    )


@router.post(
    "/incidents/{incident_id}/drone-sweep",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Fly one face of a synthetic drone sweep",
)
async def drone_sweep(
    incident_id: str,
    container: Annotated[Container, Depends(get_container)],
    # The same write scope as any other frame: a sweep amends the brief and
    # appends to the log. Watching a heat map build is not a read.
    caller: Annotated[Caller, Depends(require_profile_write)],
) -> dict[str, Any]:
    """Advance the sweep by one wall.

    Called repeatedly, it builds thermal coverage face by face at whatever
    cadence the caller chooses, through the same **Sensor Fusion** path a real
    aircraft would use. The frames are generated and labelled ``synthetic-drone``
    everywhere they land; against a live vision model the sweep refuses rather
    than producing a real reading of an imaginary building.

    Returns a value in every case. ``flown`` false with a ``reason`` is a
    refusal, and ``complete`` true means every face the footprint has is
    already covered.
    """
    session = get_session(container)
    return await session.run_drone_sweep_step(
        incident_id,
        correlation_id=get_correlation_id() or container.ids.new_id("corr"),
    )


@router.post(
    "/incidents/{incident_id}/resources",
    summary="Request a resource -- notification or commitment",
)
async def request_resource(
    incident_id: str,
    request: ResourceRequestBody,
    container: Annotated[Container, Depends(get_container)],
    caller: Annotated[Caller, Depends(require_referral_write)],
) -> dict[str, Any]:
    """Ask for a resource. The gateway decides whether a human must approve.

    Notifications go out autonomously. Anything that commits another agency's
    resources comes back ``REQUIRE_APPROVAL`` with a staged, prefilled card --
    and that distinction is made by policy, not by this endpoint.
    """
    session = get_session(container)
    outcome = await session.run_resource_request(
        incident_id,
        correlation_id=get_correlation_id() or container.ids.new_id("corr"),
        kind_id=request.kind_id,
        detail=request.detail,
        approval_id=request.approval_id,
    )
    return outcome.model_dump(mode="json")


@router.post(
    "/incidents/{incident_id}/approvals/{approval_id}",
    summary="Approve a staged action",
)
async def approve_action(
    incident_id: str,
    approval_id: str,
    container: Annotated[Container, Depends(get_container)],
    caller: Annotated[Caller, Depends(require_referral_write)],
) -> dict[str, Any]:
    """The human tap. Records who approved it, and then executes."""
    session = get_session(container)
    return await session.approve(incident_id, approval_id, decided_by=caller.subject)


# ----------------------------------------------------------------- the log


@router.get(
    "/incidents/{incident_id}/log",
    summary="The append-only incident log",
)
async def incident_log(
    incident_id: str,
    container: Annotated[Container, Depends(get_container)],
    caller: Annotated[Caller, Depends(require_read)],
) -> dict[str, Any]:
    """Everything recorded during the incident, in order, with content hashes."""
    log = await container.incident_log.get_log(incident_id)
    return {
        "incident_id": incident_id,
        "sealed_at": log.sealed_at.isoformat() if log.sealed_at else None,
        "entries": [
            {
                "sequence": entry.sequence,
                "entry_type": str(entry.entry_type),
                "occurred_at": entry.occurred_at.isoformat(),
                "profile_snapshot_id": entry.profile_snapshot_id,
                "content_hash": entry.content_hash,
                "written_to_rms_at": (
                    entry.written_to_rms_at.isoformat() if entry.written_to_rms_at else None
                ),
                "content": entry.content,
            }
            for entry in log.entries
        ],
        "unflushed": len(log.unflushed),
    }


@router.post(
    "/incidents/{incident_id}/benchmarks/{benchmark_type}",
    status_code=status.HTTP_201_CREATED,
    summary="Timestamp an operational benchmark",
)
async def record_benchmark(
    incident_id: str,
    benchmark_type: BenchmarkType,
    container: Annotated[Container, Depends(get_container)],
    caller: Annotated[Caller, Depends(require_profile_write)],
) -> dict[str, Any]:
    """Record that something happened, when it happened. Clerical, not tactical."""
    mark = await _controller(container).record_benchmark(
        incident_id, benchmark_type, recorded_by=caller.subject
    )
    return mark.model_dump(mode="json")


# ---------------------------------------------------------------- closing


@router.post(
    "/incidents/{incident_id}/close",
    summary="Close the incident",
)
async def close_incident(
    incident_id: str,
    request: CloseIncidentRequest,
    container: Annotated[Container, Depends(get_container)],
    caller: Annotated[Caller, Depends(require_profile_write)],
) -> dict[str, Any]:
    """Close: revoke the grant, seal the log, draft the report.

    Both the revocation and the seal happen. Authority ends when the incident
    does, and a log that could still be appended to afterwards is a log nobody
    can rely on.
    """
    # Before forgetting the session, undo the thermal this incident painted
    # onto the stored model. Coverage belongs to the incident that flew it;
    # leaving it behind shows a heat map from a fire that is out.
    session = get_session(container)
    cleared = await session.clear_painted_thermal(incident_id)
    result = await _controller(container).close(incident_id, closed_by=request.closed_by)
    sessions(container).forget(incident_id)
    return {**result.model_dump(mode="json"), "thermal_cleared": cleared}
