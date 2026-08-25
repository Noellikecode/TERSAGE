"""The Vertex AI Memory Bank adapter, against a recording client.

No credentials and no network: the client is injected, so what these assert is
the shape of the requests the adapter builds and the branches it takes. The
behaviours that can only be proved against the live service -- what a duplicate
create actually answers, whether partial-scope retrieval matches, whether a
withdrawn id can be reused -- are proved by ``scripts/verify_memory_bank.py``,
and the reasons they cannot be asserted here are in that script's docstring.
Three of the assertions below exist because that script contradicted what this
file originally assumed.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from firstdue.domain.enums import Classification
from firstdue.domain.threads import ThreadMemory
from firstdue.errors import ClassificationViolationError, ConfigurationError
from firstdue.ports.threads import ThreadIndex

pytestmark = pytest.mark.anyio

v1b1 = pytest.importorskip("google.cloud.aiplatform_v1beta1")
gexc = pytest.importorskip("google.api_core.exceptions")

from firstdue.adapters.vertex.threads import (  # noqa: E402
    KIND_OPEN_QUESTION,
    VertexThreadIndex,
)

EPOCH = datetime(2026, 3, 4, 8, 0, tzinfo=UTC)


class _Operation:
    def __init__(self, value: object = None) -> None:
        self._value = value

    def result(self) -> object:
        return self._value


class _RecordingClient:
    """Records requests. Raises whatever it was told to raise, once."""

    def __init__(self, *, create_raises: Exception | None = None, retrieved=()) -> None:
        self.created: list[object] = []
        self.updated: list[object] = []
        self.deleted: list[object] = []
        self.retrieved_requests: list[object] = []
        self._create_raises = create_raises
        self._retrieved = retrieved

    def create_memory(self, *, request):
        self.created.append(request)
        if self._create_raises is not None:
            raise self._create_raises
        return _Operation()

    def update_memory(self, *, request):
        self.updated.append(request)
        return _Operation()

    def delete_memory(self, *, request):
        self.deleted.append(request)
        return _Operation()

    def retrieve_memories(self, *, request):
        self.retrieved_requests.append(request)
        response = v1b1.RetrieveMemoriesResponse()
        for name, scope, distance, fact in self._retrieved:
            response.retrieved_memories.append(
                v1b1.RetrieveMemoriesResponse.RetrievedMemory(
                    memory=v1b1.Memory(name=name, scope=scope, fact=fact), distance=distance
                )
            )
        return response


def _index(client) -> VertexThreadIndex:
    return VertexThreadIndex(
        project_id="firstdue-dev",
        location="us-central1",
        engine_id="4054090136877531136",
        client=client,
    )


def _memory(**overrides) -> ThreadMemory:
    payload = {
        "question_id": "q-abc123",
        "district_id": "sf-d7",
        "address_id": "addr-1",
        "text": "does permit 201804-3321 exist -- waiting on: sf-permits publication",
        "classification": Classification.PUBLIC,
        "opened_by": "records-watcher",
        "opened_at": EPOCH,
    }
    payload.update(overrides)
    return ThreadMemory(**payload)


def test_the_adapter_satisfies_the_port() -> None:
    assert isinstance(_index(_RecordingClient()), ThreadIndex)


def test_an_engine_id_is_required() -> None:
    """An adapter that built a parent path out of an empty string would 404 later."""
    with pytest.raises(ConfigurationError):
        VertexThreadIndex(project_id="firstdue-dev", location="us-central1", engine_id="")


def test_the_parent_is_the_agent_engine_instance() -> None:
    index = _index(_RecordingClient())
    assert index.parent == (
        "projects/firstdue-dev/locations/us-central1/reasoningEngines/4054090136877531136"
    )


async def test_a_memory_carries_our_derived_id_not_a_generated_one() -> None:
    """Reopening addresses the same memory instead of adding a near-duplicate."""
    client = _RecordingClient()

    await _index(client).remember(_memory())

    assert client.created[0].memory_id == "q-abc123"


async def test_the_stored_scope_is_exactly_the_queried_scope() -> None:
    """Scope matching is exact, so a richer scope is an invisible memory.

    Verified live: a memory carrying extra scope keys is returned by *no* query
    that does not name every one of them. Storing address or opening agent here
    would have made every district-wide recall return empty, silently.
    """
    client = _RecordingClient()
    index = _index(client)

    await index.remember(_memory(address_id="addr-1"))
    await index.recall_similar("anything", district_id="sf-d7")

    stored = dict(client.created[0].memory.scope)
    queried = dict(client.retrieved_requests[0].scope)
    assert stored == {"district_id": "sf-d7", "kind": KIND_OPEN_QUESTION}
    assert stored == queried


async def test_an_address_scoped_thread_is_stored_under_its_district_only() -> None:
    """The address is on the record; the bank reads it there."""
    client = _RecordingClient()

    await _index(client).remember(_memory(address_id=None))

    assert dict(client.created[0].memory.scope) == {
        "district_id": "sf-d7",
        "kind": KIND_OPEN_QUESTION,
    }


async def test_the_thread_id_is_carried_in_the_stored_text() -> None:
    """Retrieval blanks the resource name and the display name; text is all there is."""
    client = _RecordingClient()

    await _index(client).remember(_memory())

    assert client.created[0].memory.fact.startswith("[q:q-abc123] ")


async def test_reopening_an_indexed_thread_updates_it_rather_than_failing() -> None:
    """``waiting_on`` moves as a thread ages; a stale entry would mislead a search."""
    client = _RecordingClient(create_raises=gexc.AlreadyExists("exists"))

    await _index(client).remember(_memory(text="now waiting on something else"))

    assert len(client.updated) == 1
    assert client.updated[0].memory.fact == "[q:q-abc123] now waiting on something else"
    assert client.updated[0].memory.name.endswith("/memories/q-abc123")


async def test_a_taken_id_is_recognised_from_an_invalid_argument() -> None:
    """What the live service actually answers, which is not ``AlreadyExists``.

    Pinned because the adapter caught the wrong status code until a live run
    proved it, and nothing short of the real service would have said so.
    """
    client = _RecordingClient(
        create_raises=gexc.InvalidArgument(
            "Memory with user-provided ID '.../memories/q-abc123' already exists."
        )
    )

    await _index(client).remember(_memory(text="reopened"))

    assert len(client.updated) == 1


async def test_an_oversize_fact_is_not_mistaken_for_a_taken_id() -> None:
    """The other ``InvalidArgument``, and the one that must not become an update.

    The service refuses a fact over its 2048-character ceiling with the same
    status code a taken id produces. Treating this one as "already exists" would
    turn a rejected write into a silent overwrite of whatever was there before.
    """
    client = _RecordingClient(
        create_raises=gexc.InvalidArgument("Fact length must be less than 2048 characters.")
    )

    with pytest.raises(gexc.InvalidArgument):
        await _index(client).remember(_memory())

    assert client.updated == []


async def test_a_forbidden_classification_never_reaches_the_service() -> None:
    """Writing a memory embeds it, so this gate is a statutory one."""
    client = _RecordingClient()
    smuggled = ThreadMemory.model_construct(
        question_id="q-abc123",
        district_id="sf-d7",
        address_id=None,
        text="tier two chemical inventory",
        classification=Classification.TIER_II_CONFIDENTIAL,
        opened_by="hazard-watcher",
        opened_at=EPOCH,
    )

    with pytest.raises(ClassificationViolationError):
        await _index(client).remember(smuggled)

    assert client.created == []


async def test_retrieval_narrows_by_district_at_the_service() -> None:
    """Not by filtering afterwards: a cross-district match is a jurisdiction leak."""
    client = _RecordingClient()

    await _index(client).recall_similar("attic conversion", district_id="sf-d7", limit=3)

    request = client.retrieved_requests[0]
    assert dict(request.scope) == {"district_id": "sf-d7", "kind": KIND_OPEN_QUESTION}
    assert request.similarity_search_params.search_query == "attic conversion"
    assert request.similarity_search_params.top_k == 3


async def test_a_match_is_read_out_of_the_tag_not_the_resource_name() -> None:
    """The name the service returns is synthetic and names nothing of ours."""
    client = _RecordingClient(
        retrieved=[
            (
                "projects/p/locations/l/reasoningEngines/e/memories/1356909292703186944",
                {"district_id": "sf-d7", "kind": KIND_OPEN_QUESTION},
                0.21,
                "[q:q-abc123] does permit 201804-3321 exist",
            )
        ]
    )

    matches = await _index(client).recall_similar("attic conversion", district_id="sf-d7")

    assert len(matches) == 1
    assert matches[0].question_id == "q-abc123"
    assert matches[0].district_id == "sf-d7"
    assert matches[0].distance == pytest.approx(0.21)


async def test_an_untagged_row_is_dropped_rather_than_guessed_at() -> None:
    """A match that cannot name a thread points at nothing."""
    client = _RecordingClient(
        retrieved=[
            ("projects/p/l/e/memories/99", {"district_id": "sf-d7"}, 0.1, "no tag here"),
            ("projects/p/l/e/memories/98", {"district_id": "sf-d7"}, 0.2, "[q:q-real] tagged"),
        ]
    )
    index = _index(client)

    matches = await index.recall_similar("anything", district_id="sf-d7")

    assert [m.question_id for m in matches] == ["q-real"]
    assert index.untagged == 1


async def test_closing_a_thread_touches_nothing_at_the_service() -> None:
    """The service offers no retraction that keeps the id usable.

    Deleting reserves the id forever and scope is immutable -- both verified
    against the live service. So closing is a no-op here, and the guarantee that
    a closed thread cannot reach a caller lives in the bank, which re-reads every
    match against the record.
    """
    client = _RecordingClient()
    index = _index(client)

    await index.forget("q-abc123")

    assert client.deleted == [], "deleting would reserve the id forever"
    assert client.updated == [], "scope is immutable; rewriting fact would destroy the record"


async def test_retrieval_still_names_the_open_kind() -> None:
    """The scope marks our rows. It is set once at create and never changes."""
    client = _RecordingClient()

    await _index(client).recall_similar("anything", district_id="sf-d7")

    assert dict(client.retrieved_requests[0].scope)["kind"] == KIND_OPEN_QUESTION


def test_the_adapter_never_reaches_the_model_authored_memory_path() -> None:
    """``generate_memories`` has a model decide what is worth remembering.

    That is the one capability this project refuses everywhere: a model may
    route, resolve, compose and point, and may not author. Every memory here is
    prose an agent already wrote into a question the deterministic path opened,
    so the generation surface must be unreachable -- asserted against the source
    rather than trusted, because a later edit reaching for it would look
    perfectly reasonable in isolation.
    """
    from pathlib import Path

    import firstdue.adapters.vertex.threads as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    calls = [
        line
        for line in source.splitlines()
        if "generate_memories" in line and not line.lstrip().startswith(("#", "*", '"'))
    ]
    assert calls == [], calls


def test_a_real_derived_question_id_becomes_a_legal_memory_id() -> None:
    """The service refuses underscores, and every derived id has one.

    ``derive_question_id`` emits ``mq_<hex>``. The live verification missed this
    because its probe ids were hand-written and happened to be hyphenated, so
    the first real slow-loop pass on Cloud Run rejected every single write. The
    id used here is therefore derived, not invented.
    """
    import re

    from firstdue.adapters.vertex.threads import memory_id_for
    from firstdue.domain.memory import derive_question_id

    question_id = derive_question_id(
        district_id="sffd-district-03",
        address_id="sf-0450-hayes",
        opened_by="hazard-watcher",
        question="is ACME PLATING INC at this parcel",
    )
    assert "_" in question_id, "the format this guards against has changed"

    legal = re.compile(r"^[a-z][a-z0-9-]*[a-z0-9]$")
    assert legal.match(memory_id_for(question_id)), memory_id_for(question_id)


def test_distinct_threads_keep_distinct_memory_ids() -> None:
    """An address is a name, not a hash: collapsing two threads would merge them."""
    from firstdue.adapters.vertex.threads import memory_id_for

    assert memory_id_for("mq_aaa111") != memory_id_for("mq_bbb222")
