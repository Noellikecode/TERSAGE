"""The referral email: what sends it, what cannot, and what it may say.

Three claims are under test here, and each of them is a promise the system
makes to a property owner rather than to an operator.

**Nothing leaves on a detection.** A conflict produces a draft and a staged
approval. The building department hears nothing until a captain taps, and the
mail client is not reachable from the staging path at all.

**Nothing leaves twice.** One approval, one message -- across a replay, across
a restart, and across the transport's own retries.

**Nothing is said that a stored record did not say.** A model may rewrite the
referral. If the rewrite loses a citation, invents one, restates the severity,
or drops the sentence that says this is a disagreement and not a violation, the
deterministic template ships instead and the fallback is on the audit log.

Every HTTP call here goes to an `httpx.MockTransport`. Nothing in this file
reaches a network, and the API keys are invented.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from firstdue.adapters.clock import DeterministicIdGenerator, FixedClock
from firstdue.adapters.fake.office import FakeCalendar, FakeObjectStore
from firstdue.adapters.fake.writes import FakeWriteTarget
from firstdue.adapters.memory.audit import InMemoryAuditSink
from firstdue.adapters.memory.repositories import (
    InMemoryApprovalRepository,
    InMemoryCompensationRepository,
    InMemoryConflictRepository,
    InMemoryIdempotencyRepository,
    InMemoryProfileRepository,
    InMemoryQueueRepository,
    InMemoryReferralRepository,
    InMemoryWriteActionRepository,
)
from firstdue.adapters.resend import API_KEY_SETTING, ResendMailClient
from firstdue.agents.actions import (
    REFERRAL_MAIL_TARGET,
    REFERRAL_TARGET,
    WORK_ORDER_TARGET,
    ActionFlow,
)
from firstdue.domain.conflicts import Conflict
from firstdue.domain.enums import Department, ReferralStatus
from firstdue.domain.keys import Keys
from firstdue.domain.profiles import BuildingProfile
from firstdue.domain.work import RankReason, ReferralRecord, SurveyQueueEntry
from firstdue.errors import ConfigurationError, SourceUnavailableError, ValidationError
from firstdue.ports.audit import AuditEventKind
from firstdue.ports.model import ProseResult
from firstdue.ports.office import MailMessage
from firstdue.reliability.retry import RetryPolicy

NOW = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)
ADDRESS = "sf-0450-hayes"
DISTRICT = "sffd-district-03"
CREW = "e05@sffd.example"
BUILDING_DEPT = ("intake@dbi.sfgov.example",)
FACT_IDS = ("fact_permit01", "fact_lidar02")

#: Invented. Shaped like a Resend key so the "never logged" test is meaningful.
API_KEY = "re_TestOnly_000000000000000000000000"
SENDER = "referrals@sffd.example"

#: Retries in a test wait a millisecond, not a quarter second. The schedule is
#: asserted in the retry suite; what matters here is how many attempts happen.
FAST = RetryPolicy(max_attempts=3, base_delay_ms=1, max_delay_ms=2, jitter_ratio=0.0)


# ------------------------------------------------------------------ doubles


class RecordingMailer:
    """A mail client that remembers, and dedupes exactly as the real ones do."""

    def __init__(self) -> None:
        self.messages: list[MailMessage] = []
        self._by_key: dict[str, MailMessage] = {}

    async def send(self, message: MailMessage, *, idempotency_key: str) -> MailMessage:
        existing = self._by_key.get(idempotency_key)
        if existing is not None:
            return existing
        sent = message.model_copy(update={"external_ref": f"MSG-{len(self.messages) + 1:05d}"})
        self._by_key[idempotency_key] = sent
        self.messages.append(sent)
        return sent

    async def sent(self) -> Sequence[MailMessage]:
        return list(self.messages)

    @property
    def to_building_department(self) -> list[MailMessage]:
        return [m for m in self.messages if m.to == BUILDING_DEPT]


class StubComposer:
    """A model that returns whatever the test told it to, or raises."""

    def __init__(self, text: str | None = None, *, accepted: bool = True) -> None:
        self._text = text
        self._accepted = accepted
        self.fields: dict[str, Any] = {}
        self.calls = 0

    async def compose(
        self, *, template_id: str, fields: Any, max_chars: int, deadline_ms: int
    ) -> ProseResult:
        self.calls += 1
        self.fields = dict(fields)
        if self._text is None:
            raise SourceUnavailableError("the model is down")
        return ProseResult(
            text=self._text[:max_chars], accepted=self._accepted, model_ref="stub/composer"
        )


# ------------------------------------------------------------------ fixtures


def _conflict(severity: int = 4) -> Conflict:
    return Conflict(
        conflict_id="conflict_c1",
        address_id=ADDRESS,
        canonical_key=Keys.STORIES,
        rule_id="stories-filed-vs-measured",
        severity=severity,
        fact_ids=FACT_IDS,
        summary="The permit record says two storeys; the lidar return measures three.",
        detected_at=NOW,
    )


def _entry() -> SurveyQueueEntry:
    return SurveyQueueEntry(
        entry_id="q1",
        address_id=ADDRESS,
        district_id=DISTRICT,
        rank=1,
        score=0.9,
        reasons=(
            RankReason(
                rule_id="stories-filed-vs-measured",
                detail="Filed and measured storey counts disagree.",
                weight=0.9,
            ),
        ),
        created_at=NOW,
        ranked_by_version="1.0.0",
    )


class Harness:
    """One wired ``ActionFlow`` and everything a test needs to look inside it."""

    def __init__(
        self,
        *,
        mailer: Any,
        model: Any | None = None,
        referral_mailer: Any | None = None,
        recipients: tuple[str, ...] = BUILDING_DEPT,
        severity: int = 4,
    ) -> None:
        self.clock = FixedClock(NOW)
        self.ids = DeterministicIdGenerator("referral-email")
        self.mailer = mailer
        self.profiles = InMemoryProfileRepository()
        self.referrals = InMemoryReferralRepository()
        self.approvals = InMemoryApprovalRepository()
        self.audit = InMemoryAuditSink()
        self.queue = InMemoryQueueRepository()
        self.conflict = _conflict(severity)
        self.flow = ActionFlow(
            profiles=self.profiles,
            conflicts=InMemoryConflictRepository(),
            queue=self.queue,
            referrals=self.referrals,
            approvals=self.approvals,
            write_actions=InMemoryWriteActionRepository(),
            compensations=InMemoryCompensationRepository(),
            write_targets={
                target: FakeWriteTarget(
                    target_id=target,
                    receiving_department=Department.BUILDING,
                    clock=self.clock,
                    ids=self.ids,
                    external_ref_prefix="BLD",
                )
                for target in (WORK_ORDER_TARGET, REFERRAL_TARGET)
            },
            calendar=FakeCalendar(clock=self.clock, ids=self.ids),
            mailer=mailer,
            plan_store=FakeObjectStore(bucket="test-plans", clock=self.clock),
            clock=self.clock,
            ids=self.ids,
            audit=self.audit,
            model=model,
            referral_mailer=referral_mailer,
            referral_recipients=recipients,
        )

    async def seed(self, *, prior: tuple[ReferralRecord, ...] = ()) -> SurveyQueueEntry:
        await self.profiles.create(
            BuildingProfile(
                address_id=ADDRESS,
                district_id=DISTRICT,
                conflicts=(self.conflict,),
                open_referrals=prior,
            )
        )
        entry = _entry()
        await self.queue.replace_district_queue(DISTRICT, [entry])
        return entry

    async def dispatch(self, **kwargs: Any) -> Any:
        entry = await self.seed(prior=kwargs.pop("prior", ()))
        return await self.flow.dispatch(
            entry, company="E-05", crew_email=CREW, correlation_id="corr-1", **kwargs
        )

    async def audit_kinds(self, kind: AuditEventKind) -> list[Any]:
        return [e for e in await self.audit.list_events(limit=200) if e.kind is kind]


def _transport(*responses: httpx.Response) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    """A transport that answers with each response in turn, and keeps the requests."""
    seen: list[httpx.Request] = []
    queued = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return queued.pop(0) if len(queued) > 1 else queued[0]

    return httpx.MockTransport(handler), seen


def _resend(
    *responses: httpx.Response,
    api_key: str = API_KEY,
    policy: RetryPolicy = FAST,
) -> tuple[ResendMailClient, list[httpx.Request]]:
    transport, seen = _transport(*responses)
    client = ResendMailClient(
        api_key=api_key,
        sender=SENDER,
        clock=FixedClock(NOW),
        idempotency=InMemoryIdempotencyRepository(),
        policy=policy,
        transport=transport,
    )
    return client, seen


def _ok(message_id: str = "resend-0001") -> httpx.Response:
    return httpx.Response(200, json={"id": message_id})


def _message() -> MailMessage:
    return MailMessage(
        message_id="msg_ref_1",
        to=BUILDING_DEPT,
        subject="SFFD referral",
        body="A disagreement between filed and measured records.",
    )


# --------------------------------------------------------------- the gate ---


@pytest.mark.invariant
async def test_staging_a_referral_sends_nothing_to_the_building_department() -> None:
    """The autonomous half of the flow does not reach another agency.

    The crew notification goes out, because notifying a firefighter of their
    own assignment commits nobody else's time. The referral does not.
    """
    harness = Harness(mailer=RecordingMailer())
    result = await harness.dispatch()

    assert result.referral_id is not None
    referral = await harness.referrals.get(result.referral_id)
    assert referral is not None
    assert referral.status is ReferralStatus.AWAITING_APPROVAL
    assert referral.case_number is None

    assert harness.mailer.to_building_department == []
    assert [m.to for m in harness.mailer.messages] == [(CREW,)]


@pytest.mark.invariant
async def test_a_referral_email_cannot_be_sent_without_a_recorded_approval() -> None:
    """The guard on the send itself, not on the caller.

    The one caller records the approval first. This asserts the invariant would
    survive a second caller that forgot to.
    """
    harness = Harness(mailer=RecordingMailer())
    result = await harness.dispatch()
    staged = await harness.referrals.get(str(result.referral_id))
    assert staged is not None
    approval = await harness.approvals.get(f"apr_{staged.referral_id}")
    assert approval is not None

    with pytest.raises(ValidationError):
        await harness.flow._mail_referral(staged, approval=approval, now=NOW)
    assert harness.mailer.to_building_department == []


async def test_approval_sends_exactly_one_referral_email() -> None:
    harness = Harness(mailer=RecordingMailer())
    dispatched = await harness.dispatch()
    approved = await harness.flow.approve_referral(
        str(dispatched.referral_id), approved_by="captain.reyes", correlation_id="corr-1"
    )

    sent = harness.mailer.to_building_department
    assert len(sent) == 1
    assert approved.notification_ref == sent[0].external_ref
    # The message carries the case number and names the human who approved it.
    assert approved.case_number in sent[0].body
    assert "captain.reyes" in sent[0].body
    assert sent[0].body.startswith("The San Francisco Fire Department requests review")


@pytest.mark.idempotency
async def test_a_replayed_approval_sends_no_second_email() -> None:
    """Two taps on the same card are one referral and one message."""
    harness = Harness(mailer=RecordingMailer())
    dispatched = await harness.dispatch()
    first = await harness.flow.approve_referral(
        str(dispatched.referral_id), approved_by="captain.reyes", correlation_id="corr-1"
    )
    second = await harness.flow.approve_referral(
        str(dispatched.referral_id), approved_by="captain.reyes", correlation_id="corr-2"
    )

    assert second.replayed
    assert second.case_number == first.case_number
    assert len(harness.mailer.to_building_department) == 1


async def test_the_send_is_audited_as_an_external_write() -> None:
    harness = Harness(mailer=RecordingMailer())
    dispatched = await harness.dispatch()
    await harness.flow.approve_referral(
        str(dispatched.referral_id), approved_by="captain.reyes", correlation_id="corr-1"
    )

    events = await harness.audit_kinds(AuditEventKind.WRITE_EXECUTED)
    mail_events = [e for e in events if e.target == REFERRAL_MAIL_TARGET]
    assert len(mail_events) == 1
    assert mail_events[0].detail["external_ref"]
    assert mail_events[0].detail["approved"] == "true"


async def test_no_recipient_means_no_email_and_no_failure() -> None:
    """A deployment with nowhere to send still files, and admits it sent nothing."""
    harness = Harness(mailer=RecordingMailer(), recipients=())
    dispatched = await harness.dispatch()
    approved = await harness.flow.approve_referral(
        str(dispatched.referral_id), approved_by="captain.reyes", correlation_id="corr-1"
    )

    assert approved.case_number
    assert approved.notification_ref is None
    # Only the crew notification the dispatch already sent.
    assert [m.to for m in harness.mailer.messages] == [(CREW,)]


# ------------------------------------------------------- the live transport


async def test_an_approved_referral_reaches_the_transport_once() -> None:
    """End to end over Resend's own wire format, with no network."""
    mailer, seen = _resend(_ok())
    harness = Harness(mailer=mailer)
    dispatched = await harness.dispatch()
    assert len(seen) == 1  # the crew notification, and nothing else

    approved = await harness.flow.approve_referral(
        str(dispatched.referral_id), approved_by="captain.reyes", correlation_id="corr-1"
    )
    assert len(seen) == 2
    assert approved.notification_ref == "resend-0001"

    payload = json.loads(seen[1].content)
    assert payload["to"] == list(BUILDING_DEPT)
    assert payload["from"] == SENDER
    assert approved.case_number in payload["text"]

    await harness.flow.approve_referral(
        str(dispatched.referral_id), approved_by="captain.reyes", correlation_id="corr-2"
    )
    assert len(seen) == 2


async def test_the_referral_may_use_a_different_transport_from_the_crew() -> None:
    """Two inboxes, two problems: inside the domain, and outside it."""
    crew_mailer = RecordingMailer()
    referral_mailer, seen = _resend(_ok("resend-outbound"))
    harness = Harness(mailer=crew_mailer, referral_mailer=referral_mailer)

    dispatched = await harness.dispatch()
    assert seen == []  # the crew notification did not touch the outbound transport

    approved = await harness.flow.approve_referral(
        str(dispatched.referral_id), approved_by="captain.reyes", correlation_id="corr-1"
    )
    assert approved.notification_ref == "resend-outbound"
    assert len(seen) == 1
    assert crew_mailer.to_building_department == []


@pytest.mark.idempotency
async def test_a_replayed_key_never_reaches_the_transport_twice() -> None:
    """The dedupe is durable, so a restarted process is still one send."""
    idempotency = InMemoryIdempotencyRepository()
    transport, seen = _transport(_ok())

    def build() -> ResendMailClient:
        return ResendMailClient(
            api_key=API_KEY,
            sender=SENDER,
            clock=FixedClock(NOW),
            idempotency=idempotency,
            policy=FAST,
            transport=transport,
        )

    first = await build().send(_message(), idempotency_key="referral-key-0001")
    # A second, freshly-constructed client: the memory that matters is in the
    # repository, not in the object that did the sending.
    second = await build().send(_message(), idempotency_key="referral-key-0001")

    assert len(seen) == 1
    assert first.external_ref == second.external_ref == "resend-0001"


def test_a_missing_api_key_is_a_configuration_error_at_construction() -> None:
    """Named, and raised at wiring time. Not on the first approval."""
    with pytest.raises(ConfigurationError) as raised:
        _resend(_ok(), api_key="")
    assert API_KEY_SETTING in str(raised.value)
    assert raised.value.details["setting"] == API_KEY_SETTING

    with pytest.raises(ConfigurationError):
        ResendMailClient(
            api_key=API_KEY,
            sender="   ",
            clock=FixedClock(NOW),
            idempotency=InMemoryIdempotencyRepository(),
        )


@pytest.mark.degraded
async def test_a_4xx_is_not_retried() -> None:
    """A message the vendor refused on its merits is refused again."""
    mailer, seen = _resend(httpx.Response(422, json={"message": "invalid recipient"}))
    with pytest.raises(ValidationError):
        await mailer.send(_message(), idempotency_key="referral-key-0002")
    assert len(seen) == 1


@pytest.mark.degraded
async def test_a_rejected_credential_is_not_retried() -> None:
    """401 is a deployment fault. Retrying it re-presents the same bad key."""
    mailer, seen = _resend(httpx.Response(401, json={"message": "invalid api key"}))
    with pytest.raises(ConfigurationError):
        await mailer.send(_message(), idempotency_key="referral-key-0003")
    assert len(seen) == 1


@pytest.mark.degraded
async def test_a_5xx_is_retried() -> None:
    mailer, seen = _resend(httpx.Response(503), _ok("resend-late"))
    sent = await mailer.send(_message(), idempotency_key="referral-key-0004")
    assert len(seen) == 2
    assert sent.external_ref == "resend-late"


@pytest.mark.degraded
async def test_a_persistent_outage_exhausts_the_policy_and_surfaces() -> None:
    """Not swallowed: an approval that did not reach the agency must be visible."""
    mailer, seen = _resend(httpx.Response(500))
    with pytest.raises(SourceUnavailableError):
        await mailer.send(_message(), idempotency_key="referral-key-0005")
    assert len(seen) == FAST.max_attempts


@pytest.mark.degraded
async def test_a_success_without_an_id_is_refused_rather_than_retried() -> None:
    """Retrying to chase an id would send the message a second time."""
    mailer, seen = _resend(httpx.Response(200, json={}))
    with pytest.raises(ValidationError):
        await mailer.send(_message(), idempotency_key="referral-key-0006")
    assert len(seen) == 1


@pytest.mark.invariant
async def test_the_api_key_never_reaches_a_log(caplog: pytest.LogCaptureFixture) -> None:
    """Nor does the body, nor the recipient."""
    mailer, _ = _resend(httpx.Response(500))
    with caplog.at_level(logging.DEBUG), pytest.raises(SourceUnavailableError):
        await mailer.send(_message(), idempotency_key="referral-key-0007")

    assert caplog.records, "the failure should have been logged at all"
    rendered = json.dumps([record.__dict__ for record in caplog.records], default=str)
    for secret in (API_KEY, "Bearer", _message().body, BUILDING_DEPT[0]):
        assert secret not in rendered, secret


# ----------------------------------------------------------- the draft path


async def test_without_a_model_the_narrative_is_the_deterministic_template() -> None:
    """The floor, byte for byte. This is what keeps the rest of the suite green."""
    harness = Harness(mailer=RecordingMailer())
    dispatched = await harness.dispatch()
    referral = await harness.referrals.get(str(dispatched.referral_id))
    assert referral is not None

    profile = await harness.profiles.get(ADDRESS)
    assert profile is not None
    assert referral.narrative == ActionFlow._referral_narrative(profile, harness.conflict)


async def test_a_valid_polished_draft_is_used() -> None:
    polished = (
        "The San Francisco Fire Department asks the building department to review "
        f"{ADDRESS}. A deterministic comparison under rule stories-filed-vs-measured, "
        "severity 4 of 5, found that the permit of record describes two storeys while "
        "a measured lidar return describes three.\n\n"
        f"Supporting records: {FACT_IDS[0]}, {FACT_IDS[1]}.\n\n"
        "Both source records are retained and neither has been amended. This referral "
        "states a disagreement between filed and measured records; it makes no "
        "determination of code compliance."
    )
    harness = Harness(mailer=RecordingMailer(), model=StubComposer(polished))
    dispatched = await harness.dispatch()
    referral = await harness.referrals.get(str(dispatched.referral_id))

    assert referral is not None
    assert referral.narrative == polished
    assert await harness.audit_kinds(AuditEventKind.MODEL_OUTPUT_REJECTED) == []


@pytest.mark.parametrize(
    ("draft", "expected_reason"),
    [
        pytest.param(
            "Review {address}. Severity 4 of 5 under stories-filed-vs-measured. "
            "Supporting records: {first}. It makes no determination of code compliance.",
            "fact_id_dropped",
            id="a-dropped-citation",
        ),
        pytest.param(
            "Review {address}. Severity 4 of 5. Supporting records: {first}, {second}. "
            "The structure is in violation of the building code.",
            "disclaimer_dropped",
            id="a-dropped-disclaimer",
        ),
        pytest.param(
            "Review {address}. Severity 4 of 5. Supporting records: {first}, {second}, "
            "fact_invented99. It makes no determination of code compliance.",
            "fact_id_introduced",
            id="an-invented-citation",
        ),
        pytest.param(
            "Review {address}. Severity 5 of 5 -- life safety. Supporting records: "
            "{first}, {second}. It makes no determination of code compliance.",
            "severity_altered",
            id="an-inflated-severity",
        ),
    ],
)
async def test_an_unfaithful_draft_falls_back_to_the_template(
    draft: str, expected_reason: str
) -> None:
    """Presentation is the model's. Evidence is not."""
    text = draft.format(address=ADDRESS, first=FACT_IDS[0], second=FACT_IDS[1])
    harness = Harness(mailer=RecordingMailer(), model=StubComposer(text))
    dispatched = await harness.dispatch()

    referral = await harness.referrals.get(str(dispatched.referral_id))
    profile = await harness.profiles.get(ADDRESS)
    assert referral is not None and profile is not None
    assert referral.narrative == ActionFlow._referral_narrative(profile, harness.conflict)

    rejected = await harness.audit_kinds(AuditEventKind.MODEL_OUTPUT_REJECTED)
    assert [e.detail["reason"] for e in rejected] == [expected_reason]


async def test_a_rejected_composition_falls_back() -> None:
    harness = Harness(
        mailer=RecordingMailer(), model=StubComposer("anything at all", accepted=False)
    )
    dispatched = await harness.dispatch()
    referral = await harness.referrals.get(str(dispatched.referral_id))
    profile = await harness.profiles.get(ADDRESS)

    assert referral is not None and profile is not None
    assert referral.narrative == ActionFlow._referral_narrative(profile, harness.conflict)
    rejected = await harness.audit_kinds(AuditEventKind.MODEL_OUTPUT_REJECTED)
    assert [e.detail["reason"] for e in rejected] == ["not_accepted"]


@pytest.mark.degraded
async def test_a_model_outage_does_not_stop_a_referral_being_staged() -> None:
    """A conflict nobody ever sees is worse than a plainly-worded referral."""
    harness = Harness(mailer=RecordingMailer(), model=StubComposer(None))
    dispatched = await harness.dispatch()

    referral = await harness.referrals.get(str(dispatched.referral_id))
    profile = await harness.profiles.get(ADDRESS)
    assert referral is not None and profile is not None
    assert referral.status is ReferralStatus.AWAITING_APPROVAL
    assert referral.narrative == ActionFlow._referral_narrative(profile, harness.conflict)

    rejected = await harness.audit_kinds(AuditEventKind.MODEL_OUTPUT_REJECTED)
    assert rejected[0].detail["reason"] == "model_unavailable"
    assert rejected[0].detail["error_type"] == "SourceUnavailableError"


async def test_a_redraft_is_told_how_the_last_referral_ended() -> None:
    """Outcomes, not prose: enough to answer a rejection, not enough to repeat it."""
    prior = ReferralRecord(
        referral_id="ref_old",
        address_id=ADDRESS,
        conflict_id="conflict_c0",
        supporting_fact_ids=("fact_old01", "fact_old02"),
        narrative="An earlier referral that the building department declined.",
        status=ReferralStatus.REJECTED,
        idempotency_key="referral-old-0001",
        drafted_at=NOW,
    )
    model = StubComposer("unused draft")
    harness = Harness(mailer=RecordingMailer(), model=model)
    await harness.dispatch(prior=(prior,))

    outcomes = model.fields["prior_outcomes"]
    assert outcomes == [
        {
            "referral_id": "ref_old",
            "status": str(ReferralStatus.REJECTED),
            "conflict_id": "conflict_c0",
            "case_number": "",
        }
    ]
    assert prior.narrative not in json.dumps(model.fields, default=str)


# ------------------------------------------------------- the clerk's own record


class BrokenMailer:
    """A crew mailer that is down, so a dispatch dies partway through."""

    async def send(self, message: MailMessage, *, idempotency_key: str) -> MailMessage:
        raise SourceUnavailableError("the crew mail transport is down")

    async def sent(self) -> Sequence[MailMessage]:
        return []


async def _dispatch_once(harness: Harness, **kwargs: Any) -> Any:
    entry = await harness.seed()
    return await harness.flow.dispatch(
        entry, company="E-05", crew_email=CREW, correlation_id="corr-1", **kwargs
    )


async def test_a_dispatch_with_no_budget_left_stops_itself_and_keeps_the_referral() -> None:
    """The bug that drew this clerk idle, from the other end.

    One dispatch is five external calls against a 60-second descriptor budget,
    and both runtimes enforce that budget with ``asyncio.timeout`` -- so an
    overrun is a cancellation, and the pass record at the end of ``dispatch``
    never ran. Handed a deadline it cannot meet, the flow now drops the four
    autonomous writes rather than being killed inside them, and keeps the one
    thing that is this agent's own work.
    """
    harness = Harness(mailer=RecordingMailer())
    result = await _dispatch_once(harness, deadline=NOW)

    assert result.work_order_ref is None
    assert result.calendar_event_ref is None
    assert result.notification_ref is None
    assert result.plan_object_id is None
    # Nothing was attempted, so nothing was sent -- not "sent and lost".
    assert harness.mailer.messages == []
    # The referral is the clerk's job and the cheapest thing in the dispatch.
    assert result.referral_id is not None

    passes = await harness.audit_kinds(AuditEventKind.AGENT_PASS)
    assert len(passes) == 1
    assert passes[0].detail["referral"] == "staged"
    assert passes[0].detail["skipped"] == "work_order,calendar,crew_mail,preplan"


async def test_the_last_seconds_of_a_budget_are_not_spent_on_wording() -> None:
    """Polish is the one thing a dispatch can drop without losing a fact."""
    model = StubComposer("A polished draft nobody will read.")
    harness = Harness(mailer=RecordingMailer(), model=model)
    result = await _dispatch_once(harness, deadline=NOW)

    assert model.calls == 0
    rejected = await harness.audit_kinds(AuditEventKind.MODEL_OUTPUT_REJECTED)
    assert [e.detail["reason"] for e in rejected] == ["out_of_budget"]

    referral = await harness.referrals.get(result.referral_id)
    assert referral is not None
    profile = await harness.profiles.get(ADDRESS)
    assert profile is not None
    assert referral.narrative == ActionFlow._referral_narrative(profile, harness.conflict)


async def test_a_dispatch_that_dies_partway_still_says_the_clerk_ran() -> None:
    """A failure is a result, and an absence is not.

    The pass record is this agent's only evidence on an ordinary pass, so it
    cannot be the statement that a mail outage deletes.
    """
    harness = Harness(mailer=BrokenMailer())
    entry = await harness.seed()
    with pytest.raises(SourceUnavailableError):
        await harness.flow.dispatch(entry, company="E-05", crew_email=CREW, correlation_id="corr-1")

    passes = await harness.audit_kinds(AuditEventKind.AGENT_PASS)
    assert len(passes) == 1
    # Not an outcome about the building: the dispatch ended before the referral.
    assert passes[0].detail["referral"] == "not_reached"


async def test_a_second_pass_over_the_same_building_records_what_it_found() -> None:
    """ "Nothing new to file" is a result of having looked.

    Only the ``staged`` branch used to write a step, so every pass after the
    first over a district -- a referral is derived from a conflict, and a
    conflict is stable -- left one bare line and the console drew a clerk that
    had gone quiet.
    """
    harness = Harness(mailer=RecordingMailer())
    entry = await harness.seed()
    for correlation_id in ("corr-1", "corr-2"):
        await harness.flow.dispatch(
            entry, company="E-05", crew_email=CREW, correlation_id=correlation_id
        )

    # `list_events` answers newest first; these read in the order they happened.
    steps = list(reversed(await harness.audit_kinds(AuditEventKind.AGENT_STEP)))
    assert [e.detail["status"] for e in steps] == ["awaiting_approval", "already_staged"]
    # Each step under the pass that produced it, never a fresh correlation.
    assert [e.correlation_id for e in steps] == ["corr-1", "corr-2"]

    passes = list(reversed(await harness.audit_kinds(AuditEventKind.AGENT_PASS)))
    assert [e.detail["referral"] for e in passes] == ["staged", "already_staged"]


async def test_a_building_with_nothing_to_refer_still_leaves_a_line() -> None:
    """A clerk that looked and found no open disagreement did work."""
    harness = Harness(mailer=RecordingMailer())
    await harness.profiles.create(BuildingProfile(address_id=ADDRESS, district_id=DISTRICT))
    entry = _entry()
    await harness.queue.replace_district_queue(DISTRICT, [entry])
    result = await harness.flow.dispatch(
        entry, company="E-05", crew_email=CREW, correlation_id="corr-1"
    )

    assert result.referral_id is None
    steps = await harness.audit_kinds(AuditEventKind.AGENT_STEP)
    assert [e.detail["status"] for e in steps] == ["no_open_conflict"]
    passes = await harness.audit_kinds(AuditEventKind.AGENT_PASS)
    assert passes[0].detail["referral"] == "none"


async def test_the_clerks_own_records_carry_no_prose() -> None:
    """Counts, ids and canonical keys, on the branches added here too."""
    harness = Harness(mailer=RecordingMailer())
    entry = await harness.seed()
    await harness.flow.dispatch(entry, company="E-05", crew_email=CREW, correlation_id="corr-1")
    await harness.flow.dispatch(entry, company="E-05", crew_email=CREW, correlation_id="corr-2")
    await harness.flow.dispatch(
        entry, company="E-05", crew_email=CREW, correlation_id="corr-3", deadline=NOW
    )

    written = [
        e
        for e in await harness.audit.list_events(limit=200)
        if e.kind in (AuditEventKind.AGENT_PASS, AuditEventKind.AGENT_STEP)
    ]
    assert written
    for event in written:
        for key, value in event.detail.items():
            assert isinstance(value, str), key
            assert " " not in value, (key, value)
