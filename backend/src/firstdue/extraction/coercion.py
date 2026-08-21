"""Raw extracted strings become typed values -- or they become nothing.

A model returns text. A fact needs a typed value, and the type is a property of
the attribute, not of what the model happened to write. So the mapping from
canonical key to value type is a table here, and a string that will not coerce
is dropped rather than stored as prose.

Dropping is the right failure. A storey count that arrived as "two or three"
is not an integer, and recording it as the text "two or three" would put a
value in front of an officer that no downstream rule can compare.
"""

from __future__ import annotations

import re
from typing import Final

from firstdue.domain.keys import Keys
from firstdue.domain.values import (
    BooleanValue,
    EnumValue,
    FactValue,
    IntegerValue,
    QuantityValue,
    TextValue,
)

INTEGER_KEYS: Final[frozenset[str]] = frozenset(
    {Keys.STORIES, Keys.YEAR_BUILT, Keys.STAIRWELL_COUNT, Keys.OCCUPANT_LOAD}
)

BOOLEAN_KEYS: Final[frozenset[str]] = frozenset(
    {
        Keys.LIGHTWEIGHT_TRUSS,
        Keys.SUPPRESSION_SPRINKLERED,
        Keys.SUPPRESSION_STANDPIPE,
        Keys.EGRESS_OBSTRUCTION,
        Keys.HAZARD_SOLAR_ARRAY,
        Keys.HAZARD_EV_CHARGER,
        Keys.HAZARD_TIER_II_PRESENT,
        Keys.UNPERMITTED_CONSTRUCTION,
        Keys.OPEN_VIOLATION,
    }
)

#: Keys whose value is a term from a named vocabulary.
ENUM_KEYS: Final[dict[str, str]] = {
    Keys.CONSTRUCTION_TYPE: "iso-construction",
    Keys.ROOF_TYPE: "roof-type",
    Keys.FLOOR_SYSTEM: "floor-system",
    Keys.OCCUPANCY_TYPE: "occupancy",
}

#: Keys whose value is a magnitude, with the unit it is always recorded in.
QUANTITY_KEYS: Final[dict[str, str]] = {
    Keys.HEIGHT_M: "m",
    Keys.FOOTPRINT_AREA_M2: "m2",
    Keys.HAZARD_PIPELINE_PROXIMITY_M: "m",
    Keys.THERMAL_FACE_C: "C",
    Keys.WEATHER_WIND_KPH: "kph",
}

_WORD_NUMBERS: Final[dict[str, int]] = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}

_TRUE_WORDS: Final[frozenset[str]] = frozenset(
    {"true", "yes", "y", "present", "1", "sprinklered", "obstructed", "installed"}
)
_FALSE_WORDS: Final[frozenset[str]] = frozenset({"false", "no", "n", "absent", "0", "none"})

_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")

#: Negation immediately before a matched phrase. "No sprinkler system on file"
#: contains the word "sprinkler", and an extractor that ignores the "no" asserts
#: the opposite of what the document says -- on an attribute a crew stakes an
#: interior attack on.
_NEGATION = re.compile(
    r"\b(no|not|without|none|never|lacks?|lacking|absent|removed)\b[^.;]{0,40}$",
    re.IGNORECASE,
)


#: A negation at the start of the matched phrase itself. "no hazardous
#: materials" carries its own negation, so looking only at what came before it
#: would read it as an assertion that hazardous materials are present.
_LEADING_NEGATION = re.compile(
    r"^\s*(no|not|none|without|zero)\b",
    re.IGNORECASE,
)


def is_negated(preceding_text: str, matched_text: str = "") -> bool:
    """Whether this match is negated, by what precedes it or by itself."""
    return bool(_NEGATION.search(preceding_text)) or bool(_LEADING_NEGATION.match(matched_text))


def value_type_for(canonical_key: str) -> str:
    """The value kind this attribute is always recorded as."""
    if canonical_key in INTEGER_KEYS:
        return "INTEGER"
    if canonical_key in BOOLEAN_KEYS:
        return "BOOLEAN"
    if canonical_key in ENUM_KEYS:
        return "ENUM"
    if canonical_key in QUANTITY_KEYS:
        return "QUANTITY"
    return "TEXT"


def coerce_value(canonical_key: str, raw: str, *, preceding_text: str = "") -> FactValue | None:
    """Coerce a raw extracted string into this attribute's type.

    Args:
        canonical_key: the attribute, which determines the type.
        raw: what the extractor read.
        preceding_text: the text immediately before the match, used to detect
            negation. A phrase match inside a negated sentence is dropped rather
            than asserted -- "no sprinkler system on file" must never become
            "sprinklered: yes", and asserting the *opposite* would be just as
            wrong, because the sentence is about the file rather than the
            building. Dropping leaves the attribute UNKNOWN, which is true.
            The matched text is checked for a leading negation as well, because
            a phrase like "no hazardous materials" carries its own.

    Returns ``None`` when it will not coerce, or when it was negated. The caller
    drops the candidate -- a value nothing downstream can compare, or one that
    inverts the document, is worse than an honest absence.
    """
    text = raw.strip()
    if not text:
        return None

    kind = value_type_for(canonical_key)

    if kind == "BOOLEAN" and is_negated(preceding_text, text):
        return None

    if kind == "INTEGER":
        word = _WORD_NUMBERS.get(text.lower())
        if word is not None:
            return IntegerValue(integer=word)
        match = _NUMBER.search(text)
        return IntegerValue(integer=int(float(match.group()))) if match else None

    if kind == "BOOLEAN":
        lowered = text.lower()
        if lowered in _FALSE_WORDS:
            return BooleanValue(boolean=False)
        if lowered in _TRUE_WORDS:
            return BooleanValue(boolean=True)
        # A phrase that names the thing is an assertion that it is there:
        # "lightweight parallel-chord truss" means the truss exists.
        return BooleanValue(boolean=True) if len(lowered) > 2 else None

    if kind == "QUANTITY":
        match = _NUMBER.search(text)
        if match is None:
            return None
        return QuantityValue(magnitude=float(match.group()), unit=QUANTITY_KEYS[canonical_key])

    if kind == "ENUM":
        term = re.sub(r"\s+", "-", text.lower())[:80]
        return EnumValue(term=term, vocabulary=ENUM_KEYS[canonical_key])

    return TextValue(text=text[:2000])
