# ADR 0008 — A filed column is not an extraction, and a negated phrase is not an assertion

**Status:** accepted (phase 3)

## Context

Two bugs found while building the records watcher, both of the same family: the
extraction path treating text as though it meant the opposite of what it said.

**The span rule was keyed on the wrong thing.** Phase 1 required a source span
whenever an agent produced a value from a document-shaped source. That is right
for a storey count a model read out of an inspection narrative and wrong for a
storey count sitting in a permit dataset column — there is no line to point at,
because nobody read prose. The watcher could not write a filed column at all.

**A word match ignored its own sentence.** The inspection fixture says *"No
sprinkler system on file for this structure."* The extractor matched the word
`sprinkler` and wrote `suppression.sprinklered = yes`. On the one attribute a
crew stakes an interior attack on, the system asserted the opposite of the
document.

## Decision

**Extraction is a flag, not an inference from source type.**
`StructuralFact.extracted_by_model` is set by whoever built the fact, and the
span requirement keys on it. The rule got stricter — a model-extracted value now
needs a span whatever the source — and more accurate: a column is a filing.

**Negation drops the candidate.** `coerce_value` takes the 60 characters
preceding the match. If they negate it, the candidate is dropped and the
attribute stays unestablished.

Dropping rather than inverting is deliberate. "No sprinkler system **on file**"
is a statement about the file, not about the building. Writing `false` would
claim somebody looked and found none; writing `true` inverts the document.
Writing nothing leaves it UNKNOWN, which is what is actually true.

## Consequences

- The demo's 450 Hayes profile has no `suppression.sprinklered` fact at all, and
  the pre-incident plan prints it under unknowns. That is the correct output.
- Negation detection is a 40-character window and a word list. It will miss
  constructions it has never seen. It is a floor, not a guarantee, and the
  guarantee remains the one the architecture provides: a model may not fill an
  UNKNOWN, and a human survey is what settles an attribute.
- `DOCUMENT_SOURCES` is now documentation of where prose lives rather than an
  enforcement mechanism.
- Tested in `tests/unit/test_ranker_and_extraction.py` and asserted end to end
  in `tests/integration/test_slow_loop_demo.py`.
