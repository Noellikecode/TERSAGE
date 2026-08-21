"""Typed fact values, with absence as a first-class citizen.

The central rule of this module: **there is no way to express "we did not check"
that looks like "there is nothing there."** ``UNKNOWN``, ``UNAVAILABLE``,
``WITHHELD`` and ``UNSCANNED`` are distinct inhabited types, none of which is
``None``, ``False``, or a missing field.

``StructuralFact.value`` is a required field of this union, so "missing" cannot
occur at all: a fact without a value cannot be constructed.

``is_known`` is a read-only property rather than a field, so no caller can
construct an absent value that claims to be known.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from firstdue.errors import ValidationError

#: Kinds that represent absence of an observed value.
ABSENT_KINDS: frozenset[str] = frozenset({"UNKNOWN", "UNAVAILABLE", "WITHHELD", "UNSCANNED"})


class _ValueBase(BaseModel):
    """Common behaviour for every value variant."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    @property
    def is_known(self) -> bool:
        """Whether this variant carries an observed value at all."""
        return False

    def unwrap(self) -> Any:
        """Return the underlying Python value.

        Raises:
            ValidationError: for every absent variant. Callers that want a
                default must choose one explicitly -- there is no silent
                coercion of absence into a value.
        """
        raise ValidationError(
            f"{type(self).__name__} carries no value; absence must be handled explicitly",
            details={"kind": str(getattr(self, "kind", "UNKNOWN"))},
        )

    def render(self) -> str:
        """Operator-facing single-line rendering."""
        raise NotImplementedError  # pragma: no cover - every variant overrides


class _KnownBase(_ValueBase):
    @property
    def is_known(self) -> bool:
        return True


# --------------------------------------------------------------- known values


class TextValue(_KnownBase):
    kind: Literal["TEXT"] = "TEXT"
    text: str = Field(min_length=1, max_length=2000)

    def unwrap(self) -> str:
        return self.text

    def render(self) -> str:
        return self.text


class IntegerValue(_KnownBase):
    kind: Literal["INTEGER"] = "INTEGER"
    integer: int

    def unwrap(self) -> int:
        return self.integer

    def render(self) -> str:
        return str(self.integer)


class QuantityValue(_KnownBase):
    """A magnitude that is meaningless without its unit, so the unit is required."""

    kind: Literal["QUANTITY"] = "QUANTITY"
    magnitude: float
    unit: str = Field(min_length=1, max_length=16)

    def unwrap(self) -> float:
        return self.magnitude

    def render(self) -> str:
        return f"{self.magnitude:g} {self.unit}"


class BooleanValue(_KnownBase):
    """A *checked* boolean.

    ``BooleanValue(boolean=False)`` means "we looked and it is not so".
    "We did not look" is :class:`UnknownValue` and is a different type.
    """

    kind: Literal["BOOLEAN"] = "BOOLEAN"
    boolean: bool

    def unwrap(self) -> bool:
        return self.boolean

    def render(self) -> str:
        return "yes" if self.boolean else "no"


class EnumValue(_KnownBase):
    """A term drawn from a named vocabulary (e.g. ISO construction types)."""

    kind: Literal["ENUM"] = "ENUM"
    term: str = Field(min_length=1, max_length=80)
    vocabulary: str = Field(min_length=1, max_length=80)

    def unwrap(self) -> str:
        return self.term

    def render(self) -> str:
        return self.term


# -------------------------------------------------------------- absent values


class UnknownValue(_ValueBase):
    """No record found. Distinct from "nothing is there"."""

    kind: Literal["UNKNOWN"] = "UNKNOWN"
    #: Sources that were consulted and had nothing -- shown as "could not check".
    checked_sources: tuple[str, ...] = ()

    def render(self) -> str:
        return "UNKNOWN"


class UnavailableValue(_ValueBase):
    """A source could not be reached, or its circuit breaker is open.

    Never collapses to :class:`UnknownValue`: "the hazmat database is down" and
    "the hazmat database has no filing" are different operational facts.
    """

    kind: Literal["UNAVAILABLE"] = "UNAVAILABLE"
    source_id: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=200)

    def render(self) -> str:
        return f"UNAVAILABLE - {self.source_id}"


class WithheldValue(_ValueBase):
    """Statutorily or jurisdictionally withheld. The requester learns it exists."""

    kind: Literal["WITHHELD"] = "WITHHELD"
    rule_id: str = Field(min_length=1, max_length=120)
    authority: str = Field(min_length=1, max_length=200)

    def render(self) -> str:
        return f"WITHHELD - {self.authority}"


class UnscannedValue(_ValueBase):
    """A surface exists but has no sensor coverage. Never rendered as cool."""

    kind: Literal["UNSCANNED"] = "UNSCANNED"
    surface: str | None = Field(default=None, max_length=80)

    def render(self) -> str:
        return "UNSCANNED"


KnownValueT: TypeAlias = TextValue | IntegerValue | QuantityValue | BooleanValue | EnumValue
AbsentValueT: TypeAlias = UnknownValue | UnavailableValue | WithheldValue | UnscannedValue

FactValue = Annotated[
    TextValue
    | IntegerValue
    | QuantityValue
    | BooleanValue
    | EnumValue
    | UnknownValue
    | UnavailableValue
    | WithheldValue
    | UnscannedValue,
    Field(discriminator="kind"),
]


def is_absent(value: _ValueBase) -> bool:
    """True when the value is one of the four explicit absence states."""
    return not value.is_known
