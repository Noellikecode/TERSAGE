"""The deterministic conflict engine.

Disagreement is signal. When the permit says two storeys and the lidar measures
three, the system surfaces the conflict rather than averaging or picking a
winner -- because unpermitted construction is itself a structural risk, and the
average of two and three describes no building that has ever existed.

Three properties hold for everything in this module:

* **It is pure.** No clock, no repository, no model, no randomness. ``now``
  arrives as an argument. The same facts produce the same conflicts forever,
  which is what a replay two years later requires.
* **No model participates.** A model may narrate a conflict after the fact
  (:meth:`~firstdue.domain.conflicts.Conflict.narrate`); it can neither create
  one nor change its severity. A model that could invent a conflict could also
  invent its absence.
* **Conflict ids are derived, not minted.** The id is a hash of the rule, the
  address, the attribute, and the participating fact ids. Re-running the engine
  over unchanged facts therefore produces the *same* conflict id, which is what
  makes persistence idempotent and replay equivalent instead of merely similar.

The registry is open: :class:`RuleRegistry` accepts new rules without this module
changing. What is closed is the *shape* of a rule -- a rule must cite its
``rule_id`` and every fact it rests on, so the console can show an officer
exactly which two documents disagree.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from firstdue.domain.conflicts import Conflict, ConflictResolution, ConflictStatus
from firstdue.domain.enums import SourceTier, SourceType
from firstdue.domain.facts import StructuralFact
from firstdue.domain.keys import CanonicalKey, Keys
from firstdue.domain.work import SurveyRecord
from firstdue.errors import ValidationError

CONFLICT_ID_PREFIX: Final[str] = "conflict"
_DIGEST_LENGTH: Final[int] = 24

#: Attributes whose disagreement is a life-safety matter, not a paperwork one.
LIFE_SAFETY_KEYS: Final[frozenset[str]] = frozenset(
    {
        Keys.LIGHTWEIGHT_TRUSS,
        Keys.SUPPRESSION_SPRINKLERED,
        Keys.SUPPRESSION_STANDPIPE,
        Keys.EGRESS_OBSTRUCTION,
        Keys.STAIRWELL_COUNT,
        Keys.HAZARD_TIER_II_PRESENT,
        Keys.OCCUPANCY_TYPE,
    }
)


def conflict_id_for(rule_id: str, address_id: str, key: str, fact_ids: Iterable[str]) -> str:
    """The id a given finding always produces.

    Derived from the finding's identity rather than from a counter, so detecting
    the same disagreement twice yields one conflict record, not two.
    """
    material = f"{rule_id}|{address_id}|{key}|{'+'.join(sorted(fact_ids))}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:_DIGEST_LENGTH]
    return f"{CONFLICT_ID_PREFIX}_{digest}"


# --------------------------------------------------------------- comparison


def _comparable(fact: StructuralFact) -> object:
    """Normalise a fact's value for equality across sources.

    Booleans are compared as booleans, numbers as floats, and everything else
    as case-folded text, so ``EnumValue("Wood-Frame")`` and
    ``TextValue("wood-frame")`` are not reported as a disagreement about
    capitalisation.
    """
    raw = fact.value.unwrap()
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int | float):
        return float(raw)
    return str(raw).strip().casefold()


def values_disagree(left: StructuralFact, right: StructuralFact) -> bool:
    """Whether two *known* facts assert different things.

    Values of incomparable shape count as disagreeing. An assessor saying
    ``"three"`` where lidar says ``3.0`` is exactly the kind of mismatch an
    officer should see rather than have quietly reconciled.
    """
    if not (left.is_known and right.is_known):
        return False
    return _comparable(left) != _comparable(right)


# ------------------------------------------------------------------ context


@dataclass(frozen=True, slots=True)
class RuleContext:
    """Everything a rule may look at. Deliberately small."""

    address_id: str
    now: datetime
    #: Active (non-superseded) facts, grouped by attribute, in deterministic order.
    facts_by_key: Mapping[CanonicalKey, tuple[StructuralFact, ...]]

    def active(self, key: str) -> tuple[StructuralFact, ...]:
        return self.facts_by_key.get(key, ())

    def known(self, key: str) -> tuple[StructuralFact, ...]:
        return tuple(f for f in self.active(key) if f.is_known)

    def of_source(self, key: str, *source_types: SourceType) -> tuple[StructuralFact, ...]:
        wanted = frozenset(source_types)
        return tuple(f for f in self.known(key) if f.source_type in wanted)

    def of_tier(self, key: str, *tiers: SourceTier) -> tuple[StructuralFact, ...]:
        wanted = frozenset(tiers)
        return tuple(f for f in self.known(key) if f.tier in wanted)

    @property
    def keys(self) -> tuple[CanonicalKey, ...]:
        return tuple(sorted(self.facts_by_key))


def context_from_facts(
    address_id: str, facts: Iterable[StructuralFact], *, now: datetime
) -> RuleContext:
    """Build a context from a flat fact iterable.

    Facts are sorted by ``(observed_at, fact_id)`` so rule output does not depend
    on the order a repository happened to return them in.
    """
    grouped: dict[CanonicalKey, list[StructuralFact]] = {}
    for fact in facts:
        if not fact.is_active:
            continue
        if fact.address_id != address_id:
            raise ValidationError(
                "conflict detection received a fact for a different address",
                details={"fact_id": fact.fact_id, "address_id": address_id},
            )
        grouped.setdefault(fact.canonical_key, []).append(fact)
    return RuleContext(
        address_id=address_id,
        now=now,
        facts_by_key={
            key: tuple(sorted(items, key=lambda f: (f.observed_at, f.fact_id)))
            for key, items in grouped.items()
        },
    )


# ----------------------------------------------------------------- findings


class ConflictFinding(BaseModel):
    """One rule's deterministic output, before it becomes a stored record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule_id: str = Field(min_length=1, max_length=120)
    address_id: str = Field(min_length=1, max_length=120)
    canonical_key: CanonicalKey
    severity: int = Field(ge=1, le=5)
    #: Every fact that participates. Two or more, and all of them stay stored.
    fact_ids: tuple[str, ...] = Field(min_length=2)
    #: Deterministic template text. Present with or without a model.
    summary: str = Field(min_length=1, max_length=500)

    @property
    def conflict_id(self) -> str:
        return conflict_id_for(self.rule_id, self.address_id, self.canonical_key, self.fact_ids)

    def to_conflict(self, *, detected_at: datetime) -> Conflict:
        return Conflict(
            conflict_id=self.conflict_id,
            address_id=self.address_id,
            canonical_key=self.canonical_key,
            rule_id=self.rule_id,
            severity=self.severity,
            fact_ids=self.fact_ids,
            summary=self.summary,
            detected_at=detected_at,
        )


def _finding(
    *,
    rule_id: str,
    context: RuleContext,
    key: str,
    severity: int,
    facts: Sequence[StructuralFact],
    summary: str,
) -> ConflictFinding:
    return ConflictFinding(
        rule_id=rule_id,
        address_id=context.address_id,
        canonical_key=key,
        severity=severity,
        fact_ids=tuple(sorted(f.fact_id for f in facts)),
        summary=summary,
    )


# -------------------------------------------------------------------- rules


@runtime_checkable
class ConflictRule(Protocol):
    """A deterministic detector for one class of disagreement."""

    @property
    def rule_id(self) -> str:
        """Stable identifier, cited on every conflict this rule produces."""
        ...

    def evaluate(self, context: RuleContext) -> Sequence[ConflictFinding]:
        """Return every finding for this address. Must be pure and total."""
        ...


class PermitVersusLidarStoryCount:
    """The rule the product is built around.

    A filed permit describes what was *authorised*; a lidar DSM measures what is
    *there*. When they disagree the difference is very often an unpermitted
    storey -- which means an unengineered floor, an unknown load path, and a
    crew operating above a structure nobody signed off. The engine reports the
    disagreement and preserves both facts; it never picks the taller one.
    """

    rule_id: Final[str] = "permit-vs-lidar-story-count"

    def evaluate(self, context: RuleContext) -> Sequence[ConflictFinding]:
        permits = context.of_source(Keys.STORIES, SourceType.PERMIT)
        measured = context.of_source(Keys.STORIES, SourceType.LIDAR_DSM)
        if not permits or not measured:
            return ()

        # Most recently observed on each side: an amended permit and a fresh
        # flight are the pair an officer would compare.
        permit = max(permits, key=lambda f: (f.observed_at, f.fact_id))
        lidar = max(measured, key=lambda f: (f.observed_at, f.fact_id))
        if not values_disagree(permit, lidar):
            return ()

        permit_stories = permit.value.unwrap()
        lidar_stories = lidar.value.unwrap()
        try:
            difference = abs(float(lidar_stories) - float(permit_stories))
        except (TypeError, ValueError):
            difference = 1.0
        severity = 5 if difference >= 2 else 4

        return (
            _finding(
                rule_id=self.rule_id,
                context=context,
                key=Keys.STORIES,
                severity=severity,
                facts=(permit, lidar),
                summary=(
                    f"Permit records {permit.value.render()} storeys; "
                    f"lidar DSM measures {lidar.value.render()}."
                ),
            ),
        )


class AuthoritativeSourceDisagreement:
    """Two filed municipal records that contradict each other.

    Scoped to the authoritative tier exactly, so it neither duplicates the
    permit-versus-lidar rule (lidar is a remote measurement) nor the survey rule
    (a survey is human-verified). Severity rises for attributes a crew acts on
    inside the building.
    """

    rule_id: Final[str] = "authoritative-source-disagreement"

    def evaluate(self, context: RuleContext) -> Sequence[ConflictFinding]:
        findings: list[ConflictFinding] = []
        for key in context.keys:
            records = context.of_tier(key, SourceTier.AUTHORITATIVE_RECORD)
            if len(records) < 2:
                continue
            baseline = records[-1]
            disagreeing = [f for f in records if values_disagree(baseline, f)]
            if not disagreeing:
                continue
            participants = [baseline, *disagreeing]
            rendered = sorted({f.value.render() for f in participants})
            findings.append(
                _finding(
                    rule_id=self.rule_id,
                    context=context,
                    key=key,
                    severity=3 if key in LIFE_SAFETY_KEYS else 2,
                    facts=participants,
                    summary=(
                        f"Filed records disagree on {key}: {' vs '.join(rendered)}. "
                        "Both records are retained."
                    ),
                )
            )
        return tuple(findings)


class SurveyContradictsRecord:
    """A crew stood in the building and saw something the file does not say.

    The survey wins the *display* (see :mod:`firstdue.domain.merge`), but the
    disagreement is still recorded: a filed record that no longer describes the
    structure is a referral waiting to be written.
    """

    rule_id: Final[str] = "survey-contradicts-record"

    def evaluate(self, context: RuleContext) -> Sequence[ConflictFinding]:
        findings: list[ConflictFinding] = []
        for key in context.keys:
            surveys = context.of_tier(key, SourceTier.HUMAN_VERIFIED)
            records = context.of_tier(
                key, SourceTier.AUTHORITATIVE_RECORD, SourceTier.REMOTE_MEASUREMENT
            )
            if not surveys or not records:
                continue
            survey = max(surveys, key=lambda f: (f.observed_at, f.fact_id))
            disagreeing = [f for f in records if values_disagree(survey, f)]
            if not disagreeing:
                continue
            findings.append(
                _finding(
                    rule_id=self.rule_id,
                    context=context,
                    key=key,
                    severity=4 if key in LIFE_SAFETY_KEYS else 3,
                    facts=[survey, *disagreeing],
                    summary=(
                        f"Company survey observed {survey.value.render()} for {key}; "
                        f"the filed record says "
                        f"{' / '.join(sorted({f.value.render() for f in disagreeing}))}."
                    ),
                )
            )
        return tuple(findings)


# ----------------------------------------------------------------- registry


class RuleRegistry:
    """The open set of rules the engine runs.

    Order is by ``rule_id`` rather than registration order, so two processes that
    registered the same rules in a different sequence still produce byte-identical
    output.
    """

    def __init__(self, rules: Iterable[ConflictRule] = ()) -> None:
        self._rules: dict[str, ConflictRule] = {}
        for rule in rules:
            self.register(rule)

    def register(self, rule: ConflictRule) -> None:
        """Add a rule. Re-registering the same id is an error, not a silent swap."""
        if rule.rule_id in self._rules:
            raise ValidationError(
                "a conflict rule with this id is already registered",
                details={"rule_id": rule.rule_id},
            )
        self._rules[rule.rule_id] = rule

    def replace(self, rule: ConflictRule) -> None:
        """Deliberately swap a rule -- used when a rule is versioned up."""
        self._rules[rule.rule_id] = rule

    def get(self, rule_id: str) -> ConflictRule | None:
        return self._rules.get(rule_id)

    @property
    def rule_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._rules))

    @property
    def rules(self) -> tuple[ConflictRule, ...]:
        return tuple(self._rules[rule_id] for rule_id in self.rule_ids)

    def evaluate(self, context: RuleContext) -> tuple[ConflictFinding, ...]:
        """Run every rule. Output is ordered by severity, then rule, then key."""
        findings: list[ConflictFinding] = []
        for rule in self.rules:
            findings.extend(rule.evaluate(context))
        return tuple(
            sorted(findings, key=lambda f: (-f.severity, f.rule_id, f.canonical_key, f.fact_ids))
        )


def default_registry() -> RuleRegistry:
    """The rules every FIRST DUE process runs unless a test says otherwise."""
    return RuleRegistry(
        (
            PermitVersusLidarStoryCount(),
            AuthoritativeSourceDisagreement(),
            SurveyContradictsRecord(),
        )
    )


#: Process-wide default. Tests build their own rather than mutating this one.
DEFAULT_REGISTRY: Final[RuleRegistry] = default_registry()


# ---------------------------------------------------------------- detection


def detect(
    address_id: str,
    facts: Iterable[StructuralFact],
    *,
    now: datetime,
    registry: RuleRegistry | None = None,
) -> tuple[ConflictFinding, ...]:
    """Every disagreement among ``facts``, deterministically ordered."""
    context = context_from_facts(address_id, facts, now=now)
    return (registry or DEFAULT_REGISTRY).evaluate(context)


def new_conflicts(
    findings: Sequence[ConflictFinding],
    existing: Iterable[Conflict],
    *,
    detected_at: datetime,
) -> tuple[Conflict, ...]:
    """Findings not already recorded, as storable conflicts.

    Because ids are derived, "already recorded" is an exact test rather than a
    heuristic -- which is what stops a re-poll from filing the same
    disagreement twice.
    """
    known = {conflict.conflict_id for conflict in existing}
    return tuple(
        finding.to_conflict(detected_at=detected_at)
        for finding in findings
        if finding.conflict_id not in known
    )


# --------------------------------------------------------- human resolution


def survey_resolutions(
    conflicts: Iterable[Conflict],
    survey: SurveyRecord,
    *,
    resolving_fact_ids: Mapping[CanonicalKey, str],
) -> tuple[tuple[str, ConflictResolution], ...]:
    """Resolve open conflicts the crew actually settled.

    **Only a human observation closes a conflict.** Not a newer filing, not a
    higher confidence score, not a model: someone stood in the building, looked
    at the attribute, and wrote it down. This function pairs each open conflict
    on an attribute the survey verified with the resolution that closes it.

    Attributes the survey did not verify are left open, and both original facts
    remain stored either way -- the resolution records what settled the
    disagreement, it does not erase that there was one.
    """
    if survey.outcome.name == "NO_ACCESS":
        return ()

    verified = frozenset(survey.verified_keys)
    resolutions: list[tuple[str, ConflictResolution]] = []
    for conflict in sorted(conflicts, key=lambda c: c.conflict_id):
        if conflict.status is not ConflictStatus.OPEN:
            continue
        if conflict.address_id != survey.address_id:
            continue
        if conflict.canonical_key not in verified:
            continue
        resolving_fact_id = resolving_fact_ids.get(conflict.canonical_key)
        if resolving_fact_id is None:
            # The crew verified the attribute but no fact was written for it.
            # Leaving the conflict open is the honest outcome.
            continue
        resolutions.append(
            (
                conflict.conflict_id,
                ConflictResolution(
                    resolved_at=survey.completed_at,
                    resolving_record_id=survey.survey_id,
                    resolving_fact_id=resolving_fact_id,
                    resolved_by=survey.surveyor,
                    note=f"Resolved by company survey {survey.company}.",
                ),
            )
        )
    return tuple(resolutions)
