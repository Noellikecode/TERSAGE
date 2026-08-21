"""Deterministic merge: which fact wins when several describe the same attribute.

The rule the fire service cares about, stated once and enforced in one place:
**memory never outranks live observation.** A remembered attribute loses to
tonight's thermal pass, always -- tier is compared before recency and before
confidence, and no amount of decayed confidence can promote a filed record above
something a crew just measured.

Losing facts are *not* deleted. This function chooses what to display; the
:class:`~firstdue.domain.factsets.FactSet` keeps every fact.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from pydantic import BaseModel, ConfigDict, Field

from firstdue.domain.enums import TIER_RANK, SourceTier
from firstdue.domain.facts import StructuralFact


class MergeTrace(BaseModel):
    """Why the winner won. Surfaced in the UI reasoning trace; cites fact ids."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    winning_fact_id: str
    rule: str = Field(description="which clause of the merge order decided it")
    considered_fact_ids: tuple[str, ...]
    losing_fact_ids: tuple[str, ...]


def _order_key(fact: StructuralFact) -> tuple[int, float, float, str]:
    """Ascending sort key. Tier first, then recency, then confidence, then id.

    ``fact_id`` is the final tie-break purely so the result is deterministic
    across processes and replays.
    """
    return (
        TIER_RANK[fact.tier],
        -fact.observed_at.timestamp(),
        -fact.confidence,
        fact.fact_id,
    )


def resolve_facts(
    facts: Iterable[StructuralFact],
) -> tuple[StructuralFact | None, MergeTrace | None]:
    """Choose the fact that represents an attribute right now.

    Order of decision:

    1. Superseded facts are excluded (a correction within one lineage).
    2. If any live observation exists, the most recent live observation wins --
       even if it is ``UNSCANNED`` or ``UNAVAILABLE``. Coverage that lapsed must
       show as lapsed rather than silently reverting to a stale reading.
    3. Otherwise a known value beats an absent one, so a source outage does not
       erase a filed record.
    4. Otherwise: tier, then recency, then confidence, then ``fact_id``.
    """
    active = [f for f in facts if f.is_active]
    if not active:
        return None, None

    considered = tuple(f.fact_id for f in sorted(active, key=_order_key))

    live = [f for f in active if f.tier is SourceTier.LIVE_OBSERVATION]
    if live:
        winner = sorted(live, key=_order_key)[0]
        rule = "live-observation-outranks-memory"
    else:
        known = [f for f in active if f.is_known]
        pool = known if known else active
        rule = "known-beats-absent" if known else "tier-then-recency"
        winner = sorted(pool, key=_order_key)[0]
        if known and len(known) == len(active):
            rule = "tier-then-recency"

    trace = MergeTrace(
        winning_fact_id=winner.fact_id,
        rule=rule,
        considered_fact_ids=considered,
        losing_fact_ids=tuple(fid for fid in considered if fid != winner.fact_id),
    )
    return winner, trace


def order_facts(facts: Sequence[StructuralFact]) -> list[StructuralFact]:
    """Return facts in deterministic merge order (winner first)."""
    return sorted(facts, key=_order_key)
