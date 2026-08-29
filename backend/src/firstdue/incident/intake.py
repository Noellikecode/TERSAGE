"""Reading the call: what a 911 caller or a CAD dispatcher actually said.

A fire emergency does not arrive as fields. It arrives as a person on a phone,
transcribed, or as a dispatcher's free-text narrative. Until now the incident
loop took only the columns CAD happened to put in the envelope -- an address, a
reference, an alarm level -- and everything the caller said about entrapment,
about the smell of gas, about the gate being chained, reached the commander only
by radio if it reached them at all.

This module is the one place that prose becomes structure, and it is bounded on
every side because of what section 6 forbids.

**The model extracts; it never asserts.** Gemini is asked one question -- what
does this text say about six named attributes -- against a closed schema, and
every value it returns must cite the character offsets it read it at. The quote
is re-checked against the source text here, because a model that was *asked* for
a span is not the same as a model that returned a true one. A value whose quote
does not match the text it claims to come from is dropped.

**Rejection is a value.** No path here raises. A screen that is down, a model
that is down, a response that is malformed, a narrative that is empty: all of
them produce ``accepted=False`` and an intake that reported nothing. The instant
brief is already on the commander's screen by the time any of this runs, so the
worst outcome is that it stays as it was.

**Reported is not observed, and the distinction is structural.** A caller report
never becomes a :class:`~firstdue.domain.facts.StructuralFact`. Not a low-tier
one, not a decayed one -- none at all. Giving it a tier would not be enough:
:func:`~firstdue.domain.merge.resolve_facts` has a "known beats absent" clause,
so on a cold-start building a caller's guess would be the *only* fact for a key
and would stand on the brief as the system's answer. The only version of this
rule that holds everywhere is that the call does not produce facts. What it
produces is :class:`ReportedItem`, which reaches the brief through
:func:`reported_sections` and nowhere else, always carrying the note that says a
caller said it and always short of ``CONFIRMED``.

**Nothing here is tactical.** The intake reports what was said. It does not
infer fire behaviour from a floor number, does not decide whether an occupancy
is tenable, and does not rank anything.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, ConfigDict, Field

from firstdue.domain.briefs import BriefItem, BriefSection, BriefSectionKey
from firstdue.domain.enums import AssertionStatus
from firstdue.domain.facts import SourceSpan
from firstdue.domain.keys import INTAKE_KEYS, CanonicalKey, IntakeKeys, Keys
from firstdue.domain.profiles import ProfileSnapshot
from firstdue.observability.logging import get_logger
from firstdue.ports.model import ExtractionResult, ModelClient

if TYPE_CHECKING:  # pragma: no cover - a type, not a dependency
    # Imported for the annotation only. `security.armor` reaches
    # `extraction.screening`, whose package init reaches back into
    # `security.armor`; importing it at runtime here makes the incident
    # package the first module in that loop and the loop then fails.
    from firstdue.security.armor import DocumentScreen

logger = get_logger(__name__)


class IntakeChannel(StrEnum):
    """How the narrative reached us.

    Recorded on every reported item, because "the caller said" and "the
    dispatcher wrote" are different claims with different failure modes, and a
    commander reading the brief is entitled to know which one they are looking
    at.
    """

    CALL_911 = "CALL_911"
    CAD_NARRATIVE = "CAD_NARRATIVE"


#: How each channel is named on screen. Prose, because the brief is read under
#: load and ``CAD_NARRATIVE`` is not a phrase anybody says out loud.
CHANNEL_LABEL: Final[dict[IntakeChannel, str]] = {
    IntakeChannel.CALL_911: "911 caller",
    IntakeChannel.CAD_NARRATIVE: "CAD dispatch narrative",
}


#: The filed attribute a reported item speaks to, where one exists. Used only to
#: print the filed value *next to* the reported one -- never to overwrite it.
#: Most intake keys have no entry here on purpose: a floor of origin and an
#: alarm level are facts about tonight, not about the building.
MIRRORS: Final[dict[str, CanonicalKey]] = {
    IntakeKeys.REPORTED_OCCUPANCY: Keys.OCCUPANCY_TYPE,
    IntakeKeys.HAZMAT_REPORTED: Keys.HAZARD_TIER_II_PRESENT,
}

#: Which part of the size-up each reported attribute belongs under. The intake
#: does not get a section of its own: a commander reads COAL WAS WEALTH, and
#: burying the reported entrapment in an "intake" block at the bottom would be
#: hiding it from the sequence they actually read in.
SECTION_FOR: Final[dict[str, BriefSectionKey]] = {
    IntakeKeys.REPORTED_OCCUPANCY: BriefSectionKey.OCCUPANCY,
    IntakeKeys.ENTRAPMENT_REPORTED: BriefSectionKey.LIFE_HAZARD,
    IntakeKeys.HAZMAT_REPORTED: BriefSectionKey.HAZARDS,
    IntakeKeys.REPORTED_FLOOR_OF_ORIGIN: BriefSectionKey.LOCATION_EXTENT,
    IntakeKeys.REPORTED_ALARM_LEVEL: BriefSectionKey.TIME,
    IntakeKeys.ACCESS_NOTE: BriefSectionKey.STREET_CONDITIONS,
}

#: How each attribute is labelled on the brief line.
LABEL_FOR: Final[dict[str, str]] = {
    IntakeKeys.REPORTED_OCCUPANCY: "occupancy (reported)",
    IntakeKeys.ENTRAPMENT_REPORTED: "entrapment (reported)",
    IntakeKeys.HAZMAT_REPORTED: "hazardous material (reported)",
    IntakeKeys.REPORTED_FLOOR_OF_ORIGIN: "floor of origin (reported)",
    IntakeKeys.REPORTED_ALARM_LEVEL: "alarm level (reported)",
    IntakeKeys.ACCESS_NOTE: "access (reported)",
}

#: Longest narrative read. A transcript beyond this is truncated rather than
#: refused: the opening of a 911 call is where the emergency is stated.
MAX_NARRATIVE_CHARS: Final[int] = 20_000

#: Longest reported value kept. Quoted caller speech, not a document.
MAX_REPORTED_CHARS: Final[int] = 300

#: The intake's own budget. It is **not** the instant brief's 500 ms, because
#: the intake is not on the instant path -- stage one is persisted and streamed
#: before this is asked for, and a slow read here costs an amendment arriving
#: later rather than a brief arriving later.
#:
#: **Strictly inside the run that wraps it**, which is what the second is for.
#: This was 6 s against the ``incident-interceptor`` descriptor's 6 s latency
#: target, so a model that spent its whole deadline and *then* refused -- the
#: good failure, the one this module turns into a stated ``accepted=False``
#: reading -- lost the race to the runtime's own timeout by microseconds. The
#: run was cancelled instead, and everything downstream of the extraction went
#: with it: the reading was never recorded, no amendment landed, and the
#: routing table never fired, so **no agent was woken on that incident at all**.
#: A second of reserve buys the screen, the record, the amendment, the routing
#: and the wakes, and turns the worst case back into an intake nobody could
#: read on an incident whose fleet is running.
INTAKE_DEADLINE_MS: Final[int] = 5_000

#: Alarm levels the department runs. A reported level outside this is recorded
#: as text and contributes no signal, because there is no such alarm.
MAX_ALARM_LEVEL: Final[int] = 5

#: Storeys a reported floor of origin may name before it stops being a number
#: anybody said. Bounds the parse, nothing else.
MAX_REPORTED_FLOOR: Final[int] = 200

#: Ordinals a caller actually says. A closed table rather than a parser, so the
#: mapping from speech to integer is something a reviewer can read in full.
_ORDINALS: Final[dict[str, int]] = {
    "first": 1,
    "ground": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
    "eleventh": 11,
    "twelfth": 12,
}

_DIGITS: Final[re.Pattern[str]] = re.compile(r"\d{1,3}")


class ReportedItem(BaseModel):
    """One thing the narrative said, bound to where it said it.

    Not a fact and not convertible into one. The span is required for the same
    reason it is required on an extraction: a value a human cannot trace back to
    the line it came from is a claim, and a claim a commander cannot check
    against the transcript is worse than silence.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    intake_key: str = Field(min_length=1, max_length=120)
    #: What the narrative said, quoted and bounded. The caller's words, never
    #: the system's -- which is why it is always rendered behind "REPORTED".
    raw_value: str = Field(min_length=1, max_length=MAX_REPORTED_CHARS)
    span: SourceSpan
    channel: IntakeChannel
    #: The transcript or narrative this was read out of.
    source_ref: str = Field(min_length=1, max_length=200)
    #: The model's own confidence in its reading. Recorded for the audit trail
    #: and read by nothing: routing and rendering are both blind to it, because
    #: a model that could raise its own confidence could raise its own
    #: influence. See :func:`~firstdue.incident.handoff.plan_handoffs`.
    model_confidence: float = Field(ge=0.0, le=1.0)

    @property
    def provenance(self) -> str:
        """The line that says who said this and what that is worth."""
        return (
            f"reported by the {CHANNEL_LABEL[self.channel]} ({self.source_ref}); "
            "a caller report, not a filed record and not a measurement"
        )


class IntakeReading(BaseModel):
    """Everything one narrative produced, including the ways it produced nothing.

    ``accepted=False`` is an ordinary outcome and never an exception. The three
    ways it happens -- no screen, no model, a response the contract refused --
    are all survivable, because stage one of the brief was already persisted and
    transmitted before this ran.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    incident_id: str = Field(min_length=1, max_length=120)
    channel: IntakeChannel
    source_ref: str = Field(min_length=1, max_length=200)

    #: Sorted by ``(intake_key, span.start_offset)`` so two runs over one
    #: transcript produce byte-identical output and a replay matches.
    items: tuple[ReportedItem, ...] = ()
    #: Attributes the narrative did not settle. Required, may be empty: a model
    #: that must name what it could not determine cannot quietly fill a gap.
    unknowns: tuple[str, ...] = ()

    accepted: bool = True
    rejection_reason: str | None = Field(default=None, max_length=300)
    model_ref: str = Field(default="", max_length=120)

    #: Which screen inspected the narrative, and what it found. Names only.
    screen: str = Field(default="", max_length=60)
    screen_findings: tuple[str, ...] = ()
    #: True when the screen removed something that tried to instruct the model.
    screened: bool = False

    #: Values the model returned that this module refused. Counted, never
    #: quoted -- a refused value is exactly the text nobody should re-publish.
    dropped_values: int = Field(default=0, ge=0)

    @property
    def reported_keys(self) -> tuple[str, ...]:
        return tuple(sorted({item.intake_key for item in self.items}))

    def first(self, intake_key: str) -> ReportedItem | None:
        """The earliest thing the narrative said about one attribute."""
        for item in self.items:
            if item.intake_key == intake_key:
                return item
        return None


class IntakeSignals(BaseModel):
    """The closed, deterministic reading of one intake.

    This is the *only* thing routing is allowed to see. It is deliberately six
    typed fields and nothing else: no confidence, no model reference, no free
    text the model chose the shape of. A rule table matched against this cannot
    be steered by a model, because there is no field here a model could write
    that means "wake that agent".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    channel: IntakeChannel
    #: Presence, not truth. The caller *mentioned* entrapment; whether anyone is
    #: actually trapped is not something a transcript settles and not something
    #: this system will claim. Presence is also the safe direction: the cost of
    #: a mention that turns out to be nothing is one extra agent waking up.
    entrapment_reported: bool = False
    hazmat_reported: bool = False
    reported_floor_of_origin: int | None = Field(default=None, ge=1, le=MAX_REPORTED_FLOOR)
    reported_alarm_level: int | None = Field(default=None, ge=1, le=MAX_ALARM_LEVEL)
    occupancy_reported: bool = False
    access_reported: bool = False


def _parse_count(raw: str) -> int | None:
    """The number a caller said, or ``None``.

    Digits first, then the closed ordinal table. Anything else is left as text:
    a value this cannot parse still appears on the brief as the caller's words,
    it just contributes no signal to routing. Guessing at "the top floor" would
    be the model's judgement wearing a number.
    """
    match = _DIGITS.search(raw)
    if match is not None:
        return int(match.group(0))
    lowered = raw.lower()
    for word, value in _ORDINALS.items():
        if re.search(rf"\b{word}\b", lowered):
            return value
    return None


def signals_from(reading: IntakeReading) -> IntakeSignals:
    """Reduce a reading to the closed signal set. Pure, total, deterministic.

    Same reading in, same signals out, forever -- which is what makes the
    routing decision reproducible during a replay two years later, and what
    makes it checkable that the model did not participate in it.
    """
    floor_item = reading.first(IntakeKeys.REPORTED_FLOOR_OF_ORIGIN)
    floor = _parse_count(floor_item.raw_value) if floor_item is not None else None
    if floor is not None and not (1 <= floor <= MAX_REPORTED_FLOOR):
        floor = None

    alarm_item = reading.first(IntakeKeys.REPORTED_ALARM_LEVEL)
    alarm = _parse_count(alarm_item.raw_value) if alarm_item is not None else None
    if alarm is not None and not (1 <= alarm <= MAX_ALARM_LEVEL):
        alarm = None

    return IntakeSignals(
        channel=reading.channel,
        entrapment_reported=reading.first(IntakeKeys.ENTRAPMENT_REPORTED) is not None,
        hazmat_reported=reading.first(IntakeKeys.HAZMAT_REPORTED) is not None,
        reported_floor_of_origin=floor,
        reported_alarm_level=alarm,
        occupancy_reported=reading.first(IntakeKeys.REPORTED_OCCUPANCY) is not None,
        access_reported=reading.first(IntakeKeys.ACCESS_NOTE) is not None,
    )


class IntakeReader:
    """Turns one narrative into one reading. Never raises, never asserts."""

    def __init__(
        self,
        *,
        model: ModelClient | None = None,
        screen: DocumentScreen | None = None,
        deadline_ms: int = INTAKE_DEADLINE_MS,
    ) -> None:
        self._model = model
        self._screen = screen
        self._deadline_ms = deadline_ms

    async def read(
        self,
        narrative: str,
        *,
        incident_id: str,
        channel: IntakeChannel,
        source_ref: str,
    ) -> IntakeReading:
        """Read the narrative, or report plainly that it was not read.

        The order is the design. The narrative is screened before a model sees
        it, because a 911 transcript is citizen-authored text arriving over a
        public interface and that is the exact shape of a prompt injection.
        The screen fails *closed on the model*: if it could not run, no model
        call is made and the intake reports nothing, rather than a model being
        handed unscreened text under time pressure. A live screen having an
        outage arrives here as a verdict that says so and takes that same
        branch -- it used to arrive as an exception nothing caught, so a Model
        Armor outage did not degrade the intake, it took the request down.
        """

        def rejected(reason: str, *, screen: str = "", model_ref: str = "") -> IntakeReading:
            return rejected_reading(
                incident_id=incident_id,
                channel=channel,
                source_ref=source_ref,
                reason=reason,
                screen=screen,
                model_ref=model_ref,
            )

        text = (narrative or "").strip()[:MAX_NARRATIVE_CHARS]
        if not text:
            return rejected("the narrative was empty")
        if self._model is None:
            return rejected("no model is configured; the intake was not read")

        screen_name = ""
        findings: tuple[str, ...] = ()
        screened = False
        if self._screen is not None:
            verdict = await self._screen.inspect(text)
            screen_name = verdict.screen
            findings = verdict.findings
            screened = verdict.blocked
            if not verdict.may_reach_model:
                logger.warning(
                    "intake_screen_unavailable",
                    extra={"incident_id": incident_id, "screen": screen_name},
                )
                return rejected("the narrative screen is unavailable", screen=screen_name)
            text = verdict.safe_text

        try:
            result = await self._model.extract(
                document_text=text,
                schema_keys=INTAKE_KEYS,
                source_ref=source_ref,
                deadline_ms=self._deadline_ms,
            )
        except Exception as exc:
            # Including a timeout. An intake that could take the incident down
            # would be a worse failure than an intake nobody read.
            logger.warning(
                "intake_extraction_unavailable",
                extra={"incident_id": incident_id, "error_type": type(exc).__name__},
            )
            return rejected("the intake model is unavailable", screen=screen_name)

        if not result.accepted:
            logger.warning(
                "model_output_rejected",
                extra={"incident_id": incident_id, "model_ref": result.model_ref},
            )
            return rejected(
                result.rejection_reason or "the intake model's response was refused",
                screen=screen_name,
                model_ref=result.model_ref,
            )

        items, dropped = self._bind(result, text, channel=channel, source_ref=source_ref)
        return IntakeReading(
            incident_id=incident_id,
            channel=channel,
            source_ref=source_ref,
            items=items,
            unknowns=tuple(sorted(u for u in result.unknowns if u in INTAKE_KEYS)),
            accepted=True,
            model_ref=result.model_ref,
            screen=screen_name,
            screen_findings=findings,
            screened=screened,
            dropped_values=dropped,
        )

    @staticmethod
    def _bind(
        result: ExtractionResult,
        text: str,
        *,
        channel: IntakeChannel,
        source_ref: str,
    ) -> tuple[tuple[ReportedItem, ...], int]:
        """Keep only values the source text actually supports.

        Three checks, each of which has to be here rather than in the adapter,
        because the adapter validates the *shape* of a response and this
        validates its *relationship to the document*:

        1. the key is one of the six that were asked for;
        2. the offsets fall inside the screened text;
        3. the quoted text equals the text at those offsets.

        Check three is the one that matters. A model can return a well-formed
        span pointing at a sentence that says the opposite, or at nothing at
        all, and the result would validate against every schema in the system
        while attributing words to a caller who never said them.
        """
        kept: list[ReportedItem] = []
        dropped = 0
        for value in result.values:
            if value.canonical_key not in INTAKE_KEYS:
                dropped += 1
                continue
            span = value.span
            if not (0 <= span.start_offset < span.end_offset <= len(text)):
                dropped += 1
                continue
            if text[span.start_offset : span.end_offset] != span.quoted_text:
                dropped += 1
                continue
            kept.append(
                ReportedItem(
                    intake_key=value.canonical_key,
                    raw_value=value.raw_value[:MAX_REPORTED_CHARS],
                    span=span,
                    channel=channel,
                    source_ref=source_ref,
                    model_confidence=value.model_confidence,
                )
            )
        if dropped:
            logger.warning("intake_values_dropped", extra={"dropped": dropped})
        kept.sort(key=lambda item: (item.intake_key, item.span.start_offset))
        return tuple(kept), dropped


def rejected_reading(
    *,
    incident_id: str,
    channel: IntakeChannel,
    source_ref: str,
    reason: str,
    screen: str = "",
    model_ref: str = "",
) -> IntakeReading:
    """An intake that reported nothing, and says why.

    Every one of the failure paths ends here rather than raising. The instant
    brief has already been persisted and streamed by the time the intake runs,
    so an unread narrative costs an amendment and nothing else -- and an
    exception on this path would cost the request that was carrying it.
    """
    return IntakeReading(
        incident_id=incident_id,
        channel=channel,
        source_ref=source_ref,
        accepted=False,
        rejection_reason=reason[:300],
        model_ref=model_ref,
        screen=screen,
        # Everything is unknown, stated rather than left blank: an intake that
        # reported nothing must not read like a narrative that said nothing.
        unknowns=INTAKE_KEYS,
    )


# ------------------------------------------------------------------ the brief


def _reported_item(
    item: ReportedItem,
    *,
    snapshot: ProfileSnapshot,
) -> BriefItem:
    """One brief line for one reported value.

    Two rules, both enforced by :class:`~firstdue.domain.briefs.BriefItem`
    itself once ``reported_note`` is set: the line is never ``CONFIRMED`` and it
    never carries a ``fact_id``. What is left to decide here is only whether the
    reported value *disagrees* with something on file, which is the difference
    between a line an officer should look at twice and a line they should not.
    """
    filed_key = MIRRORS.get(item.intake_key)
    filed = snapshot.facts.get(filed_key) if filed_key else None
    note = item.provenance

    status = AssertionStatus.UNKNOWN
    if filed is not None and filed.value.is_known:
        filed_render = filed.value.render()
        note = (
            f"{note}. The filed record says {filed_render} "
            f"({filed.source_type}) and stands as the value of record."
        )
        if filed_render.strip().lower() != item.raw_value.strip().lower():
            status = AssertionStatus.DISPUTED
    else:
        note = f"{note}. Nothing is on file for this attribute; a report does not become one."

    return BriefItem(
        label=LABEL_FOR.get(item.intake_key, item.intake_key),
        value_render=f"REPORTED - {item.raw_value}"[:500],
        status=status,
        # No canonical key on purpose: the brief must not index a caller report
        # under the attribute a filed record owns.
        reported_note=note[:300],
    )


def _cross_checks(
    reading: IntakeReading,
    signals: IntakeSignals,
    *,
    snapshot: ProfileSnapshot,
    cad_alarm_level: int,
) -> tuple[BriefItem, ...]:
    """Disagreements between what was reported and what is on record.

    Stated as disagreements and never resolved. Both of these are cases where
    the reported number is the one a commander is most likely to act on and the
    one this system has the least standing to confirm.
    """
    items: list[BriefItem] = []

    floors = snapshot.facts.get(Keys.STORIES)
    reported_floor = signals.reported_floor_of_origin
    if reported_floor is not None and floors is not None and floors.value.is_known:
        filed = _parse_count(floors.value.render())
        if filed is not None and reported_floor > filed:
            items.append(
                BriefItem(
                    label="reported floor of origin vs filed storey count",
                    value_render=(
                        f"REPORTED - floor {reported_floor}; the filed record shows "
                        f"{filed} storeys"
                    ),
                    status=AssertionStatus.DISPUTED,
                    reported_note=(
                        f"reported by the {CHANNEL_LABEL[reading.channel]}; the filed "
                        "storey count is unchanged by it"
                    ),
                )
            )

    reported_alarm = signals.reported_alarm_level
    if reported_alarm is not None and reported_alarm != cad_alarm_level:
        items.append(
            BriefItem(
                label="reported alarm level vs dispatched alarm level",
                value_render=(
                    f"REPORTED - alarm {reported_alarm}; CAD dispatched alarm {cad_alarm_level}"
                ),
                status=AssertionStatus.DISPUTED,
                reported_note=(
                    "the incident runs at the alarm level CAD dispatched. A reported "
                    "level is recorded and never applied, because the alarm level "
                    "bounds the incident grant."
                ),
            )
        )
    return tuple(items)


def reported_sections(
    reading: IntakeReading,
    signals: IntakeSignals,
    *,
    snapshot: ProfileSnapshot,
    cad_alarm_level: int,
) -> tuple[BriefSection, ...]:
    """The only sanctioned route from an intake to a brief.

    Everything a caller said reaches the commander through this function, which
    is what makes "reported never renders as confirmed" a property of one
    reviewable place rather than of every caller of the intake.
    """
    if not reading.items:
        return ()

    grouped: dict[BriefSectionKey, list[BriefItem]] = {}
    for item in reading.items:
        section_key = SECTION_FOR.get(item.intake_key)
        if section_key is None:  # pragma: no cover - INTAKE_KEYS is closed
            continue
        grouped.setdefault(section_key, []).append(_reported_item(item, snapshot=snapshot))

    checks = _cross_checks(reading, signals, snapshot=snapshot, cad_alarm_level=cad_alarm_level)
    if checks:
        grouped.setdefault(BriefSectionKey.CONFLICTS, []).extend(checks)

    return tuple(
        BriefSection(key=key, items=tuple(items))
        for key, items in sorted(grouped.items(), key=lambda pair: pair[0].value)
    )


__all__ = [
    "CHANNEL_LABEL",
    "INTAKE_DEADLINE_MS",
    "INTAKE_KEYS",
    "IntakeChannel",
    "IntakeKeys",
    "IntakeReader",
    "IntakeReading",
    "IntakeSignals",
    "ReportedItem",
    "rejected_reading",
    "reported_sections",
    "signals_from",
]
