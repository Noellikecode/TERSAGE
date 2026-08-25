# ADR 0010 — The managed Memory Bank holds prose; the record stays in Firestore

**Status:** accepted (2026-08-24)

Amends [ADR 0006](0006-one-contract-two-backends.md).

## Context

The Fortified Enterprise Fleet track names **Memory Bank** as a platform
component, and until now this project implemented its own over Firestore and
said so. The intent of this change was to replace ours with Vertex AI Agent
Engine Memory Bank outright: adopt the named product, delete the substitute.

The SDK made that look straightforward. `CreateMemoryRequest` accepts a
client-supplied `memory_id`, so our derived `derive_question_id` would carry
over; `ListMemoriesRequest` takes a filter; `Memory` carries a `scope` map to
narrow on. Every shape we needed appeared to be there.

Then we ran it against the live service.

## What the service actually does

Five behaviours, each measured on `firstdue-dev` and each different from what
the message shapes implied:

1. **`Memory.fact` is capped at 2048 characters.** Longer is refused with
   `InvalidArgument`.
2. **A duplicate create answers `InvalidArgument`, not `AlreadyExists`** — and
   the same status code refuses an oversize fact, so the two have to be told
   apart by message.
3. **Scope matching is exact, not subset.** A memory scoped
   `{district, kind, address, opened_by}` is invisible to a query scoped
   `{district, kind}`, and comes back empty rather than erroring.
4. **A retrieval names nothing of ours.** `retrieve_memories` returns a
   synthetic resource name and blanks `display_name` and `description`. Only
   `fact`, `scope` and `distance` survive.
5. **Deleting a memory reserves its id permanently.** `get_memory` answers
   `NotFound` while `create_memory` on the same id still answers *already
   exists*.

Two further facts shape the design. Writing a memory **embeds it** server-side
under the Reasoning Engine service agent, so it is not a key-value write and it
needs prediction rights. And `generate_memories` — the path the product leads
with — has a model decide what is worth remembering.

## Decision

**The record does not move.** An `OpenQuestion` is a state machine: eliminations
that accumulate across passes, evidence ids, an examination count, transitions
including `ABANDONED -> RESOLVED`, and graph checkpoints up to
`MAX_CHECKPOINT_STATE_BYTES`. Measured against a real district's shapes, a
long-running thread serialises past 2048 characters and a busy pass's checkpoint
well past it. A store whose writes begin failing once a thread is old enough to
matter is worse than the one we had, because the failure arrives exactly where
the component earns its keep.

**The prose does move.** A question's text and what it waits on are bounded by
`MAX_MEMORY_TEXT` at 400 characters each, fit the ceiling with room to spare,
and are the thing a managed memory service is genuinely good at: retrieval by
meaning. That buys a query `list_open` cannot answer — *has anyone asked
something like this* — which is what a watcher wants before opening a thread
another agent is already waiting on.

**Consequences of the five findings, in the code:**

- Scope is exactly `{district_id, kind}` — the query, and nothing more (3).
- The `question_id` is prefixed into the fact and parsed back out, because there
  is no other channel that survives a retrieval (4).
- A taken id is recognised from the message, narrowly, so an oversize fact is
  never mistaken for one (1, 2).
- `forget` is a documented no-op on this adapter. Deleting would burn the id,
  and `ABANDONED -> RESOLVED` is the case this component exists for; scope is
  immutable so it cannot be re-tagged either (3, 5). Closed threads are filtered
  out by the bank, against the record, and `RECALL_OVERFETCH` pays for the
  dilution.
- `PHI` and `TIER_II_CONFIDENTIAL` prose never reaches the adapter, because a
  write embeds it.
- `generate_memories` is never called. A model may route, resolve, compose and
  point, and may not author — that rule does not bend for a managed product.

**Authorization does not move either.** A match is an id and a distance. The
bank reads the stored question and applies the same scope gate a structural
recall applies, so the index is never trusted to decide who may see what.

## Consequences

The credential-free demo is unaffected: `InMemoryThreadIndex` is a real second
implementation of the same port, and `MEMORY_BANK_ENGINE_ID` unset selects it.
Recall is then per-instance and non-durable, which is the honest difference
between the two and the reason the managed one exists.

The Agent Engine instance is created **out of band**, because the Google
provider has no `google_vertex_ai_reasoning_engine` resource — checked against
the pinned provider's schema. `modules/memory-bank` creates the one IAM grant
the embedding call needs and takes the id as a variable, the same shape secret
*values* already have.

Closed threads are never removed from the managed bank. It holds nothing that
may not be there, but this is not an erasure path, and that is stated rather
than left to be discovered.

`scripts/verify_memory_bank.py` runs thirteen checks against the live service.
It exists because every one of the five findings above was invisible to a test
with a mocked client — a mock asserts your own assumptions back at you, and ours
were wrong five times out of five.
