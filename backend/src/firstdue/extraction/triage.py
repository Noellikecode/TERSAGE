"""Document triage: is this worth a Gemini call?

Triage is the one place a cheap model is allowed to make a call, because it is
the one decision whose failure is safe in both directions:

* a wrong "skip this" costs one document nobody extracted from;
* a wrong "look at this" costs one model call.

Neither can put a wrong fact in front of an officer. That asymmetry is the
entire reason a small model is permitted here and nowhere else -- and it is why
every failure path in the live client answers **yes, extract**: when the cheap
model is unreachable, unsure, or malformed, the expensive one runs.

The local classifier below is the floor. In fake mode it *is* the triage; in
live mode it is what answers when Gemma cannot.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

from firstdue.domain.keys import Keys

#: Documents shorter than this are not worth a model call at all.
TRIAGE_MIN_CHARS: Final[int] = 40

#: The keys a narrative document can plausibly speak to.
NARRATIVE_KEYS: Final[tuple[str, ...]] = (
    Keys.STORIES,
    Keys.CONSTRUCTION_TYPE,
    Keys.YEAR_BUILT,
    Keys.LIGHTWEIGHT_TRUSS,
    Keys.SUPPRESSION_SPRINKLERED,
    Keys.EGRESS_OBSTRUCTION,
    Keys.HAZARD_SOLAR_ARRAY,
)

#: The vocabulary each structural key is actually written about in.
TRIAGE_SIGNALS: Final[dict[str, tuple[str, ...]]] = {
    Keys.STORIES: ("storey", "story", "stories", "floor"),
    Keys.CONSTRUCTION_TYPE: ("wood-frame", "wood frame", "ordinary", "timber", "type i"),
    Keys.YEAR_BUILT: ("built in",),
    Keys.LIGHTWEIGHT_TRUSS: ("truss",),
    Keys.SUPPRESSION_SPRINKLERED: ("sprinkler",),
    Keys.EGRESS_OBSTRUCTION: ("stairwell", "egress", "obstructed"),
    Keys.HAZARD_SOLAR_ARRAY: ("solar",),
}


@dataclass(slots=True)
class TriageDecision:
    """Whether a document is worth extracting from, and why."""

    extract: bool
    reason: str
    #: Keys the triage thought were plausibly present. Advisory only.
    candidate_keys: tuple[str, ...] = field(default_factory=tuple)


def triage(text: str, *, keys: Sequence[str] = NARRATIVE_KEYS) -> TriageDecision:
    """Cheap local classifier: is this document worth a Gemini call?

    It looks for the vocabulary the structural keys are actually described in.
    It can only ever *skip* work, and it never reads a value out of the text.
    """
    if len(text.strip()) < TRIAGE_MIN_CHARS:
        return TriageDecision(extract=False, reason="document too short to carry a structural fact")

    lowered = text.lower()
    present = tuple(
        key for key in keys if any(token in lowered for token in TRIAGE_SIGNALS.get(key, ()))
    )
    if not present:
        return TriageDecision(extract=False, reason="no structural vocabulary present")
    return TriageDecision(
        extract=True, reason="structural vocabulary present", candidate_keys=present
    )


__all__ = [
    "NARRATIVE_KEYS",
    "TRIAGE_MIN_CHARS",
    "TRIAGE_SIGNALS",
    "TriageDecision",
    "triage",
]
