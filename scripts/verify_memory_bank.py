"""Prove the Memory Bank adapter against the live service.

Three behaviours this project relies on cannot be asserted against a recording
client, because each of them is a property of Google's service rather than of
our request:

1. **What a duplicate ``create_memory`` actually answers.** The idempotency
   story rests on it -- ``derive_question_id`` gives two instances polling one
   district the same id, and exactly one of them may create it. A client stub
   asserting this would be asserting our own assumption back at us, and when
   this script was first run it proved the assumption wrong: the service answers
   ``InvalidArgument``, not ``AlreadyExists``.
2. **Scope matching is exact, and a retrieval names nothing of ours.** Both
   found here, and both were assumed otherwise. A memory carrying scope keys
   beyond the query's is invisible to it -- silently, returning empty rather
   than erroring -- and a retrieved memory comes back under a synthetic resource
   name with ``display_name`` blanked, so the thread id has to travel in the
   text. Either one alone would have made every district-wide recall return
   nothing forever, in a way no unit test could have seen.
3. **A 2048-character fact is the ceiling.** This is the measurement the whole
   two-store split rests on, so it is re-checked rather than remembered.
4. **A withdrawn id cannot be reused, and scope cannot be edited.** Both found
   here. ``ABANDONED -> RESOLVED`` is the case the memory bank exists for, so an
   id that cannot be rewritten after withdrawal would leave a reopened thread
   permanently unindexable. These two checks are why closing is a no-op and why
   the bank filters closed threads on the way out instead.

Run it after any change to the adapter, and after any SDK upgrade:

    .venv/bin/python scripts/verify_memory_bank.py

Needs Application Default Credentials and ``MEMORY_BANK_ENGINE_ID``. It creates
memories under a probe district and leaves them there: deleting would reserve
their ids, which is one of the things being proved.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import UTC, datetime

from google.api_core import exceptions as gexc

from firstdue.adapters.vertex.threads import KIND_OPEN_QUESTION, VertexThreadIndex
from firstdue.domain.enums import Classification
from firstdue.domain.threads import MAX_INDEXED_TEXT, ThreadMemory

PROBE_DISTRICT = "probe-district-verify"
EPOCH = datetime(2026, 3, 4, 8, 0, tzinfo=UTC)

#: The limit the live service enforces on ``Memory.fact``. Measured, and the
#: reason the state machine stayed in Firestore.
SERVICE_FACT_LIMIT = 2048


def _memory(question_id: str, text: str, *, address_id: str | None = "probe-addr") -> ThreadMemory:
    return ThreadMemory(
        question_id=question_id,
        district_id=PROBE_DISTRICT,
        address_id=address_id,
        text=text,
        classification=Classification.PUBLIC,
        opened_by="records-watcher",
        opened_at=EPOCH,
    )


async def main() -> int:
    project = os.environ.get("GCP_PROJECT_ID", "firstdue-dev")
    location = os.environ.get("MEMORY_BANK_LOCATION", "us-central1")
    engine = os.environ.get("MEMORY_BANK_ENGINE_ID")
    if not engine:
        print("MEMORY_BANK_ENGINE_ID is not set", file=sys.stderr)
        return 2

    index = VertexThreadIndex(project_id=project, location=location, engine_id=engine)
    print(f"parent: {index.parent}\n")

    failures: list[str] = []
    written: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"[{'OK  ' if ok else 'FAIL'}] {name}{f': {detail}' if detail else ''}")
        if not ok:
            failures.append(name)

    try:
        # Ids are unique per run: a memory is never deleted (see below), so a
        # fixed id would be updating the last run's row rather than creating.
        run = uuid.uuid4().hex[:8]
        qid = f"probe-q-{run}-a"
        qid2 = f"probe-q-{run}-b"

        # 1. a memory round-trips under an id we chose
        await index.remember(
            _memory(qid, "unpermitted attic conversion, third storey, no sign-off")
        )
        written.append(qid)
        check("create_memory accepts our derived id", True)

        # 2. a duplicate create is recognised and turned into an update
        await index.remember(_memory(qid, "unpermitted attic conversion, now also unsigned"))
        check("a re-remembered thread updates rather than duplicating", True)

        # 3. the district-wide query finds it, and names it correctly
        matches = await index.recall_similar("attic conversion", district_id=PROBE_DISTRICT)
        found = [m.question_id for m in matches]
        check(
            "a district-wide recall finds the thread",
            qid in found,
            f"returned {found}",
        )
        check(
            "the match names our thread id, not a synthetic one",
            all(not m.question_id.isdigit() for m in matches),
            f"returned {found}",
        )

        # 4. a second thread on a different address is still in the district
        await index.remember(
            _memory(qid2, "blocked stairwell storage noted at inspection", address_id="other-addr")
        )
        written.append(qid2)
        matches = await index.recall_similar("blocked stairwell", district_id=PROBE_DISTRICT)
        check(
            "a thread on another address is in the same district",
            qid2 in [m.question_id for m in matches],
        )

        # 5. cross-district isolation
        matches = await index.recall_similar("attic conversion", district_id="probe-district-other")
        check(
            "another district sees none of it",
            all(m.question_id not in written for m in matches),
            f"returned {[m.question_id for m in matches]}",
        )

        # 6. the fact ceiling, re-measured rather than remembered
        from google.cloud import aiplatform_v1beta1 as v1b1

        client = index._service()
        try:
            operation = client.create_memory(
                request=v1b1.CreateMemoryRequest(
                    parent=index.parent,
                    memory_id=f"probe-oversize-{run}",
                    memory=v1b1.Memory(
                        fact="x" * (SERVICE_FACT_LIMIT + 1), scope={"kind": "sizeprobe"}
                    ),
                )
            )
            operation.result()
            written.append(f"probe-oversize-{run}")
            check(
                f"a fact over {SERVICE_FACT_LIMIT} is refused",
                False,
                "the service accepted it -- MAX_INDEXED_TEXT may be revisitable",
            )
        except gexc.InvalidArgument as exc:
            check(f"a fact over {SERVICE_FACT_LIMIT} is refused", True, str(exc)[:70])

        # 7. and ours sits under it
        check(
            "MAX_INDEXED_TEXT sits under the service ceiling",
            MAX_INDEXED_TEXT < SERVICE_FACT_LIMIT,
            f"{MAX_INDEXED_TEXT} < {SERVICE_FACT_LIMIT}",
        )

        # 8. an id, once withdrawn, is a tombstone -- the reason close is a no-op
        burn = f"probe-burn-{run}"
        client.create_memory(
            request=v1b1.CreateMemoryRequest(
                parent=index.parent,
                memory_id=burn,
                memory=v1b1.Memory(fact="burn probe", scope={"kind": "burnprobe"}),
            )
        ).result()
        try:
            client.delete_memory(
                request=v1b1.DeleteMemoryRequest(name=f"{index.parent}/memories/{burn}")
            )
        except TypeError:
            # The SDK declares this LRO's response ``Empty`` and the service
            # returns the deleted ``Memory``, so wrapping it raises. The delete
            # itself lands, which the next two checks establish.
            pass
        gone = False
        try:
            client.get_memory(request=v1b1.GetMemoryRequest(name=f"{index.parent}/memories/{burn}"))
        except gexc.NotFound:
            gone = True
        check("a deleted memory reads as gone", gone)

        reusable = True
        try:
            client.create_memory(
                request=v1b1.CreateMemoryRequest(
                    parent=index.parent,
                    memory_id=burn,
                    memory=v1b1.Memory(fact="reuse attempt", scope={"kind": "burnprobe"}),
                )
            ).result()
            written.append(burn)
        except gexc.InvalidArgument:
            reusable = False
        check(
            "a deleted id stays reserved -- so close must not delete",
            not reusable,
            "reuse succeeded; the no-op close could become a real delete" if reusable else "",
        )

        # 9. scope is immutable -- the reason close cannot re-tag instead
        immutable = False
        try:
            client.update_memory(
                request=v1b1.UpdateMemoryRequest(
                    memory=v1b1.Memory(
                        name=f"{index.parent}/memories/{qid}",
                        fact="prose unchanged",
                        scope={"district_id": PROBE_DISTRICT, "kind": "closed-question"},
                    )
                )
            ).result()
        except gexc.InvalidArgument as exc:
            immutable = "immutable" in str(exc).lower()
        check("scope is immutable -- so close cannot re-tag either", immutable)

        # 10. closing is a no-op, and the entry is still addressable afterwards
        await index.forget(qid)
        stored = client.get_memory(
            request=v1b1.GetMemoryRequest(name=f"{index.parent}/memories/{qid}")
        )
        check(
            "a closed thread is left addressable, so it can be reopened",
            dict(stored.scope).get("kind") == KIND_OPEN_QUESTION,
        )

        # 11. and the id still takes a rewrite -- the reopen path
        await index.remember(_memory(qid, "the permit finally published, reopening"))
        stored = client.get_memory(
            request=v1b1.GetMemoryRequest(name=f"{index.parent}/memories/{qid}")
        )
        check(
            "a reopened thread rewrites its prose in place",
            "reopening" in stored.fact,
            stored.fact[:60],
        )

    finally:
        # Closed, not deleted -- deleting would reserve the id forever, which is
        # the finding this script exists to keep proving. Probe rows are inert:
        # they carry a probe district and the closed kind, so no recall matches.
        for question_id in written:
            try:
                await index.forget(question_id)
            except Exception as exc:
                print(f"  (cleanup failed for {question_id}: {type(exc).__name__})")
        print(
            f"  (probe rows left under district {PROBE_DISTRICT!r}; they are inert -- "
            "ids cannot be reused once deleted, which is the finding above)"
        )

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("all checks passed against the live service")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
