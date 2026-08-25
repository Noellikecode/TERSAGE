"""The central database: the municipality's own records, as a department holds them.

Everything the slow loop reads about a *building* -- permits, inspections,
violations, hazardous-materials filings, the assessor's roll -- and everything
the department knows about *itself* -- prior incidents at an address, the
companies that responded, the apparatus they brought -- lives here, in
Firestore, in one place an agent can poll.

**Why it is synthetic, and why that is stated rather than hidden.** No
hackathon team has a fire department's records system. The public half of this
data exists (San Francisco publishes permits and violations) and the build reads
it live where it does; the half that matters most operationally -- what an
inspector wrote in a narrative, which company caught the last fire on that
block, what is stored in the basement under EPCRA -- is not public, by statute
in two cases. So it is generated: deterministically, over *real* parcels, at a
volume a district actually produces, and marked as generated in every record it
writes.

**What it is not.** It is not a shortcut past the agents. The corpus is *input*
-- raw records with untrusted narrative text -- not answers. A permit here says
"CONVERT ATTIC TO HABITABLE SPACE, ADD DORMER" in the prose an applicant typed;
it does not say ``structure.stories = 3``. Turning the first into the second is
the extraction path's job, through the screens, the triage, the span binding and
the provenance rules, exactly as it would be against the real feed. Replacing a
pre-built profile seed with this is the difference between a demo that shows the
answer and one that does the work.

**The memory bank is part of it.** ``open_questions`` and ``memory_checkpoints``
are collections in this same database. That is not a filing convenience: a
question a watcher opened in March because a permit cited a filing that had not
published yet is department knowledge in the same sense the permit is, and it is
what makes the loop cumulative rather than repetitive. An agent reads the
corpus, fails to settle something, writes down what it is waiting for, and the
next pass starts from there.
"""

from __future__ import annotations

from firstdue.central.corpus import (
    CENTRAL_COLLECTIONS,
    CORPUS_VERSION,
    CentralCorpus,
    build_corpus,
    collection_for_source,
)

__all__ = [
    "CENTRAL_COLLECTIONS",
    "CORPUS_VERSION",
    "CentralCorpus",
    "build_corpus",
    "collection_for_source",
]
