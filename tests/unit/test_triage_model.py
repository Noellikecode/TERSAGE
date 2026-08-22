"""Triage: the one verb whose failure must be safe in both directions.

Gemma decides whether a document is worth a Gemini call. The whole reason a
cheap model is allowed to make that call is that it cannot put a wrong fact in
front of an officer -- so every failure path has to answer *extract*, and a
triage that says "skip" must never be able to overrule a local screen that
thought the document was worth reading.
"""

from __future__ import annotations

import pytest

from firstdue.adapters.fake.model import FakeModelClient
from firstdue.adapters.vertex.model import VertexModelClient
from firstdue.errors import UpstreamTimeoutError
from firstdue.extraction.extractor import FactExtractor
from firstdue.extraction.triage import NARRATIVE_KEYS, triage
from firstdue.ports.model import TriageResult

NARRATIVE = (
    "Inspection found the rear stairwell partially obstructed by storage. "
    "The building is wood frame with a lightweight truss roof over the third floor."
)
IRRELEVANT = (
    "Payment of the annual billable inspection fee was received and processed "
    "by the bureau of delinquent revenue on the date shown above."
)


class _Client:
    """A model client whose triage answers whatever a test needs."""

    def __init__(self, verdict: TriageResult | Exception) -> None:
        self._verdict = verdict
        self.calls = 0

    async def triage(self, **_kwargs: object) -> TriageResult:
        self.calls += 1
        if isinstance(self._verdict, Exception):
            raise self._verdict
        return self._verdict


def _extractor(model: object) -> FactExtractor:
    from firstdue.adapters.clock import DeterministicIdGenerator

    return FactExtractor(ids=DeterministicIdGenerator("triage-test"), model=model)  # type: ignore[arg-type]


# ------------------------------------------------------------ local classifier


def test_the_local_classifier_still_reads_structural_vocabulary() -> None:
    decision = triage(NARRATIVE)
    assert decision.extract
    assert decision.candidate_keys


def test_the_local_classifier_skips_a_document_with_nothing_in_it() -> None:
    assert not triage(IRRELEVANT).extract


def test_the_local_classifier_skips_a_document_too_short_to_carry_a_fact() -> None:
    assert not triage("ok").extract


# ------------------------------------------------------------------ the fake


async def test_the_fake_answers_through_the_triage_verb() -> None:
    client = FakeModelClient()
    result = await client.triage(
        document_text=NARRATIVE, schema_keys=NARRATIVE_KEYS, deadline_ms=1500
    )
    assert result.extract
    assert result.accepted
    assert client.triage_calls == 1
    # A trace must be able to tell the cheap model from the expensive one.
    assert result.model_ref != "fake-extractor/1"


async def test_an_unavailable_fake_triage_still_answers_extract() -> None:
    """A triage outage must not be able to silence a document."""
    client = FakeModelClient(unavailable=True)
    result = await client.triage(
        document_text=IRRELEVANT, schema_keys=NARRATIVE_KEYS, deadline_ms=1500
    )
    assert result.extract is True
    assert result.accepted is False


async def test_the_other_verbs_still_raise_when_unavailable() -> None:
    """Only triage is allowed to fail soft."""
    client = FakeModelClient(unavailable=True)
    with pytest.raises(UpstreamTimeoutError):
        await client.extract(
            document_text=NARRATIVE,
            schema_keys=NARRATIVE_KEYS,
            source_ref="permit/1",
            deadline_ms=8000,
        )


# ---------------------------------------------------------- the fallback rule


async def test_a_timed_out_triage_falls_back_to_the_local_screen() -> None:
    extractor = _extractor(_Client(UpstreamTimeoutError("gemma unreachable")))
    decision = await extractor._triage(NARRATIVE)
    assert decision.extract


async def test_a_rejected_triage_falls_back_to_the_local_screen() -> None:
    extractor = _extractor(
        _Client(
            TriageResult(
                extract=False,
                reason="unparseable",
                accepted=False,
                model_ref="vertex/gemma",
            )
        )
    )
    decision = await extractor._triage(NARRATIVE)
    assert decision.extract, "a rejected triage must not skip a document"


async def test_a_skip_needs_both_screens_to_agree() -> None:
    """The cheap model may save a call. It may not hide a document.

    If the local vocabulary check thinks a document speaks to a structural
    attribute, a model that disagrees does not get to end the matter.
    """
    extractor = _extractor(
        _Client(TriageResult(extract=False, reason="model says no", model_ref="vertex/gemma"))
    )
    decision = await extractor._triage(NARRATIVE)
    assert decision.extract


async def test_both_screens_agreeing_to_skip_actually_skips() -> None:
    """The cost saving has to be real, or the cheap model is pointless."""
    extractor = _extractor(
        _Client(TriageResult(extract=False, reason="nothing structural", model_ref="vertex/gemma"))
    )
    decision = await extractor._triage(IRRELEVANT)
    assert not decision.extract
    assert decision.reason == "nothing structural"


async def test_the_model_can_rescue_a_document_the_local_screen_would_skip() -> None:
    """Vocabulary matching is a word list; a model reads. Either may say yes."""
    extractor = _extractor(
        _Client(
            TriageResult(
                extract=True,
                reason="describes an attic conversion",
                model_ref="vertex/gemma",
            )
        )
    )
    decision = await extractor._triage(IRRELEVANT)
    assert decision.extract


# -------------------------------------------------------------- the live path


def test_the_live_client_names_a_separate_triage_model() -> None:
    client = VertexModelClient(
        project_id="p",
        location="us-central1",
        model="gemini-3.5-flash",
        triage_model="gemma-3-4b-it",
    )
    assert client.triage_model_ref != client.model_ref
    assert "gemma" in client.triage_model_ref


async def test_the_live_client_without_a_triage_model_defers_to_the_local_screen() -> None:
    client = VertexModelClient(project_id="p", location="us-central1", model="gemini-3.5-flash")
    result = await client.triage(
        document_text=NARRATIVE, schema_keys=NARRATIVE_KEYS, deadline_ms=1500
    )
    assert result.extract is True
    assert result.accepted is False


def test_the_live_client_cannot_mint_a_canonical_key() -> None:
    """Triage names no keys at all now, so it cannot invent one.

    The answer is one word. `candidate_keys` is empty by construction rather
    than by filtering, which is a smaller surface than the JSON contract that
    preceded it -- there is nothing left to sanitise.
    """
    client = VertexModelClient(project_id="p", location="us-central1", model="m", triage_model="t")
    assert client._parse_triage("EXTRACT", NARRATIVE_KEYS).candidate_keys == ()
    assert client._parse_triage("SKIP", NARRATIVE_KEYS).candidate_keys == ()


def test_only_a_bare_skip_can_stop_a_document() -> None:
    """The asymmetry that justifies letting a cheap model decide at all.

    A wrong EXTRACT costs one model call. A wrong SKIP means nobody ever reads
    the filing. So SKIP has to be the entire answer: a model that replies
    "SKIP, because the permit is about plumbing" has explained itself into an
    extraction, which is the safe direction.
    """
    client = VertexModelClient(project_id="p", location="us-central1", model="m", triage_model="t")
    assert client._parse_triage("SKIP", NARRATIVE_KEYS).extract is False
    assert client._parse_triage("  skip \n", NARRATIVE_KEYS).extract is False
    assert client._parse_triage("SKIP.", NARRATIVE_KEYS).extract is False
    for hedged in ("SKIP, because it is about plumbing", "probably SKIP", "SKIP EXTRACT"):
        assert client._parse_triage(hedged, NARRATIVE_KEYS).extract is True


def test_the_live_client_defaults_to_extract_on_malformed_output() -> None:
    """Including the exact shape Gemma really returns.

    Verified live: asked for the documented JSON schema, Gemma answered
    `{"answer": "Yes. The permit explicitly mentions..."}`. Well-formed JSON,
    its own keys, prose inside. Under the old contract that parsed to "no
    answer" on every single document.
    """
    client = VertexModelClient(project_id="p", location="us-central1", model="m", triage_model="t")
    for raw in (
        "not json",
        "[]",
        '{"reason": "no answer"}',
        '{"answer": "Yes. The permit explicitly mentions building stories."}',
        "",
    ):
        parsed = client._parse_triage(raw, NARRATIVE_KEYS)
        assert parsed.extract is True
        assert parsed.accepted is False
