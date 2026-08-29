"""The crew brief: a model may write the sentences and may not write the facts.

The interesting tests are the refusals. A composition that rounds a measurement,
invents a storey count, or tells a crew what to do is rejected whole and the
deterministic rendering ships instead -- and the brief says which screen refused
it, because "no model was wired" and "the model said something it should not"
look identical on a page that does not distinguish them.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from typing import Any

import pytest

from firstdue.domain.enums import AssertionStatus, FaceLabel, SourceType
from firstdue.domain.geometry import Face, GeometrySpec, Level, collapse_zone_radius
from firstdue.domain.keys import IntakeKeys, Keys
from firstdue.domain.profiles import ProfileSnapshot
from firstdue.domain.values import BooleanValue, UnknownValue
from firstdue.errors import UpstreamTimeoutError
from firstdue.incident.crewbrief import (
    CREW_BRIEF_TIMEOUT_GRACE_MS,
    MIN_CREW_BRIEF_DEADLINE_MS,
    SECTION_ORDER,
    accepts,
    build_claims,
    compose,
    numbers_in,
    render,
)
from firstdue.incident.entrypath import compute_entry_path
from firstdue.incident.fusion import THERMAL_CAVEAT, FaceCoverage
from firstdue.incident.readiness import assess
from firstdue.ports.model import ProseChunk, ProseResult

NOW = datetime(2026, 8, 20, 8, 0, 0, tzinfo=UTC)
GROUND = (FaceLabel.ALPHA, FaceLabel.BRAVO, FaceLabel.CHARLIE, FaceLabel.DELTA)
ADDRESS = "sf-0450-hayes"


class StubModel:
    """A model client that returns exactly what a test tells it to.

    Only ``compose`` is reached; the other verbs raise so a change that started
    calling one of them from the brief path fails loudly rather than quietly.
    """

    def __init__(self, result: ProseResult | Exception) -> None:
        self._result = result
        self.calls: list[Mapping[str, Any]] = []

    async def compose(
        self, *, template_id: str, fields: Mapping[str, Any], max_chars: int, deadline_ms: int
    ) -> ProseResult:
        self.calls.append({"template_id": template_id, "fields": fields})
        if isinstance(self._result, Exception):
            raise self._result
        return self._result

    async def triage(self, **kwargs: Any) -> Any:  # pragma: no cover - never called
        raise AssertionError("the crew brief must not triage")

    async def extract(self, **kwargs: Any) -> Any:  # pragma: no cover - never called
        raise AssertionError("the crew brief must not extract")

    def compose_stream(self, **kwargs: Any) -> AsyncIterator[ProseChunk]:  # pragma: no cover
        raise AssertionError("the crew brief does not stream")

    async def explain(self, **kwargs: Any) -> Any:  # pragma: no cover - never called
        raise AssertionError("the crew brief must not explain")


def prose(text: str, *, accepted: bool = True) -> ProseResult:
    return ProseResult(
        text=text,
        accepted=accepted,
        rejection_reason=None if accepted else "contract validation failed",
        model_ref="stub/1",
    )


def geometry() -> GeometrySpec:
    return GeometrySpec(
        address_id=ADDRESS,
        generated_at=NOW,
        footprint=((0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)),
        levels=(
            Level(height_m=3.5, provenance=SourceType.PERMIT, status=AssertionStatus.CONFIRMED),
        ),
        faces=tuple(Face(label=label) for label in GROUND),
        collapse_zone_radius_m=collapse_zone_radius(3.5),
    )


def coverage() -> tuple[FaceCoverage, ...]:
    return tuple(
        FaceCoverage(
            face=label,
            scanned=True,
            observed_at=NOW,
            peak_c=42.0,
            coverage=1.0,
            render=f"42 C peak surface temperature. {THERMAL_CAVEAT}",
        )
        for label in GROUND
    )


@pytest.fixture
def snapshot(make_fact) -> ProfileSnapshot:
    return ProfileSnapshot(
        address_id=ADDRESS,
        district_id="sffd-district-03",
        profile_version=4,
        snapshot_id="snap-1",
        read_at=NOW,
        facts={
            Keys.LIGHTWEIGHT_TRUSS: make_fact(
                address_id=ADDRESS, key=Keys.LIGHTWEIGHT_TRUSS, value=BooleanValue(boolean=True)
            ),
            Keys.SUPPRESSION_SPRINKLERED: make_fact(
                address_id=ADDRESS,
                key=Keys.SUPPRESSION_SPRINKLERED,
                value=UnknownValue(checked_sources=("permits",)),
            ),
        },
        geometry=geometry(),
    )


@pytest.fixture
def parts(snapshot: ProfileSnapshot):
    assessment = assess(
        incident_id="inc-1",
        snapshot=snapshot,
        coverage=coverage(),
        now=NOW,
        reported_keys=(IntakeKeys.ACCESS_NOTE,),
        narratives_read=1,
        assessed_by="incident-interceptor",
    )
    plan = compute_entry_path(
        incident_id="inc-1", spec=geometry(), coverage=coverage(), facts=snapshot.facts
    )
    return assessment, plan


class SilentModel:
    """A model that accepts the call and never answers it.

    Not a timeout the client raises -- a *hang*. It is the failure a protocol
    cannot rule out and the one that used to be fatal: the composition awaited
    forever, the runtime cancelled the whole run at the interceptor's six
    seconds, and the entry package the wording was for was never staged.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.deadlines: list[int] = []

    async def compose(
        self, *, template_id: str, fields: Mapping[str, Any], max_chars: int, deadline_ms: int
    ) -> ProseResult:
        self.calls += 1
        self.deadlines.append(deadline_ms)
        await asyncio.Event().wait()
        raise AssertionError("unreachable")  # pragma: no cover

    async def triage(self, **kwargs: Any) -> Any:  # pragma: no cover - never called
        raise AssertionError("the crew brief must not triage")

    async def extract(self, **kwargs: Any) -> Any:  # pragma: no cover - never called
        raise AssertionError("the crew brief must not extract")

    def compose_stream(self, **kwargs: Any) -> AsyncIterator[ProseChunk]:  # pragma: no cover
        raise AssertionError("the crew brief does not stream")

    async def explain(self, **kwargs: Any) -> Any:  # pragma: no cover - never called
        raise AssertionError("the crew brief must not explain")


async def brief(snapshot, parts, *, model: object | None = None, deadline_ms: int | None = None):
    assessment, plan = parts
    return await compose(
        brief_id="crewbrief-1",
        incident_id="inc-1",
        snapshot=snapshot,
        coverage=coverage(),
        assessment=assessment,
        plan=plan,
        now=NOW,
        composed_by="incident-interceptor",
        model=model,  # type: ignore[arg-type]
        deadline_ms=deadline_ms,
    )


# ------------------------------------------------------------- the claims


@pytest.mark.invariant
async def test_with_no_model_the_brief_is_the_deterministic_rendering(snapshot, parts) -> None:
    """Works credential-free, like the rest of the system."""
    result = await brief(snapshot, parts)
    assert result.prose_source == "deterministic"
    assert result.prose_rejection == ""
    assert result.model_ref == ""
    assert result.prose == render(result.claims)
    for section in SECTION_ORDER:
        assert section in result.prose


@pytest.mark.invariant
async def test_every_claim_that_asserts_something_cites_where_it_came_from(snapshot, parts) -> None:
    """The caveats are the only claims with no refs, and they assert nothing."""
    result = await brief(snapshot, parts)
    for claim in result.claims:
        if claim.section == "CAVEATS":
            continue
        assert claim.refs, claim.claim_id


@pytest.mark.invariant
async def test_an_attribute_with_no_known_value_is_stated_unknown_never_asserted(
    snapshot, parts
) -> None:
    result = await brief(snapshot, parts)
    assert Keys.SUPPRESSION_SPRINKLERED in result.unknowns
    # It never appears as a structural claim, because there is nothing to claim.
    assert not any(
        claim.claim_id == f"fact.{Keys.SUPPRESSION_SPRINKLERED}" for claim in result.claims
    )
    # And it is named in the unknowns section rather than left off the page.
    unknowns = " ".join(claim.text for claim in result.section("UNKNOWNS"))
    assert Keys.SUPPRESSION_SPRINKLERED in unknowns


async def test_a_resolved_fact_becomes_a_claim_carrying_its_fact_id(snapshot, parts) -> None:
    result = await brief(snapshot, parts)
    claim = next(c for c in result.claims if c.claim_id == f"fact.{Keys.LIGHTWEIGHT_TRUSS}")
    assert Keys.LIGHTWEIGHT_TRUSS in claim.refs
    assert snapshot.facts[Keys.LIGHTWEIGHT_TRUSS].fact_id in claim.refs


async def test_the_thermal_caveat_travels_with_the_brief(snapshot, parts) -> None:
    result = await brief(snapshot, parts)
    assert any(claim.text == THERMAL_CAVEAT for claim in result.section("CAVEATS"))


async def test_a_refused_path_is_reported_as_a_refusal_not_omitted(snapshot) -> None:
    """A brief about a building nobody could route through says so."""
    assessment = assess(
        incident_id="inc-1",
        snapshot=snapshot,
        coverage=coverage(),
        now=NOW,
        assessed_by="incident-interceptor",
    )
    refused = compute_entry_path(incident_id="inc-1", spec=None)
    result = await compose(
        brief_id="crewbrief-2",
        incident_id="inc-1",
        snapshot=snapshot,
        coverage=coverage(),
        assessment=assessment,
        plan=refused,
        now=NOW,
        composed_by="incident-interceptor",
    )
    route = result.section("ROUTE")
    assert len(route) == 1
    assert route[0].claim_id == "route.refused"
    assert "No route was computed" in route[0].text


# ---------------------------------------------------------------- the guard


def test_the_number_guard_compares_whole_tokens_only() -> None:
    """The ``1`` in a template id is not a numeric claim."""
    assert numbers_in("brief.crew.v1") == frozenset()
    assert numbers_in("16.29 m over 4 legs") == frozenset({"16.29", "4"})


@pytest.mark.invariant
async def test_a_composition_that_invents_a_number_is_refused_whole(snapshot, parts) -> None:
    """Not edited, not partially kept. The deterministic wording stands."""
    model = StubModel(prose("The building has 97 storeys and a clear Alpha side."))
    result = await brief(snapshot, parts, model=model)
    assert result.prose_source == "deterministic"
    assert result.prose_rejection == "NUMBER_NOT_IN_CLAIMS"
    assert "97" not in result.prose
    assert result.model_ref == "stub/1"


@pytest.mark.invariant
async def test_a_rounded_measurement_counts_as_an_invented_one(snapshot, parts) -> None:
    """42 C measured is not 40 C composed, and the guard does not round."""
    model = StubModel(prose("Every face was measured at 40 C."))
    result = await brief(snapshot, parts, model=model)
    assert result.prose_rejection == "NUMBER_NOT_IN_CLAIMS"


@pytest.mark.invariant
async def test_a_composition_containing_a_tactical_instruction_is_refused(snapshot, parts) -> None:
    model = StubModel(prose("Crews should go interior on the Alpha side."))
    result = await brief(snapshot, parts, model=model)
    assert result.prose_source == "deterministic"
    assert result.prose_rejection == "TACTICAL_LANGUAGE"


async def test_an_empty_composition_is_refused(snapshot, parts) -> None:
    result = await brief(snapshot, parts, model=StubModel(prose("   ")))
    assert result.prose_rejection == "EMPTY_COMPOSITION"


async def test_a_model_that_refuses_leaves_the_deterministic_brief(snapshot, parts) -> None:
    result = await brief(snapshot, parts, model=StubModel(prose("", accepted=False)))
    assert result.prose_source == "deterministic"
    assert result.prose_rejection == "MODEL_REFUSED"


async def test_a_model_that_times_out_leaves_the_deterministic_brief(snapshot, parts) -> None:
    result = await brief(
        snapshot, parts, model=StubModel(UpstreamTimeoutError("vertex did not answer"))
    )
    assert result.prose_source == "deterministic"
    assert result.prose_rejection == "UPSTREAM_TIMEOUT"


@pytest.mark.invariant
async def test_wording_that_stays_inside_the_claims_is_accepted(snapshot, parts) -> None:
    """The model may compose the sentences. The claims stay the claims."""
    model = StubModel(
        prose("Alpha, Bravo, Charlie and Delta all carry a current frame. Nothing is missing.")
    )
    result = await brief(snapshot, parts, model=model)
    assert result.prose_source == "model"
    assert result.prose_rejection == ""
    assert result.prose.startswith("Alpha, Bravo")
    # The claims and their refs are identical either way -- only the wording moved.
    deterministic = await brief(snapshot, parts)
    assert result.claims == deterministic.claims
    assert result.claim_refs == deterministic.claim_refs


@pytest.mark.invariant
async def test_the_model_is_handed_the_claims_and_nothing_else(snapshot, parts) -> None:
    """It cannot repeat a fact it was never shown."""
    model = StubModel(prose("Nothing to report beyond the claims."))
    await brief(snapshot, parts, model=model)
    assert model.calls
    fields = model.calls[0]["fields"]
    assert set(fields) == {"claims"}
    assert fields["claims"] == [
        claim.text
        for claim in build_claims(
            snapshot=snapshot, coverage=coverage(), assessment=parts[0], plan=parts[1]
        )
    ]


# ---------------------------------------------- the model may not cost the brief
#
# Everything above is about a model that says the wrong thing. These are about a
# model that says nothing at all, which is a different failure with a much worse
# blast radius: the composition runs inside a run the runtime cancels on a hard
# cap, so a client that hangs does not lose a paragraph of prose, it loses the
# entry package the prose was going inside.


@pytest.mark.invariant
async def test_a_model_that_never_answers_still_yields_a_brief(snapshot, parts) -> None:
    """The one that was fatal. Bounded here, not trusted to the client."""
    model = SilentModel()
    started = time.perf_counter()
    result = await asyncio.wait_for(
        brief(snapshot, parts, model=model, deadline_ms=600), timeout=10.0
    )
    elapsed = time.perf_counter() - started

    assert model.calls == 1
    assert result.prose_source == "deterministic"
    assert result.prose_rejection == "UPSTREAM_TIMEOUT"
    assert result.prose == render(
        build_claims(snapshot=snapshot, coverage=coverage(), assessment=parts[0], plan=parts[1])
    )
    # Inside its own budget, not merely finite: the caller reserved the rest of
    # the run for staging the package and that reserve has to still be there.
    assert elapsed < 3.0


async def test_the_client_is_given_less_than_the_bound_so_it_refuses_first(snapshot, parts) -> None:
    """Two deadlines, and the named one has to be the one that fires.

    A client reaching its own deadline raises ``UpstreamTimeoutError`` with a
    ``model_ref`` on it. The outer bound produces an anonymous cancellation. The
    grace is what keeps the second path theoretical.
    """
    model = SilentModel()
    await brief(snapshot, parts, model=model, deadline_ms=800)
    assert model.deadlines == [800 - CREW_BRIEF_TIMEOUT_GRACE_MS]


async def test_a_model_that_raises_anything_leaves_the_deterministic_brief(snapshot, parts) -> None:
    """Not just the timeout. Any vendor error is a paragraph, never a package."""
    result = await brief(snapshot, parts, model=StubModel(RuntimeError("transport reset")))
    assert result.prose_source == "deterministic"
    assert result.prose_rejection == "MODEL_UNAVAILABLE"


async def test_a_run_with_no_time_left_does_not_call_the_model_at_all(snapshot, parts) -> None:
    """Refused for time, and it says so rather than spending what is left."""
    model = SilentModel()
    result = await brief(snapshot, parts, model=model, deadline_ms=MIN_CREW_BRIEF_DEADLINE_MS - 1)
    assert model.calls == 0
    assert result.prose_source == "deterministic"
    assert result.prose_rejection == "NO_MODEL_BUDGET"


async def test_a_refusal_for_time_is_never_silent(snapshot, parts) -> None:
    """``prose_rejection`` empty means "no model was wired", and it is a claim."""
    unwired = await brief(snapshot, parts)
    assert unwired.prose_rejection == ""
    starved = await brief(snapshot, parts, model=SilentModel(), deadline_ms=10)
    assert starved.prose_rejection != ""


def test_the_acceptance_check_is_a_pure_function_of_text_and_claims() -> None:
    claims = build_claims  # referenced so the import is load-bearing
    assert claims is not None
    from firstdue.incident.crewbrief import BriefClaim

    corpus = (BriefClaim(claim_id="c", section="STRUCTURE", text="3 storeys, 42 C peak"),)
    assert accepts("Three storeys were measured; the peak was 42 C.", corpus) == ""
    assert accepts("Four storeys, 91 C.", corpus) == "NUMBER_NOT_IN_CLAIMS"
    assert accepts("Crews must attack from Alpha.", corpus) == "TACTICAL_LANGUAGE"
    assert accepts("", corpus) == "EMPTY_COMPOSITION"
