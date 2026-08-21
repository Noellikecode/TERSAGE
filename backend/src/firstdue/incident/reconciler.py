"""The reconciler: three stages, and only one of them can be slow.

**Instant.** A read of one stored snapshot and a render. No model call -- not an
optional one, not a fast one: the emission model refuses to construct an instant
stage with ``model_invoked=True``. If Vertex AI is down, if the network is down,
if every source is down, stage one still lands, because none of them are on its
path. Budget is 500 ms locally, and exceeding it is a defect rather than a slow
day.

**Enriched.** Optional Gemini prose in COAL WAS WEALTH order -- the size-up
sequence a fire officer already reads in. The model composes from resolved
fields and invents nothing; a rejected or unavailable response leaves the
deterministic brief exactly as it was and says the narrative is unavailable.

**Live.** Amendments as late data arrives: EMS-derived life-safety facts, NWS
weather, thermal observations, IC resolutions. Every amendment is marked as one,
and **late data never delays earlier output** -- stage one is already on the
commander's screen before stage two is asked for.

What no stage does is recommend anything. There is no tactical language in any
template here, and a test asserts it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from firstdue.domain.briefs import BriefEmission, BriefItem, BriefSection, BriefSectionKey
from firstdue.domain.conflicts import Conflict
from firstdue.domain.enums import AssertionStatus, BriefStage
from firstdue.domain.keys import CanonicalKey, Keys
from firstdue.domain.profiles import ProfileSnapshot
from firstdue.errors import UpstreamTimeoutError
from firstdue.gateway.derivation import DerivedFact
from firstdue.incident.fusion import FaceCoverage, VoidObservation
from firstdue.incident.timer import MaterialTimeWindow
from firstdue.observability.logging import get_logger
from firstdue.ports.clock import Clock, IdGenerator
from firstdue.ports.model import ModelClient

logger = get_logger(__name__)

#: Attributes the instant brief always reports, present or not. A key with no
#: fact is printed as UNKNOWN rather than left off the screen.
INSTANT_KEYS: Final[tuple[tuple[BriefSectionKey, tuple[CanonicalKey, ...]], ...]] = (
    (
        BriefSectionKey.CONSTRUCTION,
        (Keys.CONSTRUCTION_TYPE, Keys.STORIES, Keys.HEIGHT_M, Keys.YEAR_BUILT),
    ),
    (BriefSectionKey.OCCUPANCY, (Keys.OCCUPANCY_TYPE, Keys.OCCUPANT_LOAD)),
    (
        BriefSectionKey.AUXILIARY_APPLIANCES,
        (Keys.SUPPRESSION_SPRINKLERED, Keys.SUPPRESSION_STANDPIPE, Keys.FDC_LOCATION),
    ),
    (BriefSectionKey.LIFE_HAZARD, (Keys.EGRESS_OBSTRUCTION, Keys.STAIRWELL_COUNT)),
    (
        BriefSectionKey.HAZARDS,
        (
            Keys.LIGHTWEIGHT_TRUSS,
            Keys.HAZARD_TIER_II_PRESENT,
            Keys.HAZARD_SOLAR_ARRAY,
            Keys.HAZARD_EV_CHARGER,
        ),
    ),
)

#: COAL WAS WEALTH. The order an officer's size-up already runs in, so the brief
#: does not ask them to learn a new one under load.
COAL_WAS_WEALTH: Final[tuple[BriefSectionKey, ...]] = (
    BriefSectionKey.CONSTRUCTION,
    BriefSectionKey.OCCUPANCY,
    BriefSectionKey.APPARATUS,
    BriefSectionKey.LIFE_HAZARD,
    BriefSectionKey.WATER_SUPPLY,
    BriefSectionKey.AUXILIARY_APPLIANCES,
    BriefSectionKey.STREET_CONDITIONS,
    BriefSectionKey.WEATHER,
    BriefSectionKey.EXPOSURES,
    BriefSectionKey.AREA,
    BriefSectionKey.LOCATION_EXTENT,
    BriefSectionKey.TIME,
    BriefSectionKey.HEIGHT,
    BriefSectionKey.HAZARDS,
    BriefSectionKey.CONFLICTS,
)

NARRATIVE_MAX_CHARS: Final[int] = 2_000
NARRATIVE_DEADLINE_MS: Final[int] = 4_000
AGENT_ID: Final[str] = "incident-controller"


def _derived_render(fact: DerivedFact) -> str:
    """One line for a derived life-safety fact. Never a record, never a person."""
    parts = [fact.life_safety_note]
    if fact.approximate_location:
        parts.append(fact.approximate_location)
    if fact.age_band:
        parts.append(f"age {fact.age_band}")
    return ", ".join(parts)[:500]


def _order(sections: Sequence[BriefSection]) -> tuple[BriefSection, ...]:
    """Sort sections into size-up order, keeping anything unlisted at the end."""
    rank = {key: index for index, key in enumerate(COAL_WAS_WEALTH)}
    return tuple(sorted(sections, key=lambda s: rank.get(s.key, len(rank))))


class Reconciler:
    """Builds the three brief stages from one snapshot."""

    def __init__(
        self,
        *,
        clock: Clock,
        ids: IdGenerator,
        model: ModelClient | None = None,
        agent_version: str = "1.0.0",
    ) -> None:
        self._clock = clock
        self._ids = ids
        self._model = model
        self._agent_version = agent_version

    # ------------------------------------------------------------ stage one

    def instant(
        self,
        snapshot: ProfileSnapshot,
        *,
        incident_id: str,
        collapse_zone_m: float | None = None,
        truss_window: MaterialTimeWindow | None = None,
    ) -> BriefEmission:
        """The model-free brief. One read, one render, no network.

        Everything on it came out of the snapshot. A cold profile -- no facts,
        no geometry -- produces a brief that says the structural attributes are
        unknown, which is the honest output and not an error.
        """
        sections: list[BriefSection] = []
        unknowns: list[CanonicalKey] = []
        disputed = {c.canonical_key for c in snapshot.conflicts}

        for section_key, keys in INSTANT_KEYS:
            items: list[BriefItem] = []
            for key in keys:
                fact = snapshot.facts.get(key)
                if fact is None:
                    unknowns.append(key)
                    items.append(
                        BriefItem(
                            label=key,
                            value_render="UNKNOWN - no record found",
                            status=AssertionStatus.UNKNOWN,
                            canonical_key=key,
                        )
                    )
                    continue
                if not fact.value.is_known:
                    unknowns.append(key)
                items.append(
                    BriefItem(
                        label=key,
                        value_render=fact.value.render(),
                        status=(
                            AssertionStatus.DISPUTED
                            if key in disputed
                            else AssertionStatus.CONFIRMED
                            if fact.value.is_known
                            else AssertionStatus.UNKNOWN
                        ),
                        canonical_key=key,
                        fact_id=fact.fact_id,
                        provenance=fact.source_type,
                    )
                )
            sections.append(BriefSection(key=section_key, items=tuple(items)))

        if collapse_zone_m is not None:
            sections.append(
                BriefSection(
                    key=BriefSectionKey.AREA,
                    items=(
                        BriefItem(
                            label="collapse zone",
                            # The standard 1.5x-height convention applied to a
                            # measured height. States a geometric standard;
                            # predicts nothing about this fire.
                            value_render=f"{collapse_zone_m:g} m (1.5x measured height)",
                            status=AssertionStatus.CONFIRMED,
                        ),
                    ),
                )
            )

        if truss_window is not None:
            sections.append(
                BriefSection(
                    key=BriefSectionKey.TIME,
                    items=(
                        BriefItem(
                            label="lightweight truss time window",
                            value_render=truss_window.render[:500],
                            status=AssertionStatus.CONFIRMED,
                            canonical_key=truss_window.canonical_key,
                            fact_id=truss_window.fact_id,
                        ),
                    ),
                )
            )

        sections.append(self._conflict_section(snapshot.conflicts))

        return BriefEmission(
            emission_id=self._ids.new_id("emission"),
            incident_id=incident_id,
            version=1,
            stage=BriefStage.INSTANT,
            sections=_order(sections),
            unknowns=tuple(sorted(set(unknowns))),
            conflict_ids=tuple(c.conflict_id for c in snapshot.conflicts),
            # Absent by construction: the model refuses to be invoked here.
            narrative=None,
            narrative_available=False,
            model_invoked=False,
            profile_snapshot_id=snapshot.snapshot_id,
            agent_versions={AGENT_ID: self._agent_version},
            produced_at=self._clock.now(),
        ).sealed()

    @staticmethod
    def _conflict_section(conflicts: Sequence[Conflict]) -> BriefSection:
        """Disagreements, stated as disagreements.

        A brief that resolved them would be hiding the one thing an officer can
        act on: two sources disagree, and only the person standing there can
        settle it.
        """
        return BriefSection(
            key=BriefSectionKey.CONFLICTS,
            items=tuple(
                BriefItem(
                    label=f"{c.canonical_key} (severity {c.severity})",
                    value_render=c.summary,
                    status=AssertionStatus.DISPUTED,
                    canonical_key=c.canonical_key,
                )
                for c in sorted(conflicts, key=lambda c: (-c.severity, c.conflict_id))
            ),
        )

    # ------------------------------------------------------------ stage two

    async def enriched(self, previous: BriefEmission, snapshot: ProfileSnapshot) -> BriefEmission:
        """Add model-composed prose, or say plainly that it is unavailable.

        The deterministic sections are carried over untouched. The model is
        asked to compose from fields that are already resolved -- it invents
        nothing, and if it returns something the contract rejects, the brief
        lands without prose rather than with prose nobody checked.
        """
        narrative: str | None = None
        available = False
        invoked = False

        if self._model is not None:
            invoked = True
            try:
                result = await self._model.compose(
                    template_id="brief.enriched.v1",
                    fields=self._prose_fields(previous, snapshot),
                    max_chars=NARRATIVE_MAX_CHARS,
                    deadline_ms=NARRATIVE_DEADLINE_MS,
                )
            except UpstreamTimeoutError:
                logger.warning(
                    "brief_narrative_unavailable",
                    extra={"incident_id": previous.incident_id, "reason": "UPSTREAM_TIMEOUT"},
                )
            else:
                if result.accepted and result.text.strip():
                    narrative = result.text[:NARRATIVE_MAX_CHARS]
                    available = True
                else:
                    logger.warning(
                        "model_output_rejected",
                        extra={
                            "incident_id": previous.incident_id,
                            "model_ref": result.model_ref,
                        },
                    )

        return BriefEmission(
            emission_id=self._ids.new_id("emission"),
            incident_id=previous.incident_id,
            version=previous.version + 1,
            stage=BriefStage.ENRICHED,
            sections=previous.sections,
            unknowns=previous.unknowns,
            unavailable=previous.unavailable,
            withheld=previous.withheld,
            conflict_ids=previous.conflict_ids,
            narrative=narrative,
            narrative_available=available,
            model_invoked=invoked,
            profile_snapshot_id=previous.profile_snapshot_id,
            agent_versions=dict(previous.agent_versions),
            produced_at=self._clock.now(),
        ).sealed()

    @staticmethod
    def _prose_fields(emission: BriefEmission, snapshot: ProfileSnapshot) -> dict[str, object]:
        """Already-resolved fields. The model composes; it does not decide."""
        return {
            "address_id": snapshot.address_id,
            "sections": [
                {
                    "key": str(section.key),
                    "items": [
                        {
                            "label": item.label,
                            "value": item.value_render,
                            "status": str(item.status),
                        }
                        for item in section.items
                    ],
                }
                for section in emission.sections
            ],
            "unknowns": list(emission.unknowns),
            "conflicts": [c.summary for c in snapshot.conflicts],
        }

    # ---------------------------------------------------------- stage three

    def amendment(
        self,
        previous: BriefEmission,
        *,
        derived_facts: Sequence[DerivedFact] = (),
        weather: Sequence[BriefItem] = (),
        thermal: Sequence[FaceCoverage] = (),
        voids: Sequence[VoidObservation] = (),
        resolutions: Sequence[str] = (),
        unavailable: Sequence[str] = (),
    ) -> BriefEmission:
        """One amendment carrying whatever arrived late.

        Marked as an amendment in the stage itself, so a commander can see that
        something changed rather than re-reading the whole brief to find it.
        Nothing here can delay stage one or stage two: they are already
        transmitted by the time this is built.
        """
        sections = list(previous.sections)

        if derived_facts:
            sections.append(
                BriefSection(
                    key=BriefSectionKey.LIFE_HAZARD,
                    items=tuple(
                        BriefItem(
                            label="life safety (derived)",
                            value_render=(
                                f"{fact.life_safety_note}"
                                + (
                                    f", {fact.approximate_location}"
                                    if fact.approximate_location
                                    else ""
                                )
                                + (f", age {fact.age_band}" if fact.age_band else "")
                            ),
                            status=AssertionStatus.CONFIRMED,
                            # The gateway derived this; the record never left
                            # the adapter, and the brief says so.
                            derivation_note=(
                                f"derived by {fact.derivation_function} at policy "
                                f"{fact.policy_version}; the underlying record was not released"
                            ),
                        )
                        for fact in derived_facts
                    ),
                )
            )

        if weather:
            sections.append(BriefSection(key=BriefSectionKey.WEATHER, items=tuple(weather)))

        if thermal:
            sections.append(
                BriefSection(
                    key=BriefSectionKey.LOCATION_EXTENT,
                    items=tuple(
                        BriefItem(
                            label=f"face {report.face}",
                            value_render=report.render[:500],
                            status=(
                                AssertionStatus.CONFIRMED
                                if report.scanned
                                else AssertionStatus.UNKNOWN
                            ),
                        )
                        for report in thermal
                    )
                    + tuple(
                        BriefItem(
                            label=f"thermal delta {observation.face}",
                            value_render=observation.render[:500],
                            status=AssertionStatus.CONFIRMED,
                        )
                        for observation in voids
                    ),
                )
            )

        if resolutions:
            sections.append(
                BriefSection(
                    key=BriefSectionKey.CONFLICTS,
                    items=tuple(
                        BriefItem(
                            label="settled on scene",
                            value_render=text[:500],
                            status=AssertionStatus.CONFIRMED,
                        )
                        for text in resolutions
                    ),
                )
            )

        return BriefEmission(
            emission_id=self._ids.new_id("emission"),
            incident_id=previous.incident_id,
            version=previous.version + 1,
            stage=BriefStage.AMENDMENT,
            sections=_order(sections),
            unknowns=previous.unknowns,
            unavailable=tuple(sorted({*previous.unavailable, *unavailable})),
            withheld=previous.withheld,
            conflict_ids=previous.conflict_ids,
            narrative=previous.narrative,
            narrative_available=previous.narrative_available,
            model_invoked=previous.model_invoked,
            profile_snapshot_id=previous.profile_snapshot_id,
            agent_versions=dict(previous.agent_versions),
            produced_at=self._clock.now(),
        ).sealed()


def degraded_note(unavailable: Sequence[str]) -> BriefItem:
    """The line a degraded brief carries.

    Named sources, so an officer knows *what* is missing rather than that
    something is. A brief with a gap it does not mention is worse than no brief.
    """
    return BriefItem(
        label="degraded",
        value_render=(
            "UNAVAILABLE - " + ", ".join(sorted(unavailable))
            if unavailable
            else "UNAVAILABLE - source unreachable"
        ),
        status=AssertionStatus.UNKNOWN,
    )
