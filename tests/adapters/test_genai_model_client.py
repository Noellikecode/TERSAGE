"""The Gen AI SDK seam.

``_call`` and ``_stream`` are the only two methods in the live model client
that touch a vendor SDK. Everything else there is policy -- retries, deadlines,
parsing, rejection -- and is already covered against the fake client. These
tests cover the seam itself, which was previously marked ``no cover`` on the
grounds that it was "live mode only".

That exemption was worth removing. "Live mode only" meant the two methods most
likely to break on an SDK upgrade were the two nothing checked, and the failure
would surface as a broken call in a deployed system rather than as a red test
here. A stub cannot prove the remote service behaves; it can prove we call it
with the arguments we think we do, and that is the half that breaks silently.

**The signature test is the load-bearing one.** A hand-written stub drifts: the
SDK renames a parameter, the stub keeps accepting the old one, every test still
passes, and the first real call fails. So one test asserts the real installed
SDK accepts exactly the keyword arguments the client passes, without making a
network call -- which makes an SDK upgrade that moves the seam a test failure
rather than a deployment failure.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from typing import Any

import pytest

from firstdue.adapters.vertex.model import VertexModelClient
from firstdue.errors import UpstreamTimeoutError

# --------------------------------------------------------------- the stubs


class _Usage:
    def __init__(self, prompt: int = 0, completion: int = 0, total: int = 0) -> None:
        self.prompt_token_count = prompt
        self.candidates_token_count = completion
        self.total_token_count = total


class _Response:
    """Shaped like ``GenerateContentResponse``: ``.text`` may be ``None``."""

    def __init__(self, text: str | None, usage: _Usage | None = None) -> None:
        self.text = text
        self.usage_metadata = usage


class _Models:
    """Records what it was called with. Answers with whatever a test needs."""

    def __init__(
        self,
        *,
        response: _Response | None = None,
        chunks: tuple[_Response, ...] = (),
        error: Exception | None = None,
    ) -> None:
        self._response = response or _Response("{}")
        self._chunks = chunks
        self._error = error
        self.calls: list[dict[str, Any]] = []

    async def generate_content(self, *, model: str, contents: Any, config: Any) -> _Response:
        self.calls.append({"model": model, "contents": contents, "config": config})
        if self._error is not None:
            raise self._error
        return self._response

    async def generate_content_stream(
        self, *, model: str, contents: Any, config: Any
    ) -> AsyncIterator[_Response]:
        self.calls.append({"model": model, "contents": contents, "config": config})
        if self._error is not None:
            raise self._error
        chunks = self._chunks

        async def _iter() -> AsyncIterator[_Response]:
            for chunk in chunks:
                yield chunk

        return _iter()


class _Aio:
    def __init__(self, models: _Models) -> None:
        self.models = models


class _StubGenAI:
    """Shaped like ``genai.Client``: the async surface hangs off ``.aio``."""

    def __init__(self, models: _Models) -> None:
        self.aio = _Aio(models)


def _client(models: _Models, **overrides: Any) -> VertexModelClient:
    kwargs: dict[str, Any] = {
        "project_id": "p",
        "location": "us-central1",
        "model": "gemini-3.5-flash",
        "client": _StubGenAI(models),
    }
    kwargs.update(overrides)
    return VertexModelClient(**kwargs)


# ------------------------------------------------------- the call arguments


async def test_the_call_names_the_configured_model() -> None:
    models = _Models(response=_Response("hello"))
    client = _client(models)

    text, tokens = await client._call("prompt", None)

    assert text == "hello"
    assert models.calls[0]["model"] == "gemini-3.5-flash"
    assert models.calls[0]["contents"] == "prompt"


async def test_a_response_schema_asks_for_json() -> None:
    """A schema without the JSON mime type is a schema the model may ignore."""
    models = _Models(response=_Response("{}"))
    client = _client(models)

    await client._call("prompt", {"type": "object"})

    config = models.calls[0]["config"]
    assert config["response_mime_type"] == "application/json"
    assert config["response_schema"] == {"type": "object"}


async def test_no_schema_means_no_json_constraint() -> None:
    models = _Models(response=_Response("prose"))
    client = _client(models)

    await client._call("prompt", None)

    assert "response_schema" not in models.calls[0]["config"]
    assert "response_mime_type" not in models.calls[0]["config"]


async def test_generation_is_deterministic() -> None:
    """Temperature zero, always. A brief that varies per call is not evidence."""
    models = _Models()
    client = _client(models)

    await client._call("prompt", None)

    assert models.calls[0]["config"]["temperature"] == 0.0


async def test_token_counts_reach_the_span() -> None:
    models = _Models(response=_Response("x", _Usage(prompt=11, completion=7, total=18)))
    client = _client(models)

    _, tokens = await client._call("prompt", None)

    assert tokens == {"prompt": 11, "completion": 7, "total": 18}


async def test_absent_usage_metadata_reads_as_zero_not_a_crash() -> None:
    """A missing counter is a telemetry gap, not a reason to fail a good call."""
    models = _Models(response=_Response("x", usage=None))
    client = _client(models)

    _, tokens = await client._call("prompt", None)

    assert tokens == {"prompt": 0, "completion": 0, "total": 0}


async def test_a_text_free_response_is_empty_string_not_none() -> None:
    """``.text`` is ``None`` on a safety block or a counts-only frame.

    The caller's parser decides what an empty answer means -- and already
    reports it as a rejection. Raising here would turn a refusal into a crash.
    """
    models = _Models(response=_Response(None))
    client = _client(models)

    text, _ = await client._call("prompt", None)

    assert text == ""


# ------------------------------------------------------------- which model


async def test_triage_calls_the_cheap_model_not_the_expensive_one() -> None:
    """Gemma decides whether Gemini runs. It has to actually be Gemma."""
    models = _Models(response=_Response('{"extract": true, "reason": "structural"}'))
    client = _client(models, triage_model="gemma-3-4b-it")

    result = await client.triage(
        document_text="a permit", schema_keys=("stories",), deadline_ms=500
    )

    assert result.extract is True
    assert models.calls[0]["model"] == "gemma-3-4b-it"


async def test_without_a_triage_model_the_sdk_is_never_reached() -> None:
    """No triage model configured means the local screen answers, free."""
    models = _Models()
    client = _client(models)

    result = await client.triage(
        document_text="a permit", schema_keys=("stories",), deadline_ms=500
    )

    assert result.extract is True
    assert result.accepted is False
    assert models.calls == []


async def test_extract_uses_the_expensive_model() -> None:
    models = _Models(
        response=_Response('{"values": [], "unknowns": ["stories"], "conflicts_noted": []}')
    )
    client = _client(models, triage_model="gemma-3-4b-it")

    await client.extract(
        document_text="a permit", schema_keys=("stories",), source_ref="ref", deadline_ms=500
    )

    assert models.calls[0]["model"] == "gemini-3.5-flash"


# ---------------------------------------------------------------- streaming


async def test_the_stream_yields_each_piece_in_order() -> None:
    models = _Models(chunks=(_Response("the "), _Response("attic "), _Response("conversion")))
    client = _client(models)

    pieces = [piece async for piece in client._stream("prompt", deadline_ms=5_000)]

    assert pieces == ["the ", "attic ", "conversion"]


async def test_the_stream_skips_frames_carrying_no_text() -> None:
    """A usage-only frame is not a fragment of prose."""
    models = _Models(chunks=(_Response("two "), _Response(None), _Response("storeys")))
    client = _client(models)

    pieces = [piece async for piece in client._stream("prompt", deadline_ms=5_000)]

    assert pieces == ["two ", "storeys"]


async def test_a_stream_past_its_deadline_raises_rather_than_hanging() -> None:
    """The deadline is the incident's, not the model's."""
    import asyncio

    class _SlowModels(_Models):
        async def generate_content_stream(
            self, *, model: str, contents: Any, config: Any
        ) -> AsyncIterator[_Response]:
            async def _iter() -> AsyncIterator[_Response]:
                await asyncio.sleep(5)
                yield _Response("too late")

            return _iter()

    client = _client(_SlowModels())

    with pytest.raises(UpstreamTimeoutError):
        async for _ in client._stream("prompt", deadline_ms=20):
            pass


async def test_a_failed_stream_ends_without_a_final_chunk() -> None:
    """The consumer withdraws provisional prose; nothing raises at it."""
    models = _Models(error=RuntimeError("transport gone"))
    client = _client(models)

    chunks = [
        chunk
        async for chunk in client.compose_stream(
            template_id="size-up", fields={"stories": 3}, max_chars=200, deadline_ms=1_000
        )
    ]

    assert chunks == []


async def test_a_completed_stream_ends_with_a_final_chunk() -> None:
    models = _Models(chunks=(_Response("lightweight truss"),))
    client = _client(models)

    chunks = [
        chunk
        async for chunk in client.compose_stream(
            template_id="size-up", fields={"stories": 3}, max_chars=200, deadline_ms=1_000
        )
    ]

    assert [c.text for c in chunks] == ["lightweight truss", ""]
    assert chunks[-1].final is True


async def test_the_stream_stops_at_the_callers_character_budget() -> None:
    """The cap belongs to the caller. A model that runs on is not listened to."""
    models = _Models(chunks=(_Response("abcde"), _Response("fghij")))
    client = _client(models)

    chunks = [
        chunk
        async for chunk in client.compose_stream(
            template_id="size-up", fields={}, max_chars=7, deadline_ms=1_000
        )
    ]

    assert "".join(c.text for c in chunks) == "abcdefg"


# ------------------------------------------------- the stub matches the SDK


def test_the_stub_matches_the_real_sdk_signature() -> None:
    """The installed SDK accepts exactly what the client passes it.

    Without this, the stub is free to drift: a renamed parameter keeps every
    other test in this file green and breaks only the first real call. Reading
    the signature makes no network call and needs no credentials, so an SDK
    upgrade that moves the seam fails here instead of in production.
    """
    genai_models = pytest.importorskip("google.genai.models")

    for method_name in ("generate_content", "generate_content_stream"):
        method = getattr(genai_models.AsyncModels, method_name)
        params = inspect.signature(method).parameters
        for passed in ("model", "contents", "config"):
            assert passed in params, f"{method_name} no longer accepts {passed}"
            assert params[passed].kind is inspect.Parameter.KEYWORD_ONLY


def test_the_client_reaches_vertex_rather_than_the_public_api() -> None:
    """``vertexai=True`` is the difference between a service account and a key.

    An API key is a credential that travels and that nothing can attribute. A
    municipal deployment has to be able to say which identity made a call and
    to revoke it, so this argument is not a preference.
    """
    genai = pytest.importorskip("google.genai")
    captured: dict[str, Any] = {}

    class _Recording(genai.Client):  # type: ignore[misc, name-defined]
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    original = genai.Client
    genai.Client = _Recording  # type: ignore[misc]
    try:
        VertexModelClient(project_id="proj", location="us-west1", model="m")._genai()
    finally:
        genai.Client = original  # type: ignore[misc]

    assert captured == {"vertexai": True, "project": "proj", "location": "us-west1"}
