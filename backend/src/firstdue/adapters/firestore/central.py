"""Serving the central database to the slow loop, and loading it.

The agents do not know this exists. A watcher polls ``sf-permits`` and gets
records; whether those came from San Francisco's open-data endpoint, a JSON
fixture, or the department's own store in Firestore is a decision made once, in
:func:`~firstdue.sources.catalog.build_fetcher`, behind the
:class:`~firstdue.sources.framework.PageFetcher` seam. Everything above it --
the caching, the rate limiting, the circuit breaker, the screens, the triage,
the span binding -- is unchanged and untouched.

That is the whole reason this is a fetcher rather than a new path through the
watchers. A corpus the agents had to be *taught* to read would be a demo of the
corpus. A corpus they read through the same seam as a live feed is a demo of
the fleet.

**It reports ``FIXTURE``, not ``LIVE``.** The records are generated. The console
renders that verbatim, and a source backed by this says so on screen rather than
quietly passing for the real feed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from firstdue.adapters.firestore.client import FirestoreConfig, safe_document_id
from firstdue.central.corpus import CENTRAL_COLLECTIONS, CENTRAL_REFERENCE_COLLECTIONS
from firstdue.errors import SourceUnavailableError
from firstdue.observability.logging import get_logger
from firstdue.ports.sources import SourceMode, SourceRecord
from firstdue.sources.framework import RawPage

logger = get_logger(__name__)


class CentralDatabaseFetcher:
    """One central collection, paginated the way a feed would be."""

    def __init__(self, client: Any, config: FirestoreConfig, collection: str) -> None:
        if collection not in CENTRAL_COLLECTIONS:
            raise ValueError(f"{collection} is not a central source collection")
        self._client = client
        self._config = config
        self._collection = config.collection(collection)

    @property
    def mode(self) -> SourceMode:
        """Generated records, and the console says so."""
        return SourceMode.FIXTURE

    async def fetch_page(
        self,
        *,
        address_id: str | None,
        since: datetime | None,
        cursor: str | None,
        page_size: int,
    ) -> RawPage:
        """Return one page, narrowed by address and observation time.

        Ordered by ``record_ref`` rather than by time, because the cursor has to
        be stable: paginating a time-ordered query while records are being
        written would skip or repeat rows, and a backfill that silently skipped
        a permit is exactly the failure the snapshot id exists to rule out.
        """
        try:
            rows = await self._query(address_id, since, cursor, page_size)
        except Exception as exc:  # surfaced as UNAVAILABLE, never as an empty result
            logger.warning(
                "central_source_unreachable",
                extra={"collection": self._collection, "error_type": type(exc).__name__},
            )
            raise SourceUnavailableError(
                "the central database could not be read",
                details={"collection": self._collection},
            ) from exc

        records = tuple(SourceRecord.model_validate(row) for row in rows[:page_size])
        # A full page means there is probably more; the last ref is where the
        # next call resumes.
        next_cursor = records[-1].record_ref if len(rows) > page_size else None
        return RawPage(records=records, next_cursor=next_cursor)

    async def _query(
        self,
        address_id: str | None,
        since: datetime | None,
        cursor: str | None,
        page_size: int,
    ) -> list[dict[str, Any]]:
        from google.cloud.firestore_v1.base_query import FieldFilter

        query: Any = self._client.collection(self._collection)
        if address_id is not None:
            # Filtered *or* ordered, never both.
            #
            # Firestore needs a composite index for every filter/order pair, and
            # this collection is namespaced -- the contract suite and a local run
            # both prefix it -- so an index declared for `central_permits` does
            # not cover `local_central_permits` and the query fails on a
            # collection group nobody thought to declare. Ordering an
            # address-scoped read in Python costs nothing: a building has a
            # handful of permits, not a page of them.
            #
            # `DocumentStore.stream` made this same call for the same reason.
            query = query.where(filter=FieldFilter("address_id", "==", address_id))
        else:
            # The district-wide read is ordered at the server, which needs only
            # the single-field index Firestore maintains on its own.
            query = query.order_by("record_ref")
            if cursor:
                query = query.start_after({"record_ref": cursor})
            query = query.limit(page_size + 1)

        rows: list[dict[str, Any]] = []
        async for snapshot in query.stream():
            row = snapshot.to_dict() or {}
            row.pop("_corpus_version", None)
            if since is not None:
                observed = row.get("observed_at")
                if isinstance(observed, str):
                    observed = datetime.fromisoformat(observed)
                if observed is not None and observed < since:
                    continue
            rows.append(row)

        if address_id is not None:
            # The order the server would have applied, applied here instead, so
            # the cursor means the same thing on both paths.
            rows.sort(key=lambda r: str(r.get("record_ref", "")))
            if cursor:
                rows = [r for r in rows if str(r.get("record_ref", "")) > cursor]
            rows = rows[: page_size + 1]
        return rows


async def load_corpus(
    client: Any,
    config: FirestoreConfig,
    corpus: Any,
    *,
    replace: bool = True,
) -> dict[str, int]:
    """Write a generated corpus into Firestore.

    Idempotent on ``record_ref``: a document id is derived from the record's own
    reference, so re-loading the same corpus rewrites the same documents rather
    than accumulating a second copy of the district. ``replace`` clears each
    collection first, which is what makes a regenerated corpus a replacement
    rather than a merge of two municipalities.
    """
    written: dict[str, int] = {}

    for collection, records in corpus.records.items():
        name = config.collection(collection)
        if replace:
            await _purge(client, name)
        payloads = [
            (safe_document_id(record.record_ref), _document(record, corpus.corpus_version))
            for record in records
        ]
        await _write_all(client, name, payloads)
        written[collection] = len(payloads)

    for collection in CENTRAL_REFERENCE_COLLECTIONS:
        rows = corpus.reference.get(collection, ())
        name = config.collection(collection)
        if replace:
            await _purge(client, name)
        key = "incident_ref" if collection == "central_incidents" else "personnel_ref"
        payloads = [
            (safe_document_id(str(row[key])), {**row, "_corpus_version": corpus.corpus_version})
            for row in rows
        ]
        await _write_all(client, name, payloads)
        written[collection] = len(payloads)

    logger.info("central_corpus_loaded", extra={"written": sum(written.values())})
    return written


def _document(record: SourceRecord, corpus_version: str) -> dict[str, Any]:
    payload = record.model_dump(mode="json")
    payload["_corpus_version"] = corpus_version
    return payload


async def _write_all(
    client: Any, collection: str, payloads: list[tuple[str, dict[str, Any]]]
) -> None:
    """Batched writes. Firestore caps a batch at 500 operations."""
    for start in range(0, len(payloads), 400):
        batch = client.batch()
        for document_id, payload in payloads[start : start + 400]:
            batch.set(client.collection(collection).document(document_id), payload)
        await batch.commit()


async def _purge(client: Any, collection: str) -> None:
    """Clear a collection a page at a time, so a reload replaces rather than merges."""
    while True:
        snapshots = [
            snapshot async for snapshot in client.collection(collection).limit(400).stream()
        ]
        if not snapshots:
            return
        batch = client.batch()
        for snapshot in snapshots:
            batch.delete(snapshot.reference)
        await batch.commit()
