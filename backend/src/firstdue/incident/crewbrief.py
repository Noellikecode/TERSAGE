"""The crew brief: prose a crew reads, assembled from things somebody recorded.

Same discipline as the enriched brief and the same shape, deliberately. The
deterministic half is a list of **claims**, each one a sentence with the fact
ids, canonical keys and face labels it came from hanging off it. The optional
half is a model wording those claims into continuous prose. What the model
contributes is sentence construction; what it may not contribute is a claim.

**The guard is arithmetic, not a promise.** Every number in an accepted
composition has to appear in the claims it was composed from -- extracted with
a word-boundary match and compared as a set. A model that rounded 16.29 m to
16 m, invented a storey count, or carried a temperature across from another
building fails that check and the brief lands with the deterministic wording
instead. It is a crude test and it is a *checkable* one, which is the property
that matters: "the model was told not to invent facts" is not a control.

**Tactical language is screened for the same reason.** This system delivers
information and performs clerical execution; a sentence telling a crew to go
interior is not a wording of a claim, it is a new claim about what should
happen. A composition containing one is refused whole.

**Refusal is ordinary, and it is the only thing that ever fails.** No model
wired, a model that times out, a model that raises, a run with no time left to
ask one, a composition that fails either screen -- every one of them lands the
same brief with ``prose_source="deterministic"`` and, where there was something
to say, the reason. There is no path where a crew is handed prose nothing
checked, and -- since the composition runs inside a run the runtime cancels on
a hard cap -- no path where a slow model costs the package it was wording.

The claims themselves are the record's, not this module's. Each is built from a
value that already exists with a provenance -- a resolved fact, a coverage
report, a criterion verdict, a leg of a computed path -- and every one of them
carries the ids it was built from, so a line on the page can be traced back to
the thing that put it there. Anything unknown is stated as unknown; there is no
claim anywhere in this module that is constructed from an absent value.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from datetime import datetime
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, computed_field

from firstdue.domain.profiles import ProfileSnapshot
from firstdue.errors import UpstreamTimeoutError
from firstdue.incident.documents import RecordedDocument
from firstdue.incident.entrypath import EntryPathPlan
from firstdue.incident.fusion import THERMAL_CAVEAT, FaceCoverage
from firstdue.incident.readiness import ReadinessAssessment
from firstdue.observability.logging import get_logger
from firstdue.ports.model import ModelClient

logger = get_logger(__name__)

CREW_BRIEF_TEMPLATE: Final[str] = "brief.crew.v1"
CREW_BRIEF_MAX_CHARS: Final[int] = 3_000

#: What the wording is worth waiting for when nothing tighter is asked for.
#:
#: It was 4 s, and against a real Vertex endpoint that number could never be
#: met. Measured on `gemini-3.5-flash` at `global`, one `compose` of a crew
#: brief: 6.97 s, 6.05 s, 5.72 s, 6.35 s, 5.94 s. The *fastest* of those is
#: over the old budget, so the composition was refused for time on every live
#: incident and the package it belonged to was cancelled with it -- while fake
#: mode, answering in microseconds, passed every test.
#:
#: 10 s is the slowest observed call plus room for a cold one, on the same
#: reasoning `sensor-fusion` uses for its 12 s frame cap. It is a ceiling for a
#: call that is merely slow, not a target: nothing waits for the whole of it,
#: and `CREW_BRIEF_TIMEOUT_GRACE_MS` still hands the client the smaller number
#: so a refusal arrives named rather than as an anonymous cancellation.
CREW_BRIEF_DEADLINE_MS: Final[int] = 10_000

#: Held back from whatever budget the caller gives, so the *client* reaches its
#: own deadline and raises the refusal it knows how to name before the bound
#: below cancels it anonymously. Two paths to the same code, and the one that
#: comes with a ``model_ref`` is the better one to take.
CREW_BRIEF_TIMEOUT_GRACE_MS: Final[int] = 250

#: Below this, the model is not called at all. A composition given a quarter of
#: a second is a composition that will be refused for time; refusing it here
#: costs nothing, states itself, and hands the rest of the budget to the work
#: that actually stages the package.
MIN_CREW_BRIEF_DEADLINE_MS: Final[int] = 500

#: Numbers, as whole tokens. The word boundaries matter: without them the ``1``
#: in a template id like ``brief.crew.v1`` reads as a numeric claim and every
#: composition is refused for a digit nobody wrote.
_NUMBER = re.compile(r"\b\d+(?:\.\d+)?\b")

#: Language that would make the prose a recommendation rather than a reading of
#: the record. Screened rather than trusted: the deterministic claims below
#: contain none of it, so a composition that does introduced it.
FORBIDDEN_PHRASES: Final[tuple[str, ...]] = (
    "recommend",
    "should ",
    "must ",
    "advise",
    "offensive",
    "defensive",
    "evacuat",
    "go interior",
    "vent the roof",
    "pull out",
    "will collapse",
    "assign engine",
    "attack",
)


class BriefClaim(BaseModel):
    """One thing the record says, and where it says it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_id: str = Field(min_length=1, max_length=60)
    #: Which part of the brief it belongs under. Fixed vocabulary in
    #: :data:`SECTION_ORDER`, so the page is the same shape every time.
    section: str = Field(min_length=1, max_length=40)
    text: str = Field(min_length=1, max_length=600)
    #: Fact ids, canonical keys, face labels, conflict ids, node ids, criterion
    #: ids. Never a value that is not already in ``text`` with its source.
    refs: tuple[str, ...] = ()


#: The order the sections print in. Reading order for a crew: what the record
#: could not settle first, then the structure, then what was measured on it,
#: then the route, then the caveats that travel with all of it.
SECTION_ORDER: Final[tuple[str, ...]] = (
    "READINESS",
    "STRUCTURE",
    "THERMAL",
    "ROUTE",
    "UNKNOWNS",
    "CAVEATS",
)


class CrewBrief(RecordedDocument):
    """One synthesised brief. Deterministic claims, optional model wording."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    brief_id: str = Field(min_length=1, max_length=120)
    incident_id: str = Field(min_length=1, max_length=120)
    address_id: str = Field(min_length=1, max_length=120)
    composed_at: datetime
    composed_by: str = Field(min_length=1, max_length=120)
    composed_by_version: str = Field(default="1.0.0", max_length=40)
    profile_snapshot_id: str = Field(min_length=1, max_length=120)

    claims: tuple[BriefClaim, ...] = Field(min_length=1)
    #: The prose a crew reads. Always present: the deterministic rendering when
    #: no accepted composition exists, so there is never a blank page.
    prose: str = Field(min_length=1, max_length=20_000)
    #: ``deterministic`` or ``model``.
    prose_source: str = Field(default="deterministic", max_length=20)
    #: Why a composition was refused, when one was. A stable code.
    prose_rejection: str = Field(default="", max_length=60)
    model_ref: str = Field(default="", max_length=120)

    #: Attributes with no record, carried explicitly rather than elided.
    unknowns: tuple[str, ...] = ()
    readiness_summary: str = Field(min_length=1, max_length=300)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def claim_refs(self) -> tuple[str, ...]:
        """Every id and key the brief rests on, deduplicated in claim order."""
        seen: list[str] = []
        for claim in self.claims:
            for ref in claim.refs:
                if ref not in seen:
                    seen.append(ref)
        return tuple(seen)

    def section(self, name: str) -> tuple[BriefClaim, ...]:
        return tuple(claim for claim in self.claims if claim.section == name)


# ----------------------------------------------------------------- the claims


def _readiness_claims(assessment: ReadinessAssessment) -> list[BriefClaim]:
    claims = [
        BriefClaim(
            claim_id="readiness.verdict",
            section="READINESS",
            text=assessment.summary,
            refs=(assessment.profile_snapshot_id, *assessment.failed_ids),
        )
    ]
    claims.extend(
        BriefClaim(
            claim_id=f"readiness.{criterion.criterion_id}",
            section="READINESS",
            text=f"{criterion.title}: {'met' if criterion.passed else 'NOT met'} -- "
            f"{criterion.reason}",
            refs=(criterion.criterion_id, *criterion.refs),
        )
        for criterion in assessment.criteria
    )
    return claims


def _structure_claims(snapshot: ProfileSnapshot) -> list[BriefClaim]:
    """Only resolved facts. An absent one becomes an unknown, never a sentence."""
    claims: list[BriefClaim] = []
    spec = snapshot.geometry
    if spec is not None:
        claims.append(
            BriefClaim(
                claim_id="structure.massing",
                section="STRUCTURE",
                text=(
                    f"The measured massing model carries {len(spec.levels)} storey(s) over a "
                    f"{len(spec.footprint)}-sided footprint, total height "
                    f"{spec.total_height_m:g} m, collapse zone "
                    f"{spec.collapse_zone_radius_m:g} m at the 1.5x standard applied to that "
                    "height. A geometric standard, not a prediction about this fire."
                ),
                refs=(snapshot.address_id, snapshot.snapshot_id),
            )
        )
        if spec.has_disputed_mass:
            disputed = [
                f"level-{index}"
                for index, level in enumerate(spec.levels)
                if str(level.status) == "DISPUTED"
            ]
            claims.append(
                BriefClaim(
                    claim_id="structure.disputed-mass",
                    section="STRUCTURE",
                    text=(
                        f"{len(disputed)} storey(s) of that mass are DISPUTED: the records "
                        "disagree that they are there, and both records are retained."
                    ),
                    refs=tuple(disputed),
                )
            )

    for key, fact in sorted(snapshot.facts.items()):
        if not fact.value.is_known:
            continue
        claims.append(
            BriefClaim(
                claim_id=f"fact.{key}",
                section="STRUCTURE",
                text=f"{key} is {fact.value.render()}, from {fact.source_type}.",
                refs=(key, fact.fact_id),
            )
        )

    for conflict in sorted(snapshot.conflicts, key=lambda c: (-c.severity, c.conflict_id)):
        claims.append(
            BriefClaim(
                claim_id=f"conflict.{conflict.conflict_id}",
                section="STRUCTURE",
                text=f"Open disagreement, severity {conflict.severity}: {conflict.summary}",
                refs=(conflict.conflict_id, conflict.canonical_key, *conflict.fact_ids),
            )
        )
    return claims


def _thermal_claims(coverage: Sequence[FaceCoverage]) -> list[BriefClaim]:
    """One line per wall, plus the caveat once.

    This used to hand back `report.render` verbatim, on the reasoning that the
    coverage report already words itself and a second wording would be a second
    version of one reading. That was right about the *reading* and wrong about
    the page: `render` ends with `THERMAL_CAVEAT`, so four walls printed the
    same two sentences about what thermal imaging can and cannot see four
    times, around an ISO timestamp to the microsecond. What a crew got was
    ~600 characters saying, somewhere inside it, "Bravo is hot".

    So the numbers lead and the caveat is stated once, as its own claim. It is
    not dropped: "a hot surface has many causes" is the sentence that stops a
    reading being taken for a diagnosis, and it has to be on the page. It has
    to be on it once.
    """
    claims = [
        BriefClaim(
            claim_id=f"thermal.{report.face}",
            section="THERMAL",
            text=(
                f"{report.face}: {report.peak_c:.0f} C peak, "
                f"{report.coverage * 100:.0f}% of the wall read"
                if report.scanned and report.peak_c is not None
                else f"{report.face}: not flown — treat as unknown, never as cool"
            ),
            refs=(str(report.face),),
        )
        for report in coverage
    ]
    if any(report.scanned for report in coverage):
        claims.append(
            BriefClaim(
                claim_id="thermal.caveat",
                # CAVEATS, not THERMAL. The invariant the suite enforces is
                # that a claim carrying no refs asserts nothing -- and this one
                # does not: it is what the instrument cannot tell you, which is
                # true of every reading above it and of none of them
                # particularly. Filing it under THERMAL would have made it look
                # like a fifth wall.
                section="CAVEATS",
                text=THERMAL_CAVEAT,
                refs=(),
            )
        )
    return claims


def _route_claims(plan: EntryPathPlan) -> list[BriefClaim]:
    if plan.refused:
        return [
            BriefClaim(
                claim_id="route.refused",
                section="ROUTE",
                text=f"No route was computed. {plan.refusal_reason}",
                refs=plan.refusal_refs,
            )
        ]
    claims: list[BriefClaim] = []
    if plan.entry is not None:
        claims.append(
            BriefClaim(
                claim_id="route.entry",
                section="ROUTE",
                text=(
                    f"The cheapest traverse of the navigable graph enters on "
                    f"{plan.entry_face or 'an unlabelled face'} and reaches storey "
                    f"{plan.target_level + 1}: {plan.entry.total_distance_m:g} m over "
                    f"{len(plan.entry.legs)} leg(s) at a weighted cost of "
                    f"{plan.entry.total_cost:g}."
                ),
                refs=tuple(w.node_id for w in plan.entry.waypoints),
            )
        )
        claims.extend(
            BriefClaim(
                claim_id=f"route.leg.{index}",
                section="ROUTE",
                text=f"Leg {index + 1}, {leg.from_id} to {leg.to_id}: {leg.chose_because}.",
                refs=(leg.from_id, leg.to_id, *(term.term_id for term in leg.terms)),
            )
            for index, leg in enumerate(plan.entry.legs)
        )
    if plan.egress is not None:
        claims.append(
            BriefClaim(
                claim_id="route.egress",
                section="ROUTE",
                text=(
                    f"A second way out leaves by {plan.egress.waypoints[-1].node_id}: "
                    f"{plan.egress.total_distance_m:g} m at a weighted cost of "
                    f"{plan.egress.total_cost:g}."
                ),
                refs=tuple(w.node_id for w in plan.egress.waypoints),
            )
        )
    elif plan.egress_note:
        claims.append(
            BriefClaim(
                claim_id="route.egress-absent",
                section="ROUTE",
                text=plan.egress_note,
                refs=(plan.address_id,),
            )
        )
    claims.extend(
        BriefClaim(
            claim_id=f"route.barrier.{index}",
            section="ROUTE",
            text=f"Not traversable: {barrier.reason}.",
            refs=(barrier.from_id, barrier.to_id, *barrier.refs),
        )
        for index, barrier in enumerate(plan.barriers)
    )
    return claims


def _unknown_claims(snapshot: ProfileSnapshot, plan: EntryPathPlan) -> list[BriefClaim]:
    claims: list[BriefClaim] = []
    absent = sorted(key for key, fact in snapshot.facts.items() if not fact.value.is_known)
    if absent:
        claims.append(
            BriefClaim(
                claim_id="unknown.attributes",
                section="UNKNOWNS",
                text=(
                    f"{len(absent)} attribute(s) on file carry no known value: "
                    f"{', '.join(absent)}. Unknown, not absent."
                ),
                refs=tuple(absent)[:12],
            )
        )
    if plan.unscanned_faces:
        claims.append(
            BriefClaim(
                claim_id="unknown.faces",
                section="UNKNOWNS",
                text=(
                    f"{len(plan.unscanned_faces)} face(s) have no current thermal coverage: "
                    f"{', '.join(plan.unscanned_faces)}. Nobody has flown them; they are not "
                    "cool, they are unmeasured, and the route prices them as unknown."
                ),
                refs=plan.unscanned_faces,
            )
        )
    if not claims:
        claims.append(
            BriefClaim(
                claim_id="unknown.none",
                section="UNKNOWNS",
                text=(
                    "Every attribute on this profile and every face of this building carries "
                    "a value somebody recorded."
                ),
                refs=(snapshot.snapshot_id,),
            )
        )
    return claims


def _caveat_claims() -> list[BriefClaim]:
    return [
        BriefClaim(
            claim_id="caveat.thermal",
            section="CAVEATS",
            text=THERMAL_CAVEAT,
        ),
        BriefClaim(
            claim_id="caveat.scope",
            section="CAVEATS",
            text=(
                "This is a reading of the record, not a plan of action. The route is the "
                "cheapest traverse of a graph whose costs are printed beside it; every "
                "tactical decision belongs to the incident commander."
            ),
        ),
    ]


def build_claims(
    *,
    snapshot: ProfileSnapshot,
    coverage: Sequence[FaceCoverage],
    assessment: ReadinessAssessment,
    plan: EntryPathPlan,
) -> tuple[BriefClaim, ...]:
    """Everything the brief may say, in section order. Nothing else may be said."""
    claims = (
        _readiness_claims(assessment)
        + _structure_claims(snapshot)
        + _thermal_claims(coverage)
        + _route_claims(plan)
        + _unknown_claims(snapshot, plan)
        + _caveat_claims()
    )
    rank = {name: index for index, name in enumerate(SECTION_ORDER)}
    return tuple(sorted(claims, key=lambda c: rank.get(c.section, len(rank))))


def render(claims: Sequence[BriefClaim]) -> str:
    """The deterministic prose. The floor, and the fallback.

    One heading per section and one sentence per claim, in the order
    :func:`build_claims` fixed. It is plain and it is complete, which is the
    right trade for the version that has to work when nothing else does.
    """
    lines: list[str] = []
    for section in SECTION_ORDER:
        rows = [claim for claim in claims if claim.section == section]
        if not rows:
            continue
        lines.append(section)
        lines.extend(f"  - {row.text}" for row in rows)
        lines.append("")
    return "\n".join(lines).strip()


# ------------------------------------------------------------- the composition


def numbers_in(text: str) -> frozenset[str]:
    """Every standalone numeric token. The unit the guard compares in."""
    return frozenset(_NUMBER.findall(text))


def accepts(text: str, claims: Sequence[BriefClaim]) -> str:
    """Empty when the composition may ship; otherwise a stable rejection code.

    Three checks, in the order that makes the failure clearest: is there prose
    at all, does it stay inside the claims' arithmetic, and does it stay a
    description rather than becoming an instruction.
    """
    stripped = text.strip()
    if not stripped:
        return "EMPTY_COMPOSITION"
    corpus = numbers_in(" ".join(claim.text for claim in claims))
    invented = numbers_in(stripped) - corpus
    if invented:
        return "NUMBER_NOT_IN_CLAIMS"
    lowered = stripped.lower()
    if any(phrase in lowered for phrase in FORBIDDEN_PHRASES):
        return "TACTICAL_LANGUAGE"
    return ""


async def compose(
    *,
    brief_id: str,
    incident_id: str,
    snapshot: ProfileSnapshot,
    coverage: Sequence[FaceCoverage],
    assessment: ReadinessAssessment,
    plan: EntryPathPlan,
    now: datetime,
    composed_by: str,
    composed_by_version: str = "1.0.0",
    model: ModelClient | None = None,
    deadline_ms: int | None = None,
) -> CrewBrief:
    """Build the claims, then let a model word them -- or not.

    The deterministic brief is produced first and unconditionally, so the return
    value is the same shape whether or not a model was reachable. Only the
    ``prose`` and the two fields describing where it came from ever differ, and
    the claims -- which are what the refs, the PDF and any later audit read --
    are identical either way.

    **The model call cannot outlive its budget, whatever the client does.**
    That sentence used to be a description of ``ModelClient`` and it is now
    enforced here, because the client is a protocol and the caller is inside a
    run the runtime cancels on a hard cap. A client that hung -- a socket with
    no read timeout, a recorded-response layer waiting on a lock, a stub in a
    test -- took the whole composition down with it: the runtime cancelled the
    handler mid-``compose_entry_package``, the package was never staged, and
    what a commander got was nothing at all. A refused *wording* costs a
    paragraph of prose. A cancelled *composition* costs the entry plan, and the
    two are not close enough to leave to a promise.

    ``deadline_ms`` is how much of the enclosing run the caller is willing to
    spend on wording. Whatever it is, the deterministic brief is what comes back
    when it is spent -- with the reason on ``prose_rejection``, never blank.
    """
    claims = build_claims(snapshot=snapshot, coverage=coverage, assessment=assessment, plan=plan)
    deterministic = render(claims)
    prose = deterministic
    source = "deterministic"
    rejection = ""
    model_ref = ""

    # The smaller of what the wording is worth and what the run can spare. Both
    # halves are needed: without the first, a generous run deadline would let a
    # paragraph of prose consume a budget the loop wanted for the sweep; without
    # the second, a tight run cancels the composition rather than the call.
    budget_ms = (
        CREW_BRIEF_DEADLINE_MS if deadline_ms is None else min(CREW_BRIEF_DEADLINE_MS, deadline_ms)
    )
    if model is not None and budget_ms < MIN_CREW_BRIEF_DEADLINE_MS:
        # Stated rather than attempted. The composition continues; only the
        # wording is given up, and it is given up in time to stage the package
        # the wording was going to sit inside.
        model = None
        rejection = "NO_MODEL_BUDGET"
        logger.warning(
            "crew_brief_composition_unavailable",
            extra={"incident_id": incident_id, "reason": rejection, "budget_ms": budget_ms},
        )

    if model is not None:
        try:
            # Two bounds, deliberately nested. The inner one is the client's own
            # and produces a named refusal; the outer one is this module's and
            # produces a cancellation it converts to the same code. The client
            # is given the smaller number so it reaches its deadline first and
            # the anonymous path stays the one nothing ever takes.
            async with asyncio.timeout(budget_ms / 1000.0):
                result = await model.compose(
                    template_id=CREW_BRIEF_TEMPLATE,
                    # The claims and nothing else. The model is not handed the
                    # profile, the log, or the transcript: it cannot repeat a
                    # fact it was never shown, and what it was shown is exactly
                    # what the guard below compares its output against.
                    fields={"claims": [claim.text for claim in claims]},
                    max_chars=CREW_BRIEF_MAX_CHARS,
                    deadline_ms=max(
                        MIN_CREW_BRIEF_DEADLINE_MS, budget_ms - CREW_BRIEF_TIMEOUT_GRACE_MS
                    ),
                )
        except (UpstreamTimeoutError, TimeoutError):
            rejection = "UPSTREAM_TIMEOUT"
            logger.warning(
                "crew_brief_composition_unavailable",
                extra={"incident_id": incident_id, "reason": rejection},
            )
        except Exception as exc:
            # Every remaining way a model client can fail, folded into the one
            # outcome that was always the answer: the deterministic wording. A
            # narrower ``except`` here reads as discipline and is not -- it
            # decides which unforeseen vendor error is allowed to cost the
            # commander the entry plan. ``CancelledError`` is a ``BaseException``
            # and is deliberately not caught: the enclosing run really is being
            # torn down and this must not pretend otherwise.
            rejection = "MODEL_UNAVAILABLE"
            logger.warning(
                "crew_brief_composition_unavailable",
                extra={
                    "incident_id": incident_id,
                    "reason": rejection,
                    "error_type": type(exc).__name__,
                },
            )
        else:
            model_ref = result.model_ref
            if not result.accepted:
                rejection = "MODEL_REFUSED"
            else:
                rejection = accepts(result.text, claims)
                if not rejection:
                    prose = result.text.strip()[:CREW_BRIEF_MAX_CHARS]
                    source = "model"
            if rejection:
                logger.warning(
                    "crew_brief_composition_rejected",
                    extra={"incident_id": incident_id, "reason": rejection},
                )

    return CrewBrief(
        brief_id=brief_id,
        incident_id=incident_id,
        address_id=snapshot.address_id,
        composed_at=now,
        composed_by=composed_by,
        composed_by_version=composed_by_version,
        profile_snapshot_id=snapshot.snapshot_id,
        claims=claims,
        prose=prose,
        prose_source=source,
        prose_rejection=rejection,
        model_ref=model_ref,
        unknowns=tuple(
            sorted(key for key, fact in snapshot.facts.items() if not fact.value.is_known)
        ),
        readiness_summary=assessment.summary,
    )


__all__ = [
    "CREW_BRIEF_DEADLINE_MS",
    "CREW_BRIEF_TEMPLATE",
    "MIN_CREW_BRIEF_DEADLINE_MS",
    "FORBIDDEN_PHRASES",
    "SECTION_ORDER",
    "BriefClaim",
    "CrewBrief",
    "accepts",
    "build_claims",
    "compose",
    "numbers_in",
    "render",
]
