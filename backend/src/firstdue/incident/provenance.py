"""Who, in the slow loop, produced the thing an incident agent just used.

The incident loop's cards each described their own work and stopped there. A
frame resolved to a wall, a route solved over a footprint, six criteria checked
against a profile -- and read down the stream, every one of them looked like an
agent that had arrived at a fire and worked it out on the spot. It had not. The
footprint was measured weeks earlier, the hazard attributes were filed off a
permit, and the disagreement about the storey count was found by a rule that ran
on a Tuesday. **The slow loop is why the incident loop has anything to read**,
and nothing on screen said so.

This module is the citation, and its whole discipline is that it never asserts
one. Every name it returns was read out of the record:
:attr:`~firstdue.domain.facts.StructuralFact.produced_by_agent`, written by the
agent that wrote the fact and carried on the profile snapshot the incident
opened against. Nothing here maps a canonical key to an agent by convention, and
nothing infers an author from a source type -- ``LIDAR_DSM`` looks like geometry
work and saying so would be this module deciding, not reporting.

**What is genuinely unattributed stays unattributed.** Three real cases, and
they are the reason :func:`credit` takes an ``otherwise`` clause rather than a
default name:

* the **footprint** on a :class:`~firstdue.domain.geometry.GeometrySpec` carries
  no fact id and no author, so a frame resolved against it can be said to have
  been resolved against the profile snapshot and cannot be said to have been
  resolved against anybody's work;
* a **conflict** records the deterministic ``rule_id`` that fired and no actor,
  so it is cited by its rule -- which is the more useful citation anyway, since
  the rule is the thing an officer can go and read;
* a fact written before ``produced_by_agent`` existed, or by a human survey,
  carries no agent and must not acquire one here.

A wrong attribution in an append-only record is worse than a missing one: the
missing one is answered by opening the profile, and the wrong one is believed.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from firstdue.domain.conflicts import Conflict
from firstdue.domain.facts import StructuralFact
from firstdue.domain.keys import GEOMETRY_INVALIDATING_KEYS, CanonicalKey
from firstdue.domain.profiles import ProfileSnapshot


def _distinct(names: Iterable[str]) -> tuple[str, ...]:
    """Sorted, deduplicated, and empties dropped.

    Sorted rather than in encounter order because these end up in an
    append-only record: two runs over the same snapshot have to produce the same
    sentence, and dict iteration order is not a promise a record may rest on.
    """
    return tuple(sorted({name for name in names if name}))


def authors_of(
    facts: Mapping[CanonicalKey, StructuralFact], keys: Sequence[CanonicalKey]
) -> tuple[str, ...]:
    """The agents that wrote the facts behind these attributes.

    Keys with no fact, and facts with no recorded author, contribute nothing --
    they are not an omission to be filled, they are the absence itself.
    """
    return _distinct(
        fact.produced_by_agent or "" for key in keys if (fact := facts.get(key)) is not None
    )


def authors_of_geometry(snapshot: ProfileSnapshot) -> tuple[str, ...]:
    """The agents whose facts the storeys in the massing model came from.

    Not "the agent that built the spec" -- the spec records no such thing. What
    it records is, per storey, the fact the storey was derived from, and *that*
    carries an author. So this answers a narrower question than it looks like it
    answers, and the narrower question is the one the record can support.

    A storey whose fact is not on the snapshot contributes nothing. That happens
    for real: a snapshot holds the active fact per attribute, and a DISPUTED
    storey is frequently the losing side of a disagreement.
    """
    spec = snapshot.geometry
    if spec is None:
        return ()
    by_id = {fact.fact_id: fact for fact in snapshot.facts.values()}
    return _distinct(
        (by_id[level.fact_id].produced_by_agent or "")
        for level in spec.levels
        if level.fact_id is not None and level.fact_id in by_id
    )


def structural_authors(snapshot: ProfileSnapshot) -> tuple[str, ...]:
    """The agents behind the attributes a massing model is extruded from.

    The wider answer, for when :func:`authors_of_geometry` finds nothing --
    which is the common case rather than the odd one. A snapshot carries the
    *active* fact per attribute, and a spec built while a disagreement was open
    was extruded from both sides of it, so its storeys routinely name fact ids
    the snapshot no longer holds.

    The keys are :data:`~firstdue.domain.keys.GEOMETRY_INVALIDATING_KEYS`,
    borrowed rather than listed again here. That set already *is* the system's
    recorded answer to "which attributes is the measured geometry a function
    of" -- it is what queues a re-measure when one of them moves -- so reading
    it is reading the record. Writing a second list beside it would be this
    module deciding what geometry depends on, and the two would drift.

    It is a weaker claim than the exact one and callers must phrase it as such:
    these agents filed the facts the geometry is a function of. They are not
    thereby the authors of the footprint, which records none.
    """
    return authors_of(snapshot.facts, sorted(GEOMETRY_INVALIDATING_KEYS))


def rules_behind(conflicts: Sequence[Conflict]) -> tuple[str, ...]:
    """The deterministic rules that found these disagreements.

    A :class:`~firstdue.domain.conflicts.Conflict` names a ``rule_id`` and no
    agent. The agent is on the profile's timeline, which the incident loop does
    not read -- the snapshot is the interface between the loops and reaching
    around it to decorate a card would be the wrong trade. The rule is the
    better citation regardless: an officer can go and read what it checks.
    """
    return _distinct(conflict.rule_id for conflict in conflicts)


def name(items: Sequence[str]) -> str:
    """One, two or many, as a person would write them. Empty for none."""
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def credit(authors: Sequence[str], *, work: str, otherwise: str) -> str:
    """``"<work> <authors>"``, or ``otherwise`` when nobody is recorded.

    The two halves are separate arguments on purpose. ``work`` says what was
    used and only makes sense with a name after it; ``otherwise`` is a whole
    clause that has to stand on its own, because the case it covers is "this
    input is real and its author is not on the record" -- which is a thing to
    say, not a thing to leave blank.
    """
    named = name(authors)
    return f"{work} {named}" if named else otherwise


__all__ = [
    "authors_of",
    "authors_of_geometry",
    "credit",
    "name",
    "rules_behind",
    "structural_authors",
]
