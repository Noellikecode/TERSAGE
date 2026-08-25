"""Vertex AI Agent Engine Memory Bank, holding the prose half of a thread.

The named platform component, adopted for the one job it is actually built for:
durable natural-language memory retrievable by meaning, scoped to a partition
the caller names. Everything a question *knows* -- its eliminations, its
evidence, its examination count, its transitions, its checkpoints -- stays in
the repositories behind :mod:`firstdue.ports.memory`, and the reasons are
recorded here because they were measured against the live service rather than
assumed.

**A ``Memory`` is a 2048-character fact, not a record.** Verified: the service
refuses a longer one with ``InvalidArgument``. A question's prose is bounded far
below that by :data:`~firstdue.domain.memory.MAX_MEMORY_TEXT`, so it fits with
room to spare -- but a serialized ``OpenQuestion`` carrying a long-running
thread's eliminations does not, and neither does a graph checkpoint on a busy
pass. A state machine whose writes start failing once a thread gets old enough
to matter is worse than one that never moved, so the state machine did not move.

**Writing a memory embeds it.** ``create_memory`` runs the fact through an
embedding model server-side; it is not a key-value write. Two consequences are
load-bearing. The service account needs prediction rights, which is why the
Reasoning Engine service agent is granted them in
``infra/terraform/modules/memory-bank``. And a classification that may never
reach an embedding model may never reach this adapter --
:class:`~firstdue.domain.threads.ThreadMemory` refuses ``PHI`` and
``TIER_II_CONFIDENTIAL`` at construction and :meth:`_guard` refuses them again
here, because the failure being prevented is a statutory one.

**``generate_memories`` is never called, and that is a design decision.** The
service offers a path that reads a conversation and has a model *write the
memories it thinks are worth keeping*. That is precisely the capability this
project refuses everywhere else: a model may route, resolve, compose and point,
and may not author. Every memory this adapter stores is prose an agent already
wrote into a question the deterministic path opened. The API surface used here
is ``create_memory``, ``retrieve_memories``, ``delete_memory`` -- no generation,
no consolidation.

**Ids are ours on the way in, and not on the way out.**
``CreateMemoryRequest`` accepts a client-supplied ``memory_id``, so a memory is
addressed by the same derived ``question_id`` its record is, and reopening the
same question rewrites that memory instead of accumulating near-duplicates that
would all match one query. Retrieval, however, hands back a synthetic name and
blanks ``display_name`` -- so the id has to travel in the text. See
:data:`_ID_TAG`. Server-assigned ``create_time``/``update_time`` are read by
nothing: a replay re-derives its timestamps from the
:class:`~firstdue.ports.clock.Clock`, and a field the service stamps could not
be replayed.

**Four of the behaviours above were wrong when this file was first written, and
the live verification is what corrected them.** The duplicate-create status
code, the exactness of scope matching, the reuse of a withdrawn id, and the
contents of a retrieval response were each assumed from the message shapes in
the SDK and each turned out otherwise. ``scripts/verify_memory_bank.py`` is what
found them and is what keeps them found.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Final

from firstdue.domain.enums import VECTOR_FORBIDDEN_CLASSIFICATIONS
from firstdue.domain.threads import ThreadMemory
from firstdue.errors import ClassificationViolationError, ConfigurationError
from firstdue.observability.logging import get_logger
from firstdue.observability.tracing import source_query_span, source_write_span
from firstdue.ports.threads import ThreadMatch

logger = get_logger(__name__)

#: Scope keys, and there are deliberately only two.
#:
#: The service stores a ``map<string, string>`` alongside each memory and
#: narrows retrieval on it -- but **the match is exact, not a subset**. Verified
#: live: a memory scoped ``{district, kind, address, opened_by}`` is invisible to
#: a query scoped ``{district, kind}``, and returns nothing at all rather than
#: erroring. An earlier version of this adapter stored the richer scope and
#: every district-wide recall would have silently returned empty forever.
#:
#: So the scope is exactly the query: the district a recall narrows to, and the
#: marker distinguishing our rows. Anything else about a thread is on the record
#: in Firestore, which the bank reads before it returns anything.
SCOPE_DISTRICT: Final[str] = "district_id"
SCOPE_KIND: Final[str] = "kind"

#: How a thread's id travels back from a retrieval.
#:
#: ``retrieve_memories`` returns a *synthetic* resource name -- not the
#: client-supplied ``memory_id`` that ``create_memory`` and ``list_memories``
#: both echo back -- and blanks ``display_name`` and ``description``. Verified
#: live. Of everything on a retrieved memory, only ``fact``, ``scope`` and
#: ``distance`` carry anything we wrote, and the scope has to stay exactly the
#: query. That leaves the text as the only channel, so the id is prefixed to it
#: and stripped on the way back.
#:
#: The cost is honest and small: the tag is part of what gets embedded, so it
#: contributes a little noise to similarity. It is kept short and punctuation-
#: heavy for that reason -- it tokenises to nearly nothing next to a sentence of
#: prose.
_ID_TAG = re.compile(r"^\[q:(?P<question_id>[^\]]{1,120})\]\s*(?P<prose>.*)$", re.DOTALL)

#: Marks our rows in a bank that may hold other things later. Retrieval narrows
#: on it, so it is also what takes a closed thread out of circulation.
KIND_OPEN_QUESTION: Final[str] = "open-question"


#: What the service says when a client-supplied ``memory_id`` is taken.
#:
#: It answers ``InvalidArgument`` -- *not* ``AlreadyExists``, which is what the
#: status code for this condition would ordinarily be and what this adapter
#: caught until the live verification proved otherwise. So the branch cannot key
#: on the status code alone, and matching on the message is the only thing left.
#:
#: Which makes the narrowness of the match load-bearing. ``InvalidArgument`` is
#: also how the service refuses a fact over its 2048-character ceiling, and
#: treating *that* as "already exists" would turn a rejected write into a silent
#: update of whatever was there before. The marker keeps the two apart.
_ALREADY_EXISTS_MARKER: Final[str] = "already exists"


def _reports_already_exists(exc: BaseException) -> bool:
    """Whether this failure means the id is taken, rather than the write is bad.

    See :data:`_ALREADY_EXISTS_MARKER` for why the message is consulted at all
    and why it must be consulted narrowly.
    """
    from google.api_core import exceptions as gexc

    if isinstance(exc, gexc.AlreadyExists):
        return True
    return _ALREADY_EXISTS_MARKER in str(exc).lower()


#: What the service accepts as a ``memory_id``: lowercase letters, digits and
#: hyphens, starting with a letter and ending alphanumerically.
#:
#: ``derive_question_id`` produces ``mq_<hex>`` -- an underscore the service
#: refuses outright, which the live verification missed because its probe ids
#: were written by hand and happened to be hyphenated. The first real slow-loop
#: pass on Cloud Run rejected every write. Asserting a format you invented for
#: the test is the same failure the rest of this module was written to avoid.
#:
#: Recovery is unaffected: the *real* ``question_id`` travels in the fact tag
#: (see :data:`_ID_TAG`), and this name is only an address. The mapping is
#: injective for anything ``derive_question_id`` emits, whose sole non-conforming
#: character is that one separator.
_MEMORY_ID_ALLOWED = re.compile(r"[^a-z0-9-]")


def memory_id_for(question_id: str) -> str:
    """The service-legal name a thread's memory is stored under."""
    slug = _MEMORY_ID_ALLOWED.sub("-", question_id.lower())
    if not slug[:1].isalpha():
        slug = f"q-{slug}"
    slug = slug.rstrip("-") or "q"
    return slug[:63]


class VertexThreadIndex:
    """Stores and recalls question prose in a Vertex AI Memory Bank."""

    def __init__(
        self,
        *,
        project_id: str,
        location: str,
        engine_id: str,
        client: Any | None = None,
    ) -> None:
        if not project_id or not engine_id:
            raise ConfigurationError(
                "Memory Bank requires GCP_PROJECT_ID and MEMORY_BANK_ENGINE_ID",
                details={"missing": "memory_bank_engine_id"},
            )
        self._project_id = project_id
        self._location = location
        self._engine_id = engine_id
        self._client = client
        self.remembered = 0
        self.recalls = 0
        self.forgotten = 0
        #: Retrieved rows whose text carried no id tag. See :data:`_ID_TAG`.
        self.untagged = 0

    @property
    def parent(self) -> str:
        """The Agent Engine instance the memories hang off."""
        return (
            f"projects/{self._project_id}/locations/{self._location}"
            f"/reasoningEngines/{self._engine_id}"
        )

    @staticmethod
    def _guard(memory: ThreadMemory) -> None:
        """The gate, checked again at the boundary it protects."""
        if memory.classification in VECTOR_FORBIDDEN_CLASSIFICATIONS:
            raise ClassificationViolationError(
                "this classification may never be handed to a managed recall index",
                details={
                    "classification": str(memory.classification),
                    "question_id": memory.question_id,
                },
            )

    def _service(self) -> Any:  # pragma: no cover - live mode only
        if self._client is None:
            try:
                from google.cloud import aiplatform_v1beta1
            except ImportError as exc:
                raise ConfigurationError(
                    "google-cloud-aiplatform is not installed; install the 'google' extra",
                    details={"package": "google-cloud-aiplatform"},
                ) from exc
            self._client = aiplatform_v1beta1.MemoryBankServiceClient(
                client_options={"api_endpoint": f"{self._location}-aiplatform.googleapis.com"}
            )
        return self._client

    @staticmethod
    def _scope(memory: ThreadMemory) -> dict[str, str]:
        """Exactly the keys a retrieval names. See :data:`SCOPE_DISTRICT`."""
        return {SCOPE_DISTRICT: memory.district_id, SCOPE_KIND: KIND_OPEN_QUESTION}

    @staticmethod
    def _tagged(memory: ThreadMemory) -> str:
        """The stored text: the thread's id, then its prose. See :data:`_ID_TAG`."""
        return f"[q:{memory.question_id}] {memory.text}"

    async def remember(self, memory: ThreadMemory) -> None:
        """Index one thread's prose, replacing the entry if it is already there."""
        self._guard(memory)
        with source_write_span(target="vertex-memory-bank") as span:
            span.set("question_id", memory.question_id)
            await self._write(memory)
            self.remembered += 1

    async def recall_similar(
        self, text: str, *, district_id: str, limit: int = 5
    ) -> tuple[ThreadMatch, ...]:
        """Threads in this district whose prose is nearest ``text``."""
        with source_query_span(source_id="vertex-memory-bank") as span:
            span.set("limit", limit)
            matches = await self._search(text, district_id=district_id, limit=limit)
            self.recalls += 1
            span.set("matches", len(matches))
            return matches

    async def forget(self, question_id: str) -> None:
        """Drop a thread from the index. Absent is not an error."""
        with source_write_span(target="vertex-memory-bank") as span:
            span.set("question_id", question_id)
            await self._delete(question_id)

    # --------------------------------------------------------------- live I/O

    async def _write(self, memory: ThreadMemory) -> None:  # pragma: no cover - live mode only
        from google.api_core import exceptions as gexc
        from google.cloud import aiplatform_v1beta1 as v1b1

        client = self._service()
        body = v1b1.Memory(
            fact=self._tagged(memory),
            scope=self._scope(memory),
            display_name=memory.question_id,
        )
        try:
            operation = await asyncio.to_thread(
                client.create_memory,
                request=v1b1.CreateMemoryRequest(
                    parent=self.parent,
                    memory_id=memory_id_for(memory.question_id),
                    memory=body,
                ),
            )
            await asyncio.to_thread(operation.result)
        except (gexc.AlreadyExists, gexc.InvalidArgument) as exc:
            if not _reports_already_exists(exc):
                raise
            # Reopening a thread. The record moved on -- ``waiting_on`` may have
            # changed -- so the indexed prose is updated rather than left stale.
            body.name = f"{self.parent}/memories/{memory_id_for(memory.question_id)}"
            operation = await asyncio.to_thread(
                client.update_memory, request=v1b1.UpdateMemoryRequest(memory=body)
            )
            await asyncio.to_thread(operation.result)

    async def _search(  # pragma: no cover - live mode only
        self, text: str, *, district_id: str, limit: int
    ) -> tuple[ThreadMatch, ...]:
        from google.cloud import aiplatform_v1beta1 as v1b1

        client = self._service()
        response = await asyncio.to_thread(
            client.retrieve_memories,
            request=v1b1.RetrieveMemoriesRequest(
                parent=self.parent,
                # Partial scope: every memory in the district, whatever address
                # it hangs off. The service matches the keys given, so naming
                # the district alone is the district-wide query.
                scope={SCOPE_DISTRICT: district_id, SCOPE_KIND: KIND_OPEN_QUESTION},
                similarity_search_params=v1b1.RetrieveMemoriesRequest.SimilaritySearchParams(
                    search_query=text, top_k=limit
                ),
            ),
        )
        matches: list[ThreadMatch] = []
        for retrieved in response.retrieved_memories:
            tag = _ID_TAG.match(retrieved.memory.fact or "")
            if tag is None:
                # Not one of ours, or written before the tag existed. A match
                # that cannot name a thread points at nothing, and guessing
                # which thread it meant is exactly the kind of invention this
                # port refuses -- so it is dropped and counted.
                self.untagged += 1
                logger.warning(
                    "memory_thread_match_untagged",
                    extra={"district_id": district_id},
                )
                continue
            matches.append(
                ThreadMatch(
                    question_id=tag.group("question_id"),
                    district_id=dict(retrieved.memory.scope).get(SCOPE_DISTRICT, district_id),
                    distance=float(retrieved.distance),
                )
            )
        return tuple(matches)

    async def _delete(self, question_id: str) -> None:
        """Deliberately a no-op. This service cannot un-index without burning the id.

        Two properties of the live service close off every other option, and
        both were found by running against it rather than by reading about it.

        **Deleting reserves the id forever.** After a successful
        ``delete_memory``, ``get_memory`` answers ``NotFound`` while
        ``create_memory`` on the same id still answers *already exists*. The id
        becomes a tombstone: unreadable and unwritable. That is fatal here
        specifically, because ``ABANDONED -> RESOLVED`` is a legal transition and
        the case the memory bank exists for -- a filing lands two months after
        everyone stopped waiting and the thread is picked back up. A deleted id
        would leave that reopened thread permanently unindexable, and silently,
        because indexing failures are counted rather than raised.

        **Scope is immutable.** The obvious alternative -- flip the entry's
        ``kind`` so retrieval stops matching it -- is refused outright:
        ``Immutable field `scope` can not be changed``. ``fact`` *is* mutable,
        but rewriting a thread's prose to something unmatchable would destroy
        the record of what it said in order to hide it, which is worse than
        leaving it.

        So closed threads stay in the bank, and **the guarantee moves to where it
        was already enforced**: :meth:`~firstdue.services.memory_bank.MemoryBank.recall_similar`
        reads every match back from the repository and drops anything that is not
        open, not visible to the caller, or expired. A closed thread was already
        unable to reach a caller through this path; what this method would have
        added is only that the index stops *offering* it.

        The cost is dilution rather than disclosure: as closed threads
        accumulate, more of a fixed ``top_k`` is spent on entries the bank will
        discard. That is paid for by over-fetching -- see
        :data:`~firstdue.services.memory_bank.RECALL_OVERFETCH` -- and it is the
        reason this is a documented no-op rather than a silent one.
        """
        logger.debug(
            "memory_thread_close_is_a_no_op",
            extra={"question_id": question_id, "reason": "memory bank ids cannot be reused"},
        )
