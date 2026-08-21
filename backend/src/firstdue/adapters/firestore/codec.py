"""How a domain model becomes a Firestore document.

**The model is stored as one canonical JSON string, with a few fields lifted out
for querying.** That is a deliberate choice, and it buys three things:

* *Round-trip fidelity.* ``model_dump(mode="json")`` then ``model_validate``
  reproduces the object exactly, including tuples, frozensets, discriminated
  unions, and timezone-aware datetimes. A native Firestore map does not: it
  returns lists for tuples and its own datetime subclass for timestamps, so
  equality after a round trip becomes a thing you hope for.
* *Structures Firestore cannot hold.* Firestore rejects nested arrays, and a
  building footprint is a tuple of coordinate pairs. Encoding to a native map
  would mean reshaping the domain to suit the database.
* *Byte-identical replay.* The stored bytes are the same canonical JSON the demo
  seed hashes, so a document restored from Firestore hashes to what it hashed
  when written.

The cost is that only the lifted index fields are queryable, and a document is
capped at 1 MiB. Both are recorded in ``docs/build-notes.md``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Final, TypeVar

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from firstdue.errors import ValidationError

M = TypeVar("M", bound=BaseModel)

#: The field holding the canonical JSON payload.
PAYLOAD_FIELD: Final[str] = "payload"
#: Schema marker, so a future migration can recognise old documents.
DOC_SCHEMA_FIELD: Final[str] = "doc_schema"
DOC_SCHEMA_VERSION: Final[int] = 1
#: Firestore's hard per-document limit.
MAX_DOCUMENT_BYTES: Final[int] = 1_048_576


def encode(model: BaseModel, **index: Any) -> dict[str, Any]:
    """Encode a model plus its queryable index fields.

    Index fields are a *copy* of data already inside the payload. The payload is
    the source of truth; an index field that drifts is a query bug, never a
    change to what the system believes.
    """
    payload = json.dumps(model.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    encoded = payload.encode("utf-8")
    if len(encoded) > MAX_DOCUMENT_BYTES:
        raise ValidationError(
            "document exceeds the Firestore size limit",
            details={"bytes": len(encoded), "max": MAX_DOCUMENT_BYTES},
        )
    document: dict[str, Any] = {
        DOC_SCHEMA_FIELD: DOC_SCHEMA_VERSION,
        PAYLOAD_FIELD: payload,
    }
    document.update({k: v for k, v in index.items() if v is not None})
    return document


def decode(model_type: type[M], document: Mapping[str, Any]) -> M:
    """Decode a document back into its model, re-checking every invariant.

    Rehydration goes through ``model_validate``, so state that would fail
    validation cannot be read out of the database any more than it could be
    written into it.
    """
    raw = document.get(PAYLOAD_FIELD)
    if not isinstance(raw, str):
        raise ValidationError(
            "stored document has no payload",
            details={"model": model_type.__name__},
        )
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationError(
            "stored document payload is not valid JSON",
            details={"model": model_type.__name__},
        ) from exc
    try:
        return model_type.model_validate(parsed)
    except PydanticValidationError as exc:
        raise ValidationError(
            "stored document failed validation on read",
            details={
                "model": model_type.__name__,
                "fields": sorted({str(e["loc"][0]) for e in exc.errors() if e["loc"]}),
            },
        ) from exc


def decode_all(model_type: type[M], documents: list[dict[str, Any]]) -> list[M]:
    return [decode(model_type, document) for document in documents]
