"""UNKNOWN is a value, not an absence.

These tests exist because the failure they prevent is fatal: a system that
renders "not checked" as "safe" tells a crew a building is clear when nobody
looked.
"""

from __future__ import annotations

import pytest

from firstdue.domain.values import (
    BooleanValue,
    IntegerValue,
    QuantityValue,
    TextValue,
    UnavailableValue,
    UnknownValue,
    UnscannedValue,
    WithheldValue,
    is_absent,
)
from firstdue.errors import ValidationError

pytestmark = pytest.mark.invariant


def test_unknown_is_not_false() -> None:
    assert UnknownValue() != BooleanValue(boolean=False)


def test_unknown_is_not_none() -> None:
    assert UnknownValue() is not None


def test_unknown_is_not_empty_text() -> None:
    with pytest.raises(Exception):  # noqa: B017 - empty text is simply not constructible
        TextValue(text="")


def test_absence_states_are_distinct_types() -> None:
    absents = [
        UnknownValue(),
        UnavailableValue(source_id="tier-ii", reason="circuit open"),
        WithheldValue(rule_id="epcra-311", authority="Tier II confidential filing"),
        UnscannedValue(surface="BRAVO"),
    ]
    # No two absence states compare equal: "we could not reach the hazmat
    # database" is a different fact from "the filing is statutorily withheld".
    for i, a in enumerate(absents):
        for j, b in enumerate(absents):
            if i != j:
                assert a != b


@pytest.mark.parametrize(
    "value",
    [
        UnknownValue(),
        UnavailableValue(source_id="s", reason="r"),
        WithheldValue(rule_id="r", authority="a"),
        UnscannedValue(),
    ],
)
def test_absent_values_refuse_to_unwrap(value: object) -> None:
    assert is_absent(value)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        value.unwrap()  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (IntegerValue(integer=3), 3),
        (BooleanValue(boolean=False), False),
        (QuantityValue(magnitude=10.5, unit="m"), 10.5),
        (TextValue(text="ordinary"), "ordinary"),
    ],
)
def test_known_values_unwrap(value: object, expected: object) -> None:
    assert value.is_known is True  # type: ignore[attr-defined]
    assert value.unwrap() == expected  # type: ignore[attr-defined]


def test_is_known_cannot_be_forged() -> None:
    """`is_known` is a property, so no constructor can claim an absent value
    is known."""
    with pytest.raises(Exception):  # noqa: B017 - extra="forbid"
        UnknownValue(is_known=True)  # type: ignore[call-arg]


def test_values_are_frozen() -> None:
    value = IntegerValue(integer=2)
    with pytest.raises(Exception):  # noqa: B017 - pydantic frozen model
        value.integer = 3  # type: ignore[misc]


def test_absent_values_render_their_state() -> None:
    assert UnknownValue().render() == "UNKNOWN"
    assert "UNAVAILABLE" in UnavailableValue(source_id="epa", reason="timeout").render()
    assert "WITHHELD" in WithheldValue(rule_id="r", authority="EPCRA").render()
    assert UnscannedValue().render() == "UNSCANNED"
