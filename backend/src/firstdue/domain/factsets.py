"""A set of facts asserting the same attribute of the same address.

Conflicting facts both remain stored. This container is append-only: there is no
method that removes or rewrites a fact, only one that adds another.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from firstdue.domain.enums import AssertionStatus
from firstdue.domain.facts import StructuralFact
from firstdue.domain.keys import CanonicalKey
from firstdue.domain.merge import MergeTrace, resolve_facts
from firstdue.errors import AppendOnlyViolationError, ValidationError


class FactSet(BaseModel):
    """Every fact ever written about one ``(address_id, canonical_key)`` pair."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    address_id: str = Field(min_length=1, max_length=120)
    canonical_key: CanonicalKey
    facts: tuple[StructuralFact, ...] = Field(min_length=1)

    def append(self, fact: StructuralFact) -> FactSet:
        """Return a new set with ``fact`` added. Nothing is ever removed.

        Raises:
            ValidationError: if the fact describes a different address or key.
            AppendOnlyViolationError: if the fact id is already present.
        """
        if fact.address_id != self.address_id or fact.canonical_key != self.canonical_key:
            raise ValidationError(
                "fact does not belong to this fact set",
                details={"canonical_key": self.canonical_key, "address_id": self.address_id},
            )
        if any(existing.fact_id == fact.fact_id for existing in self.facts):
            raise AppendOnlyViolationError(
                "fact_id already present in this fact set",
                details={"fact_id": fact.fact_id},
            )
        return self.model_copy(update={"facts": (*self.facts, fact)})

    @property
    def active(self) -> tuple[StructuralFact, ...]:
        return tuple(f for f in self.facts if f.is_active)

    @property
    def resolved(self) -> StructuralFact | None:
        """The fact that currently represents this attribute."""
        winner, _ = resolve_facts(self.facts)
        return winner

    @property
    def merge_trace(self) -> MergeTrace | None:
        _, trace = resolve_facts(self.facts)
        return trace

    @property
    def local_status(self) -> AssertionStatus:
        """Agreement status derived from this set alone.

        The authoritative ``DISPUTED`` marking comes from the deterministic
        conflict engine, which sees rules this container does not. This property
        is the local view used for rendering when no conflict record exists yet.
        """
        winner = self.resolved
        if winner is None or not winner.is_known:
            return AssertionStatus.UNKNOWN
        distinct = {f.value for f in self.active if f.is_known}
        if len(distinct) > 1:
            return AssertionStatus.DISPUTED
        return AssertionStatus.CONFIRMED

    @classmethod
    def of(cls, fact: StructuralFact) -> FactSet:
        return cls(
            address_id=fact.address_id,
            canonical_key=fact.canonical_key,
            facts=(fact,),
        )
