"""PHI derivation -- the only way person-level data reaches an officer.

An EMS record says who lives at an address, what happened to them, and when. A
fire officer arriving at 03:14 needs to know that **somebody on the second floor
may not be able to self-evacuate**. Those are different facts, and the gap
between them is this module.

The rule is structural, not procedural: **a raw EMS record never leaves the
adapter.** A derivation function reads it, returns a scoped life-safety fact,
and the raw record is not in the return value, not in the audit log, and not in
any exception message. There is no code path that returns the record itself,
because no function here returns one.

What a derivation may return: a mobility limitation, an approximate location
(floor, not unit), an age band (not a date of birth), where it came from, and
how confident it is. What it may never return: a name, an address unit, a
diagnosis, a date, or the record id.

Every derivation is a **named function**, and its name goes on the policy
decision. "The gateway derived something" is not auditable; "the gateway ran
``derive_ems_life_safety`` at policy version 1" is.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from firstdue.domain.enums import Classification
from firstdue.errors import ClassificationViolationError
from firstdue.observability.logging import get_logger

logger = get_logger(__name__)

#: Age is reported in bands. "84" identifies a person in a small building;
#: "over 75" tells a crew what they need without doing that.
AGE_BANDS: Final[tuple[tuple[int, str], ...]] = (
    (5, "under 5"),
    (18, "5 to 17"),
    (65, "18 to 64"),
    (75, "65 to 74"),
    (200, "over 75"),
)

#: Fields a derived fact may never carry, checked at construction. A guard, not
#: a convention: the list is what someone would reach for by accident.
FORBIDDEN_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "name",
        "patient_name",
        "first_name",
        "last_name",
        "dob",
        "date_of_birth",
        "unit",
        "apartment",
        "diagnosis",
        "medical_record_number",
        "mrn",
        "ssn",
        "phone",
        "email",
        "narrative",
        "record_id",
    }
)


def age_band(age_years: int | None) -> str | None:
    """Bucket an age, or return None when there is no age to report."""
    if age_years is None or age_years < 0:
        return None
    for ceiling, label in AGE_BANDS:
        if age_years < ceiling:
            return label
    return AGE_BANDS[-1][1]


class DerivedFact(BaseModel):
    """A scoped life-safety fact. Never a record, never a person.

    Construction fails if any forbidden field is present, so a derivation
    function that grew a field it should not have is a startup-time error in the
    test suite rather than a leak in production.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: What a crew can act on: "may not self-evacuate", "bariatric assist".
    life_safety_note: str = Field(min_length=1, max_length=200)
    #: Floor or face, never a unit number.
    approximate_location: str | None = Field(default=None, max_length=120)
    #: A band, never a date of birth.
    age_band: str | None = Field(default=None, max_length=40)
    #: Which derivation produced this, at which policy version.
    derivation_function: str = Field(min_length=1, max_length=120)
    policy_version: str = Field(min_length=1, max_length=40)
    #: Where it came from, as a source id -- never the record reference.
    source_id: str = Field(min_length=1, max_length=120)
    observed_at: datetime
    confidence: float = Field(ge=0.0, le=1.0)
    #: Derived facts are RESTRICTED, never PHI: the PHI stayed in the adapter.
    classification: Classification = Classification.RESTRICTED

    def model_post_init(self, __context: object) -> None:
        if self.classification is Classification.PHI:
            raise ClassificationViolationError(
                "a derived fact is not PHI; the PHI never left the adapter",
                details={"derivation_function": self.derivation_function},
            )
        haystack = " ".join(
            part.lower()
            for part in (
                self.life_safety_note,
                self.approximate_location or "",
                self.age_band or "",
            )
        )
        for field in ("dob", "ssn", "@"):
            if field in haystack:
                raise ClassificationViolationError(
                    "a derived fact carries something that identifies a person",
                    details={"derivation_function": self.derivation_function},
                )


def _hash_source(record_ref: str) -> str:
    """A stable, non-reversing handle for the record a derivation read.

    The audit log needs to say *which* record was derived from, so an
    investigator with lawful access can find it. It must not say what the record
    contained, or who it was about. A hash does both.
    """
    return hashlib.sha256(record_ref.encode("utf-8")).hexdigest()[:16]


#: Mobility signals, in the words EMS records actually use.
_IMMOBILE_TERMS: Final[tuple[str, ...]] = (
    "wheelchair",
    "bedbound",
    "bed-bound",
    "non-ambulatory",
    "nonambulatory",
    "hoyer",
    "bariatric",
    "ventilator",
    "oxygen dependent",
)
_ASSISTED_TERMS: Final[tuple[str, ...]] = ("walker", "cane", "crutches", "assisted", "mobility aid")


def derive_ems_life_safety(
    record: Mapping[str, Any], *, policy_version: str, source_id: str = "ems-derived"
) -> DerivedFact | None:
    """Derive one life-safety fact from one EMS record.

    Args:
        record: the raw record. It is read here and does not leave.
        policy_version: recorded on the derived fact for replay.
        source_id: which source it came from.

    Returns:
        A scoped fact, or ``None`` when the record supports no life-safety
        conclusion. ``None`` is the common case and the right one: most records
        say nothing a fire officer should be told.

    The raw record is never logged, never returned, and never included in an
    error. The audit line carries a hash of the record reference and the name of
    this function, which is what makes the derivation replayable without making
    it readable.
    """
    mobility = str(record.get("mobility") or record.get("mobility_status") or "").lower()
    equipment = " ".join(str(item).lower() for item in record.get("equipment", []) or [])
    haystack = f"{mobility} {equipment}"

    if any(term in haystack for term in _IMMOBILE_TERMS):
        note = "Occupant may not be able to self-evacuate"
        confidence = 0.85
    elif any(term in haystack for term in _ASSISTED_TERMS):
        note = "Occupant uses a mobility aid; evacuation assistance may be needed"
        confidence = 0.7
    else:
        # Nothing here a crew can act on. Returning None is the honest answer,
        # and it is why most EMS records produce no fact at all.
        logger.info(
            "phi_derivation_no_finding",
            extra={
                "derivation_function": "derive_ems_life_safety",
                "record_hash": _hash_source(str(record.get("record_ref", ""))),
            },
        )
        return None

    floor = record.get("floor")
    location = f"floor {floor}" if floor is not None else None
    observed_raw = record.get("observed_at")
    observed_at = (
        datetime.fromisoformat(str(observed_raw)) if observed_raw else datetime.now().astimezone()
    )

    derived = DerivedFact(
        life_safety_note=note,
        approximate_location=location,
        age_band=age_band(record.get("age_years")),
        derivation_function="derive_ems_life_safety",
        policy_version=policy_version,
        source_id=source_id,
        observed_at=observed_at,
        confidence=confidence,
    )

    # The audit line names the function and hashes the record. It carries no
    # name, no unit, no diagnosis, and no narrative.
    logger.info(
        "phi_derived",
        extra={
            "derivation_function": derived.derivation_function,
            "policy_version": policy_version,
            "record_hash": _hash_source(str(record.get("record_ref", ""))),
            "confidence": derived.confidence,
        },
    )
    return derived


#: The closed set of derivations the gateway may run. A ``DERIVE`` decision must
#: name one of these; an unnamed derivation is not a decision anybody can audit.
DERIVATIONS: Final[dict[str, Callable[..., DerivedFact | None]]] = {
    "derive_ems_life_safety": derive_ems_life_safety,
}


def derive_all(
    records: Sequence[Mapping[str, Any]], *, policy_version: str
) -> tuple[DerivedFact, ...]:
    """Derive over a set of records, dropping the ones that say nothing."""
    derived = (derive_ems_life_safety(record, policy_version=policy_version) for record in records)
    return tuple(fact for fact in derived if fact is not None)
