"""The two kinds of page fetcher: a live feed and a deterministic fixture.

A source is *defined* by its config and its fetcher. Which fetcher it gets is a
deployment decision, and it is the only thing that changes between the
credential-free demo and a live poll -- provenance, pagination, caching, breaker
behaviour, and what the console reports are identical either way.

Both are honest about what they are. :class:`FixtureFetcher` reports
``FIXTURE``; :class:`HttpFetcher` reports ``LIVE``. The console renders that
verbatim, because a hidden simulation is worse than an admitted one.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from firstdue.errors import SourceUnavailableError
from firstdue.observability.logging import get_logger
from firstdue.ports.sources import SourceMode, SourceRecord
from firstdue.sources.framework import RawPage, paginate

logger = get_logger(__name__)

#: Maps one raw upstream record to a SourceRecord, or None to skip it.
RecordMapper = Callable[[dict[str, Any]], SourceRecord | None]


class FixtureFetcher:
    """Deterministic records from a JSON fixture.

    The fixture is loaded once and paginated in memory, so the same call
    sequence produces the same snapshots on every run -- which is what makes
    ``make demo`` reproducible.
    """

    def __init__(
        self,
        path: Path,
        *,
        mapper: RecordMapper | None = None,
    ) -> None:
        self._path = path
        self._mapper = mapper
        self._records: tuple[SourceRecord, ...] | None = None

    @property
    def mode(self) -> SourceMode:
        return SourceMode.FIXTURE

    def _load(self) -> tuple[SourceRecord, ...]:
        if self._records is not None:
            return self._records
        if not self._path.is_file():
            raise SourceUnavailableError(
                "source fixture is missing",
                details={"path": self._path.name},
            )
        payload = json.loads(self._path.read_text(encoding="utf-8"))
        raw = payload.get("records", []) if isinstance(payload, dict) else payload
        mapped: list[SourceRecord] = []
        for entry in raw:
            record = self._mapper(entry) if self._mapper else SourceRecord.model_validate(entry)
            if record is not None:
                mapped.append(record)
        # Sorted so pagination is stable regardless of file order.
        self._records = tuple(sorted(mapped, key=lambda r: (r.observed_at, r.record_ref)))
        return self._records

    async def fetch_page(
        self,
        *,
        address_id: str | None,
        since: datetime | None,
        cursor: str | None,
        page_size: int,
    ) -> RawPage:
        selected = [
            record
            for record in self._load()
            if (address_id is None or record.address_id == address_id)
            and (since is None or record.observed_at >= since)
        ]
        return paginate(selected, cursor, page_size)


class HttpFetcher:
    """A live public feed.

    The boundary, not the integration: it issues one GET, maps the rows, and
    lets every failure become ``SourceUnavailableError`` so the managed source
    can break the circuit. Nothing here retries -- that is the caller's policy,
    and it already exists in :mod:`firstdue.reliability`.

    Networking is imported lazily so a fake-mode process never opens a socket.
    """

    def __init__(
        self,
        *,
        url: str,
        mapper: RecordMapper,
        params: dict[str, str] | None = None,
        rows_path: Sequence[str] = (),
        address_param: str | None = None,
        since_param: str | None = None,
        offset_param: str | None = "$offset",
        limit_param: str | None = "$limit",
        timeout_s: float = 10.0,
    ) -> None:
        self._url = url
        self._mapper = mapper
        self._params = dict(params or {})
        self._rows_path = tuple(rows_path)
        self._address_param = address_param
        self._since_param = since_param
        self._offset_param = offset_param
        self._limit_param = limit_param
        self._timeout_s = timeout_s

    @property
    def mode(self) -> SourceMode:
        return SourceMode.LIVE

    def _build_params(
        self, address_id: str | None, since: datetime | None, cursor: str | None, page_size: int
    ) -> dict[str, str]:
        params = dict(self._params)
        if self._limit_param:
            params[self._limit_param] = str(page_size)
        if self._offset_param:
            params[self._offset_param] = cursor or "0"
        if self._address_param and address_id:
            params[self._address_param] = address_id
        if self._since_param and since:
            params[self._since_param] = since.isoformat()
        return params

    def _rows(self, payload: Any) -> list[dict[str, Any]]:
        node = payload
        for key in self._rows_path:
            if not isinstance(node, dict):
                return []
            node = node.get(key, [])
        return [row for row in node if isinstance(row, dict)] if isinstance(node, list) else []

    async def fetch_page(
        self,
        *,
        address_id: str | None,
        since: datetime | None,
        cursor: str | None,
        page_size: int,
    ) -> RawPage:
        import httpx

        params = self._build_params(address_id, since, cursor, page_size)
        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                response = await client.get(self._url, params=params)
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:
            # Message deliberately omits the URL: source internals never leave
            # the adapter, and an operator has the source_id already.
            logger.warning(
                "source_fetch_failed",
                extra={"error_type": type(exc).__name__},
            )
            raise SourceUnavailableError(
                "live source is unreachable", details={"error_type": type(exc).__name__}
            ) from exc

        records: list[SourceRecord] = []
        for row in self._rows(payload):
            try:
                mapped = self._mapper(row)
            except Exception:
                # One malformed row does not fail the page. Skipping it is
                # visible as a smaller record count, never as a wrong fact.
                logger.warning("source_row_skipped", extra={"reason": "unmappable"})
                continue
            if mapped is not None:
                records.append(mapped)

        offset = int(cursor or "0")
        next_cursor = str(offset + len(records)) if len(records) == page_size else None
        return RawPage(records=tuple(records), next_cursor=next_cursor)
