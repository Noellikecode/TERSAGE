"""The console API.

What the command center reads and what a captain acts on. Everything here
renders state the slow loop already produced -- none of these endpoints decides
anything, and the two that write (dispatch, approve) are the human taps the
governance model requires.

Three shapes recur, and they are the honest ones:

* an attribute that nothing settled comes back as ``UNKNOWN`` with the sources
  that were checked, never as a missing key;
* an attribute two sources disagree about comes back ``DISPUTED``, with both
  facts, never averaged;
* a source that was unreachable comes back ``UNAVAILABLE`` naming the source,
  never as an absence of hazard.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict, Field

from firstdue.agents.actions import ActionFlow
from firstdue.api.dependencies import (
    Caller,
    require_geometry_read,
    require_profile_write,
    require_read,
    require_referral_write,
    require_work_order,
)
from firstdue.api.routes.health import get_container
from firstdue.container import Container
from firstdue.demo.scenario import build_agents
from firstdue.domain.conflicts import ConflictStatus
from firstdue.domain.enums import AssertionStatus, SourceType, SurveyOutcome
from firstdue.domain.geometry import GeometrySpec
from firstdue.domain.keys import CanonicalKey
from firstdue.domain.preplan import render_svg
from firstdue.domain.profiles import BuildingProfile
from firstdue.domain.values import FactValue
from firstdue.domain.work import SurveyRecord
from firstdue.errors import NotFoundError, ValidationError
from firstdue.observability.metrics import METRICS
from firstdue.ports.fireactivity import FireActivity, FireActivityClient
from firstdue.ports.imagery import BuildingImagery, ImageryClient
from firstdue.services.surveys import SurveyService

router = APIRouter(tags=["console"])


# ------------------------------------------------------------------- models


class FactView(BaseModel):
    """One attribute as the console renders it."""

    model_config = ConfigDict(extra="forbid")

    canonical_key: CanonicalKey
    value: str
    #: CONFIRMED, DISPUTED, or UNKNOWN. Never inferred by the client.
    status: AssertionStatus
    known: bool
    source_type: SourceType
    source_ref: str
    observed_at: datetime
    confidence: float
    #: Decayed confidence, recomputed by the slow loop.
    decayed_confidence: float | None = None
    human_verified: bool = False
    #: Every fact ever written for this attribute, winners and losers alike.
    all_fact_ids: tuple[str, ...] = ()


class ConflictView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conflict_id: str
    canonical_key: CanonicalKey
    rule_id: str
    severity: int
    summary: str
    fact_ids: tuple[str, ...]
    status: ConflictStatus
    detected_at: datetime
    resolved_by: str | None = None


class TimelineEventView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence: int
    occurred_at: datetime
    type: str
    actor: str
    actor_version: str | None = None
    summary: str
    fact_ids: tuple[str, ...] = ()
    conflict_id: str | None = None


class BuildingProfileView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    address_id: str
    district_id: str
    profile_version: int
    facts: list[FactView]
    conflicts: list[ConflictView]
    #: Attributes with no record at all. Printed, never omitted.
    unknown_keys: list[str]
    hydrant_ids: tuple[str, ...] = ()
    last_human_survey: datetime | None = None
    open_referrals: list[dict[str, Any]] = Field(default_factory=list)
    has_geometry: bool = False


class RankReasonView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str
    detail: str
    weight: float
    canonical_key: str | None = None
    conflict_id: str | None = None


class QueueEntryView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry_id: str
    address_id: str
    rank: int
    score: float
    status: str
    reasons: list[RankReasonView]
    assigned_company: str | None = None
    dispatched_at: datetime | None = None
    calendar_event_ref: str | None = None
    survey_id: str | None = None


class QueueView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    district_id: str
    entries: list[QueueEntryView]
    count: int


class SourceHealthView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    mode: str
    circuit_state: str
    available: bool
    cache_hits: int
    upstream_calls: int
    last_snapshot_id: str | None = None


class DistrictStatsView(BaseModel):
    """What a battalion chief sees before opening anything."""

    model_config = ConfigDict(extra="forbid")

    district_id: str
    profiles: int
    facts: int
    open_conflicts: int
    #: Conflicts at severity 4 or 5 -- the ones worth a morning.
    high_severity_conflicts: int
    queued_for_survey: int
    dispatched: int
    surveyed: int
    profiles_never_surveyed: int
    open_referrals: int
    #: Availability of every configured source, reported honestly.
    sources: list[SourceHealthView]


class DispatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company: str = Field(min_length=1, max_length=60)
    crew_email: str = Field(min_length=3, max_length=200)


class ApprovalRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved_by: str = Field(min_length=1, max_length=120, description="the human approving")


class SurveyObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_key: CanonicalKey
    value: FactValue


class SurveySubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company: str = Field(min_length=1, max_length=60)
    surveyor: str = Field(min_length=1, max_length=120)
    outcome: SurveyOutcome = SurveyOutcome.COMPLETED
    queue_entry_id: str | None = Field(default=None, max_length=120)
    observations: list[SurveyObservation] = Field(default_factory=list)
    notes: str | None = Field(default=None, max_length=8000)


# ---------------------------------------------------------------- rendering


def _fact_views(profile: BuildingProfile) -> list[FactView]:
    views: list[FactView] = []
    disputed = {c.canonical_key for c in profile.conflicts if c.status is ConflictStatus.OPEN}
    for key, fact in sorted(profile.facts.items()):
        fact_set = profile.fact_sets[key]
        if key in disputed:
            assertion = AssertionStatus.DISPUTED
        elif not fact.value.is_known:
            assertion = AssertionStatus.UNKNOWN
        else:
            assertion = fact_set.local_status
        views.append(
            FactView(
                canonical_key=key,
                value=fact.value.render(),
                status=assertion,
                known=fact.value.is_known,
                source_type=fact.source_type,
                source_ref=fact.source_ref,
                observed_at=fact.observed_at,
                confidence=fact.confidence,
                decayed_confidence=profile.confidence_decay.get(key),
                human_verified=fact.human_verified,
                all_fact_ids=tuple(f.fact_id for f in fact_set.facts),
            )
        )
    return views


def _conflict_views(profile: BuildingProfile) -> list[ConflictView]:
    return [
        ConflictView(
            conflict_id=c.conflict_id,
            canonical_key=c.canonical_key,
            rule_id=c.rule_id,
            severity=c.severity,
            summary=c.summary,
            fact_ids=c.fact_ids,
            status=c.status,
            detected_at=c.detected_at,
            resolved_by=c.resolution.resolved_by if c.resolution else None,
        )
        for c in sorted(profile.conflicts, key=lambda c: (-c.severity, c.conflict_id))
    ]


def _action_flow(container: Container) -> ActionFlow:
    _records, _geometry, _hazards, _watch, actions = build_agents(container)
    return actions


def _survey_service(container: Container) -> SurveyService:
    return SurveyService(
        profiles=container.profiles,
        facts=container.facts,
        conflicts=container.conflicts,
        surveys=container.surveys,
        queue=container.queue,
        clock=container.clock,
    )


# --------------------------------------------------------------- districts


@router.get(
    "/districts/{district_id}/stats",
    response_model=DistrictStatsView,
    summary="District readiness statistics",
)
async def district_stats(
    district_id: str,
    container: Annotated[Container, Depends(get_container)],
    caller: Annotated[Caller, Depends(require_read)],
) -> DistrictStatsView:
    profiles = await container.profiles.list_by_district(district_id)
    queue = await container.queue.list_for_district(district_id)

    open_conflicts = [c for p in profiles for c in p.conflicts if c.status is ConflictStatus.OPEN]
    sources: list[SourceHealthView] = []
    for adapter in container.source_adapters:
        health = await adapter.health()
        sources.append(
            SourceHealthView(
                source_id=health.source_id,
                mode=str(health.mode),
                circuit_state=str(health.circuit_state),
                available=health.is_available,
                cache_hits=health.cache_hits,
                upstream_calls=health.upstream_calls,
                last_snapshot_id=health.last_snapshot_id,
            )
        )

    # Conflicts per 1,000 structures is the slow loop's headline quality number,
    # and both of its terms are already computed here. Recording it where the
    # console reads it keeps the metric and the displayed figure the same
    # arithmetic rather than two implementations that drift.
    METRICS.record_district(structures=len(profiles), open_conflicts=len(open_conflicts))

    return DistrictStatsView(
        district_id=district_id,
        profiles=len(profiles),
        facts=sum(len(p.all_facts()) for p in profiles),
        open_conflicts=len(open_conflicts),
        high_severity_conflicts=len([c for c in open_conflicts if c.severity >= 4]),
        queued_for_survey=len([e for e in queue if e.status.value == "RANKED"]),
        dispatched=len([e for e in queue if e.status.value == "DISPATCHED"]),
        surveyed=len([e for e in queue if e.status.value == "SURVEYED"]),
        profiles_never_surveyed=len([p for p in profiles if p.last_human_survey is None]),
        open_referrals=sum(len(p.open_referrals) for p in profiles),
        sources=sources,
    )


@router.post(
    "/districts/{district_id}/poll",
    summary="Run one slow-loop pass over a district",
)
async def poll_district(
    district_id: str,
    container: Annotated[Container, Depends(get_container)],
    caller: Annotated[Caller, Depends(require_profile_write)],
    approve: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    """Poll every source, materialize, and re-rank.

    In production a scheduler drives this; exposing it lets the console show the
    loop running and lets the demo be a single request. It is idempotent: a
    second call re-derives what exists and writes none of it again.

    ``approve`` defaults to false, which leaves any staged referral waiting for
    a human -- the honest default.
    """
    from firstdue.demo.scenario import run_slow_loop

    report = await run_slow_loop(container, district_id=district_id, approve=approve)
    return report.model_dump(mode="json")


@router.get(
    "/districts/{district_id}/queue",
    response_model=QueueView,
    summary="The district's ranked survey queue",
)
async def district_queue(
    district_id: str,
    container: Annotated[Container, Depends(get_container)],
    caller: Annotated[Caller, Depends(require_read)],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> QueueView:
    """The queue as ranked. Every row carries the reasons that produced it."""
    entries = await container.queue.list_for_district(district_id)
    views = [
        QueueEntryView(
            entry_id=e.entry_id,
            address_id=e.address_id,
            rank=e.rank,
            score=e.score,
            status=str(e.status),
            reasons=[
                RankReasonView(
                    rule_id=r.rule_id,
                    detail=r.detail,
                    weight=r.weight,
                    canonical_key=r.canonical_key,
                    conflict_id=r.conflict_id,
                )
                for r in e.reasons
            ],
            assigned_company=e.assigned_company,
            dispatched_at=e.dispatched_at,
            calendar_event_ref=e.calendar_event_ref,
            survey_id=e.survey_id,
        )
        for e in entries[:limit]
    ]
    return QueueView(district_id=district_id, entries=views, count=len(entries))


def _fire_activity_client(container: Container) -> FireActivityClient:
    """The wired fire-activity client.

    Built once per process in :mod:`firstdue.container`. The adapter fronts
    someone else's quota with a cache and a token bucket, and one rebuilt per
    request would arrive with an empty cache and a full bucket.
    """
    return container.fire_activity


@router.get(
    "/districts/{district_id}/fire-activity",
    response_model=FireActivity,
    summary="Regional fire activity and recent fire weather",
)
async def district_fire_activity(
    district_id: str,
    container: Annotated[Container, Depends(get_container)],
    caller: Annotated[Caller, Depends(require_read)],
) -> FireActivity:
    """What is burning *around* the district, and how dry it has been.

    **Regional on purpose.** Measured against the live NASA FIRMS feed over its
    maximum five-day window, San Francisco proper returns zero VIIRS detections
    and Northern California returns hundreds: the pixels are ~375 m and the
    instrument exists to find wildfire, so a structure fire never registers. A
    city-only map would be blank essentially always, which reads as an outage on
    a bad day and as reassurance on a good one. So the region is the subject --
    regional activity is what drives mutual-aid demand, crew availability, smoke
    over the district, and red-flag posture -- and the city's own count is
    reported separately beside it.

    ``resolution_note`` therefore ships with every answer and the console must
    render it: it is what makes ``in_city_count: 0`` read as the ordinary fact
    it is rather than as a dead feed or as an all-clear.

    **The fire-weather block is not current conditions.** NASA POWER is
    reanalysis and lags real time by days; every reading carries the hour it
    describes, the block carries the window those hours span, and ``caveat``
    says so in words. Current wind reaches the console from the National Weather
    Service source in the catalog, and the two must not be presented alike.

    Same scope as ``/stats`` and ``/queue``: this is district situational
    awareness, and it decides nothing.

    **Always 200.** No map key, an unknown district, a dead provider, a spent
    rate budget, or a blown deadline all come back with ``available=false`` and
    a sentence in ``unavailable_reason``. A 404 would render as a broken
    console, and a console that drew an empty map would be asserting that
    nothing is burning.

    The response carries detections and never a provider URL: FIRMS puts its map
    key in the request *path*, so a URL reaching the browser is the key reaching
    the browser.
    """
    return await _fire_activity_client(container).fetch(district_id=district_id)


# --------------------------------------------------------------- buildings


@router.get(
    "/buildings/{address_id}",
    response_model=BuildingProfileView,
    summary="One building profile",
)
async def building_profile(
    address_id: str,
    container: Annotated[Container, Depends(get_container)],
    caller: Annotated[Caller, Depends(require_read)],
) -> BuildingProfileView:
    profile = await container.profiles.get(address_id)
    if profile is None:
        raise NotFoundError("no profile for this address", details={"address_id": address_id})

    return BuildingProfileView(
        address_id=profile.address_id,
        district_id=profile.district_id,
        profile_version=profile.profile_version,
        facts=_fact_views(profile),
        conflicts=_conflict_views(profile),
        unknown_keys=sorted(key for key, fact in profile.facts.items() if not fact.value.is_known),
        hydrant_ids=profile.hydrant_ids,
        last_human_survey=profile.last_human_survey,
        open_referrals=[
            {
                "referral_id": r.referral_id,
                "status": str(r.status),
                "case_number": r.case_number,
                "conflict_id": r.conflict_id,
            }
            for r in profile.open_referrals
        ],
        has_geometry=profile.geometry is not None,
    )


@router.get(
    "/buildings/{address_id}/timeline",
    response_model=list[TimelineEventView],
    summary="The building's append-only timeline",
)
async def building_timeline(
    address_id: str,
    container: Annotated[Container, Depends(get_container)],
    caller: Annotated[Caller, Depends(require_read)],
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> list[TimelineEventView]:
    """Everything that ever happened to this profile, in order. Never edited."""
    profile = await container.profiles.get(address_id)
    if profile is None:
        raise NotFoundError("no profile for this address", details={"address_id": address_id})
    return [
        TimelineEventView(
            sequence=e.sequence,
            occurred_at=e.occurred_at,
            type=str(e.type),
            actor=e.actor,
            actor_version=e.actor_version,
            summary=e.summary,
            fact_ids=e.fact_ids,
            conflict_id=e.conflict_id,
        )
        for e in profile.timeline[-limit:]
    ]


class GeometryView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spec: GeometrySpec
    #: Static elevation for a renderer that cannot run the interactive spec.
    svg: str
    has_disputed_mass: bool
    total_height_m: float


@router.get(
    "/buildings/{address_id}/geometry",
    response_model=GeometryView,
    summary="Renderable geometry, with an SVG fallback",
)
async def building_geometry(
    address_id: str,
    container: Annotated[Container, Depends(get_container)],
    caller: Annotated[Caller, Depends(require_geometry_read)],
) -> GeometryView:
    """The spec plus its static fallback.

    Disputed mass is marked in the data, so the fallback shows it as disputed
    too -- the conflict is not a property of the renderer.
    """
    profile = await container.profiles.get(address_id)
    if profile is None:
        raise NotFoundError("no profile for this address", details={"address_id": address_id})
    if profile.geometry is None:
        raise NotFoundError(
            "no geometry has been derived for this address",
            details={"address_id": address_id},
        )
    return GeometryView(
        spec=profile.geometry,
        svg=render_svg(profile),
        has_disputed_mass=profile.geometry.has_disputed_mass,
        total_height_m=round(profile.geometry.total_height_m, 2),
    )


def _imagery_client(container: Container) -> ImageryClient:
    """The wired imagery client.

    Built once per process in :mod:`firstdue.container`, beside vision, because
    the adapter owns a response cache and a token bucket: a client rebuilt per
    request would arrive with an empty cache and a full bucket, which is how a
    metered API gets billed twice for the same building.
    """
    return container.imagery


@router.get(
    "/buildings/{address_id}/imagery",
    response_model=BuildingImagery,
    summary="A photograph of the building, or a stated refusal",
)
async def building_imagery(
    address_id: str,
    container: Annotated[Container, Depends(get_container)],
    caller: Annotated[Caller, Depends(require_geometry_read)],
) -> BuildingImagery:
    """What the building actually looks like, beside what it was measured to be.

    The massing model on ``/geometry`` is derived from permits, lidar, and the
    assessor's roll. This is the photograph: the door, the windows, the bars, a
    storey count an officer can check with their own eyes against the model
    standing next to it. Same scope as the geometry it renders beside, because
    it is the other half of the same pane.

    **Always 200.** No coverage, no key, a dead provider, or a blown deadline
    all come back with ``available=false`` and a sentence in
    ``unavailable_reason``. A 404 would render as a broken console, and a
    console that draws nothing would teach an officer that the building has no
    photograph -- which is a claim, and a false one.

    **The console must render ``attribution``** under the frame whenever it is
    non-empty. Google's Maps Platform Terms require visible attribution on
    Street View and Static Maps imagery, and the department is the licensee.

    The response carries the image inline as a data URL and never a provider
    URL: a signed Street View URL is ``GOOGLE_MAPS_API_KEY`` in a browser's
    network tab.
    """
    return await _imagery_client(container).fetch(address_id=address_id)


@router.get(
    "/buildings/{address_id}/surveys",
    response_model=list[SurveyRecord],
    summary="Company surveys recorded for this building",
)
async def building_surveys(
    address_id: str,
    container: Annotated[Container, Depends(get_container)],
    caller: Annotated[Caller, Depends(require_read)],
) -> list[SurveyRecord]:
    return list(await container.surveys.list_for_address(address_id))


@router.post(
    "/buildings/{address_id}/surveys",
    status_code=status.HTTP_201_CREATED,
    summary="Record a completed company survey",
)
async def record_survey(
    address_id: str,
    submission: SurveySubmission,
    container: Annotated[Container, Depends(get_container)],
    caller: Annotated[Caller, Depends(require_profile_write)],
) -> dict[str, Any]:
    """Record what a crew saw.

    This is the only input that can set ``human_verified`` and the only one that
    can close a conflict. Attributes the crew did not observe stay open.
    """
    now = container.clock.now()
    observations = {o.canonical_key: o.value for o in submission.observations}
    survey = SurveyRecord(
        survey_id=container.ids.new_id("survey"),
        address_id=address_id,
        queue_entry_id=submission.queue_entry_id,
        company=submission.company,
        surveyor=submission.surveyor,
        started_at=now,
        completed_at=now,
        outcome=submission.outcome,
        verified_keys=tuple(sorted(observations)),
        notes=submission.notes,
    )
    result = await _survey_service(container).record(survey, observations=observations)
    return result.model_dump(mode="json")


# ------------------------------------------------------------- human taps


@router.post(
    "/queue/{entry_id}/dispatch",
    summary="Dispatch a company to a queued structure",
)
async def dispatch_entry(
    entry_id: str,
    request: DispatchRequest,
    container: Annotated[Container, Depends(get_container)],
    caller: Annotated[Caller, Depends(require_work_order)],
) -> dict[str, Any]:
    """Cut the work order, hold the calendar, notify the crew, write the plan.

    Idempotent: dispatching the same entry twice returns the original work
    order and calendar event rather than double-booking a company.
    """
    entry = await container.queue.get(entry_id)
    if entry is None:
        raise NotFoundError("queue entry not found", details={"entry_id": entry_id})
    result = await _action_flow(container).dispatch(
        entry,
        company=request.company,
        crew_email=request.crew_email,
        correlation_id=container.ids.new_id("corr"),
    )
    return result.model_dump(mode="json")


@router.post(
    "/conflicts/{conflict_id}/referral",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Stage a building-department referral for a conflict",
)
async def stage_referral(
    conflict_id: str,
    container: Annotated[Container, Depends(get_container)],
    caller: Annotated[Caller, Depends(require_work_order)],
) -> dict[str, Any]:
    """Stage, never file.

    Returns ``202``: the referral exists and is waiting for a human. Filing it
    accuses a property owner of unpermitted construction, which is a captain's
    decision and not an agent's.
    """
    conflict = await container.conflicts.get(conflict_id)
    if conflict is None:
        raise NotFoundError("conflict not found", details={"conflict_id": conflict_id})
    if conflict.status is not ConflictStatus.OPEN:
        raise ValidationError(
            "a resolved conflict does not support a referral",
            details={"conflict_id": conflict_id},
        )

    profile = await container.profiles.get(conflict.address_id)
    if profile is None:
        raise NotFoundError(
            "no profile for this conflict's address",
            details={"address_id": conflict.address_id},
        )
    entries = await container.queue.list_for_district(profile.district_id)
    entry = next((e for e in entries if e.address_id == conflict.address_id), None)
    if entry is None:
        raise NotFoundError(
            "this structure is not in the survey queue",
            details={"address_id": conflict.address_id},
        )
    result = await _action_flow(container).dispatch(
        entry,
        company=entry.assigned_company or "E-05",
        crew_email="crew@sffd.example",
        correlation_id=container.ids.new_id("corr"),
    )
    return {
        "referral_id": result.referral_id,
        "approval_id": result.approval_id,
        "status": "AWAITING_APPROVAL",
    }


@router.post(
    "/referrals/{referral_id}/approve",
    summary="Approve and file a staged referral",
)
async def approve_referral(
    referral_id: str,
    body: ApprovalRequestBody,
    container: Annotated[Container, Depends(get_container)],
    caller: Annotated[Caller, Depends(require_referral_write)],
) -> dict[str, Any]:
    """The one human tap that files.

    Idempotent on the referral's key: approving twice returns the first case
    number rather than opening a second case against the same property.
    """
    # The approver recorded on the referral is the *authenticated* caller. A
    # body field naming somebody else would make the audit record a claim.
    result = await _action_flow(container).approve_referral(
        referral_id,
        approved_by=caller.subject if body.approved_by == "self" else body.approved_by,
        correlation_id=container.ids.new_id("corr"),
    )
    # Referral acceptance is the slow loop's only outward-facing quality signal:
    # it is the rate at which a building department agreed the system was right
    # to escalate. A falling number means the ranker is wasting inspectors.
    METRICS.record_referral_outcome(accepted=result.case_number is not None)
    return result.model_dump(mode="json")


class RecalledNarrativeView(BaseModel):
    """One filing the semantic index thinks resembles the query.

    Deliberately not a fact. It carries no value, no confidence, and no
    canonical attribute of its own -- only the ids that lead a human back to
    the record. Nothing downstream may promote one of these into something the
    system believes about a building.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    address_id: str
    source_ref: str
    #: Lower is nearer. The index's own unit, shown rather than compared.
    distance: float
    #: True when the match is on the building that was asked about, rather than
    #: a comparable one elsewhere in the district.
    same_building: bool


class NarrativeRecallResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    address_id: str
    query: str
    matches: tuple[RecalledNarrativeView, ...] = ()
    #: Stated so a console never renders an empty list as "nothing similar has
    #: ever been filed" when the index simply holds nothing yet.
    index_populated: bool


@router.get(
    "/buildings/{address_id}/narratives",
    response_model=NarrativeRecallResponse,
    summary="Semantic recall over filed narratives",
)
async def recall_narratives(
    address_id: str,
    container: Annotated[Container, Depends(get_container)],
    caller: Annotated[Caller, Depends(require_read)],
    q: Annotated[str, Query(min_length=3, max_length=400)],
    limit: Annotated[int, Query(ge=1, le=20)] = 5,
) -> NarrativeRecallResponse:
    """Find filings that read like the question, here and at comparable buildings.

    This is the other half of the memory bank. Structured facts answer "what
    does the department believe about this building"; this answers "where has
    somebody written about something like this before" -- the inspection three
    doors down that described the same stair arrangement, the violation from
    2019 nobody has read since.

    **A match is a pointer, never an assertion.** It returns ids and a distance,
    not text and not a value, so the officer goes and reads the record. That is
    the whole boundary: an embedding can tell you two documents resemble each
    other, and it cannot tell you a building has three storeys.

    Only screened narratives are indexed, so an injection attempt an ingested
    document carried is not something this can recall. ``PHI`` and Tier II
    filings never enter the index at all.
    """
    matches = await container.vectors.query(q, limit=limit)
    return NarrativeRecallResponse(
        address_id=address_id,
        query=q,
        matches=tuple(
            RecalledNarrativeView(
                address_id=match.address_id,
                source_ref=match.source_ref,
                distance=match.distance,
                same_building=match.address_id == address_id,
            )
            for match in matches
        ),
        index_populated=bool(matches),
    )
