"""The base every document written into the incident log shares.

Three of the artifacts in this package -- the readiness assessment, the entry
path and the crew brief -- are persisted verbatim inside a log entry and read
back out of it later, and all three carry **computed fields**: ``ready``,
``entry_face``, ``status`` and the rest. Those are on the wire deliberately. A
console that had to derive "is this ready" from six booleans, or find the entry
face by scanning waypoints for a node kind, is a console that can disagree with
the document it is rendering.

But a computed field is output-only. It appears in ``model_dump`` and pydantic
refuses it on the way back in, and these models are ``extra="forbid"`` -- which
is the right setting and the reason a round trip through the log would otherwise
fail with three validation errors about fields the model itself wrote.

So a document drops its own computed field names before validating. Not by
loosening ``extra``, which would let a genuinely unexpected key through
unnoticed, and not by naming them in an exclude list at every dump site, which
is the same statement made in more places than can stay in step. The names come
from ``model_computed_fields``, so adding a computed field to a document needs
nothing else changed anywhere.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator


class RecordedDocument(BaseModel):
    """Frozen, strict, and safe to read back out of the log it was written to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _drop_computed(cls, data: Any) -> Any:
        """Ignore the fields this model derives. They are recomputed, never read.

        Only for mappings: a value that is already an instance of the model, or
        anything else pydantic knows how to coerce, passes through untouched.
        """
        if isinstance(data, dict) and cls.model_computed_fields:
            derived = set(cls.model_computed_fields)
            return {key: value for key, value in data.items() if key not in derived}
        return data


__all__ = ["RecordedDocument"]
