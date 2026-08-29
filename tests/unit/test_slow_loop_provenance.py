"""Citing the slow loop, and refusing to cite it when the record does not.

The incident loop's cards described their own work and never said where the
work's inputs came from, so a stream of them read like agents deciding things
on the spot. These tests are the correction and, more importantly, the limit on
it: every name that reaches a card has to have been read off the record, and an
input whose author nobody wrote down has to stay unattributed.

That second half is the one worth testing hardest. A card that names the wrong
agent is worse than a card that names none -- the missing name is answered by
opening the profile, and the wrong one is believed and filed.
"""

from __future__ import annotations

from datetime import UTC, datetime

from firstdue.domain.conflicts import Conflict, ConflictStatus
from firstdue.domain.enums import AssertionStatus, Classification, SourceType
from firstdue.domain.facts import StructuralFact
from firstdue.domain.geometry import GeometrySpec, Level
from firstdue.domain.keys import GEOMETRY_INVALIDATING_KEYS, Keys
from firstdue.domain.profiles import ProfileSnapshot
from firstdue.domain.values import QuantityValue
from firstdue.incident.provenance import (
    authors_of,
    authors_of_geometry,
    credit,
    name,
    rules_behind,
    structural_authors,
)

NOW = datetime(2026, 8, 20, 8, 0, 0, tzinfo=UTC)
ADDRESS = "sf-0450-hayes"


def fact(fact_id: str, key: str, *, author: str | None, value: float = 3.0) -> StructuralFact:
    return StructuralFact(
        fact_id=fact_id,
        address_id=ADDRESS,
        canonical_key=key,
        value=QuantityValue(magnitude=value, unit="m"),
        source_type=SourceType.PERMIT,
        source_ref="permit/2019-0042",
        source_snapshot_id="snap-src-1",
        observed_at=NOW,
        ingested_at=NOW,
        confidence=0.9,
        classification=Classification.PUBLIC,
        produced_by_agent=author,
    )


def snapshot(
    *, facts: dict[str, StructuralFact], levels: tuple[Level, ...] = ()
) -> ProfileSnapshot:
    return ProfileSnapshot(
        address_id=ADDRESS,
        district_id="sffd-district-03",
        profile_version=7,
        snapshot_id="snap-1",
        read_at=NOW,
        facts=facts,
        geometry=(
            GeometrySpec(
                address_id=ADDRESS,
                generated_at=NOW,
                footprint=((0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)),
                levels=levels,
                collapse_zone_radius_m=15.0,
            )
            if levels
            else None
        ),
    )


def level(fact_id: str | None) -> Level:
    return Level(
        height_m=3.2,
        provenance=SourceType.PERMIT,
        status=AssertionStatus.CONFIRMED,
        fact_id=fact_id,
    )


# ------------------------------------------------------ what is on the record


def test_an_author_is_reported_only_when_the_fact_records_one() -> None:
    facts = {
        Keys.STORIES: fact("f1", Keys.STORIES, author="records-watcher"),
        Keys.HEIGHT_M: fact("f2", Keys.HEIGHT_M, author=None),
    }
    # The one that names an agent is cited; the one that does not contributes
    # nothing rather than contributing a guess.
    assert authors_of(facts, [Keys.STORIES, Keys.HEIGHT_M]) == ("records-watcher",)


def test_an_attribute_with_no_fact_at_all_contributes_nothing() -> None:
    facts = {Keys.STORIES: fact("f1", Keys.STORIES, author="records-watcher")}
    assert authors_of(facts, [Keys.HEIGHT_M, Keys.ROOF_TYPE]) == ()


def test_authors_are_sorted_and_deduplicated() -> None:
    """Two runs over one snapshot have to write the same sentence.

    These end up in an append-only record. A sentence whose word order came
    from dict iteration is a sentence that can differ between two readings of
    the same data, which is exactly what a record must not do.
    """
    facts = {
        Keys.STORIES: fact("f1", Keys.STORIES, author="records-watcher"),
        Keys.HEIGHT_M: fact("f2", Keys.HEIGHT_M, author="geometry-watcher"),
        Keys.ROOF_TYPE: fact("f3", Keys.ROOF_TYPE, author="records-watcher"),
    }
    keys = [Keys.STORIES, Keys.HEIGHT_M, Keys.ROOF_TYPE]
    assert authors_of(facts, keys) == ("geometry-watcher", "records-watcher")
    assert authors_of(facts, list(reversed(keys))) == authors_of(facts, keys)


def test_a_storey_derived_from_a_carried_fact_names_that_facts_author() -> None:
    """The exact link: this storey came from this fact, which this agent wrote."""
    one = fact("f1", Keys.STORIES, author="geometry-watcher")
    assert authors_of_geometry(snapshot(facts={Keys.STORIES: one}, levels=(level("f1"),))) == (
        "geometry-watcher",
    )


def test_a_storey_pointing_at_a_fact_the_snapshot_dropped_names_nobody() -> None:
    """The ordinary case, and the reason the weaker claim exists.

    A snapshot holds the *active* fact per attribute. A spec extruded while a
    disagreement was open was built from both sides of it, so its storeys
    routinely cite fact ids that lost and are no longer carried. There is no
    author to be had here and none is invented.
    """
    active = fact("f-winner", Keys.STORIES, author="records-watcher")
    dropped = snapshot(facts={Keys.STORIES: active}, levels=(level("f-loser"),))
    assert authors_of_geometry(dropped) == ()
    # And the weaker claim does have something to say about the same snapshot,
    # which is the whole point of having two.
    assert structural_authors(dropped) == ("records-watcher",)


def test_a_storey_with_no_fact_id_names_nobody() -> None:
    assert authors_of_geometry(snapshot(facts={}, levels=(level(None),))) == ()


def test_an_address_with_no_geometry_names_nobody() -> None:
    facts = {Keys.STORIES: fact("f1", Keys.STORIES, author="records-watcher")}
    assert authors_of_geometry(snapshot(facts=facts)) == ()


def test_the_weaker_claim_reads_the_keys_geometry_is_a_function_of() -> None:
    """Borrowed from the domain, not listed again beside it.

    ``GEOMETRY_INVALIDATING_KEYS`` already *is* this system's recorded answer to
    which attributes measured geometry depends on -- it is what queues a
    re-measure. A second list here would be this module deciding, and the two
    would drift.
    """
    facts = {
        key: fact(f"f-{key}", key, author=f"agent-{key}") for key in GEOMETRY_INVALIDATING_KEYS
    }
    facts[Keys.YEAR_BUILT] = fact("f-unrelated", Keys.YEAR_BUILT, author="records-watcher")
    cited = structural_authors(snapshot(facts=facts))
    assert set(cited) == {f"agent-{key}" for key in GEOMETRY_INVALIDATING_KEYS}
    # An attribute geometry is not a function of is not evidence about geometry.
    assert "records-watcher" not in cited


def test_a_conflict_is_cited_by_its_rule_because_it_records_no_actor() -> None:
    """The honest citation, and the more useful one.

    ``Conflict`` carries the deterministic ``rule_id`` that fired and no agent
    at all. The agent is on the profile timeline, which the incident loop does
    not read. So the card names the rule -- which an officer can go and read --
    rather than the agent it would have had to guess at.
    """
    disagreement = Conflict(
        conflict_id="conflict_1",
        address_id=ADDRESS,
        canonical_key=Keys.STORIES,
        rule_id="permit-vs-lidar-story-count",
        severity=5,
        fact_ids=("f1", "f2"),
        summary="the permit and the lidar disagree about the storey count",
        detected_at=NOW,
        status=ConflictStatus.OPEN,
    )
    assert rules_behind([disagreement]) == ("permit-vs-lidar-story-count",)


# ------------------------------------------------------- how it reads on a card


def test_names_read_the_way_a_person_writes_them() -> None:
    assert name([]) == ""
    assert name(["records-watcher"]) == "records-watcher"
    assert name(["records-watcher", "geometry-watcher"]) == "records-watcher and geometry-watcher"
    assert name(["a", "b", "c"]) == "a, b and c"


def test_an_unattributed_input_is_described_and_not_credited() -> None:
    """The rule this whole module exists to keep.

    ``otherwise`` is a whole clause rather than a blank, because "this input is
    real and nobody recorded who produced it" is a thing to say. What it must
    never be is a name.
    """
    said = credit((), work="filed by", otherwise="this snapshot names no author")
    assert said == "this snapshot names no author"
    assert "filed by" not in said


def test_a_credit_never_contains_a_name_it_was_not_given() -> None:
    said = credit(["geometry-watcher"], work="filed by", otherwise="unrecorded")
    assert said == "filed by geometry-watcher"
    assert "records-watcher" not in said
    assert "unrecorded" not in said
