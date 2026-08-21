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

from firstdue.domain.enums import Classification
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


#: Resolves an address id to (latitude, longitude), or None when unknown.
PointResolver = Callable[[str], "tuple[float, float] | None"]

#: Maps one point-query response to a SourceRecord, or None to skip it.
PointMapper = Callable[[dict[str, Any], str, float, float], SourceRecord | None]


class PointFetcher:
    """A live feed that answers about one coordinate rather than a list.

    Elevation, roof geometry, and weather are not paginated record sets -- they
    are questions about a place. Asking them requires an address, so this
    fetcher refuses to run without one: a district-wide sweep would be a
    per-address fan-out the caller has to decide to do, not something a fetcher
    should do behind its back.

    The coordinate comes from the city adapter, which is the only component
    allowed to know where an address is. A fetcher that geocoded for itself
    would be a second, disagreeing answer to that question.
    """

    def __init__(
        self,
        *,
        url: str,
        mapper: PointMapper,
        resolver: PointResolver,
        params: dict[str, str] | None = None,
        lat_param: str = "y",
        lon_param: str = "x",
        point_param: str | None = None,
        point_format: str = "{lat},{lon}",
        headers: dict[str, str] | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        self._url = url
        self._mapper = mapper
        self._resolver = resolver
        self._params = dict(params or {})
        self._lat_param = lat_param
        self._lon_param = lon_param
        self._point_param = point_param
        self._point_format = point_format
        self._headers = dict(headers or {})
        self._timeout_s = timeout_s

    @property
    def mode(self) -> SourceMode:
        return SourceMode.LIVE

    def _build_params(self, latitude: float, longitude: float) -> dict[str, str]:
        params = dict(self._params)
        if self._templated:
            # The coordinate is already in the path; repeating it as a query
            # parameter is how you get a 400 from an API that validates them.
            return params
        if self._point_param:
            params[self._point_param] = self._point_format.format(lat=latitude, lon=longitude)
        else:
            params[self._lat_param] = f"{latitude:.6f}"
            params[self._lon_param] = f"{longitude:.6f}"
        return params

    @property
    def _templated(self) -> bool:
        return "{lat}" in self._url or "{lon}" in self._url

    def _build_url(self, latitude: float, longitude: float) -> str:
        if not self._templated:
            return self._url
        return self._url.format(lat=f"{latitude:.4f}", lon=f"{longitude:.4f}")

    async def fetch_page(
        self,
        *,
        address_id: str | None,
        since: datetime | None,
        cursor: str | None,
        page_size: int,
    ) -> RawPage:
        import httpx

        if address_id is None:
            raise SourceUnavailableError(
                "this source answers about one address and none was given",
                details={"reason": "address_required"},
            )
        # A second page of a one-point answer is always empty. Saying so here
        # keeps the backfill loop from asking the upstream the same question
        # again for a cursor that cannot advance.
        if cursor:
            return RawPage(records=(), next_cursor=None)

        point = self._resolver(address_id)
        if point is None:
            raise SourceUnavailableError(
                "the address has no resolved coordinate",
                details={"reason": "address_unresolved"},
            )
        latitude, longitude = point

        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                response = await client.get(
                    self._build_url(latitude, longitude),
                    params=self._build_params(latitude, longitude),
                    headers=self._headers or None,
                )
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:
            logger.warning("source_fetch_failed", extra={"error_type": type(exc).__name__})
            raise SourceUnavailableError(
                "live source is unreachable", details={"error_type": type(exc).__name__}
            ) from exc

        if not isinstance(payload, dict):
            raise SourceUnavailableError(
                "point source returned an unexpected shape",
                details={"payload_type": type(payload).__name__},
            )

        try:
            record = self._mapper(payload, address_id, latitude, longitude)
        except Exception:
            logger.warning("source_row_skipped", extra={"reason": "unmappable"})
            return RawPage(records=(), next_cursor=None)

        return RawPage(records=(record,) if record is not None else (), next_cursor=None)


class NwsPointFetcher:
    """Current-hour conditions from the National Weather Service.

    Two hops, because that is how the API is shaped: ``/points/{lat},{lon}``
    resolves a coordinate to a forecast grid, and the grid answers the weather.
    The intermediate URL is taken from the first response rather than
    constructed, so a change to NWS's grid scheme does not silently produce a
    404 this code would report as "no weather".

    NWS asks every caller to identify itself in ``User-Agent``. An anonymous
    caller is rate-limited hard, so the identity is part of the config.
    """

    POINTS_URL: str = "https://api.weather.gov/points/{lat},{lon}"

    def __init__(
        self,
        *,
        resolver: PointResolver,
        user_agent: str,
        timeout_s: float = 10.0,
    ) -> None:
        self._resolver = resolver
        self._user_agent = user_agent
        self._timeout_s = timeout_s

    @property
    def mode(self) -> SourceMode:
        return SourceMode.LIVE

    async def fetch_page(
        self,
        *,
        address_id: str | None,
        since: datetime | None,
        cursor: str | None,
        page_size: int,
    ) -> RawPage:
        import httpx

        if address_id is None:
            raise SourceUnavailableError(
                "weather is asked about one address and none was given",
                details={"reason": "address_required"},
            )
        if cursor:
            return RawPage(records=(), next_cursor=None)

        point = self._resolver(address_id)
        if point is None:
            raise SourceUnavailableError(
                "the address has no resolved coordinate",
                details={"reason": "address_unresolved"},
            )
        latitude, longitude = point
        headers = {"User-Agent": self._user_agent, "Accept": "application/geo+json"}

        try:
            async with httpx.AsyncClient(timeout=self._timeout_s, headers=headers) as client:
                grid = await client.get(
                    self.POINTS_URL.format(lat=f"{latitude:.4f}", lon=f"{longitude:.4f}")
                )
                grid.raise_for_status()
                hourly_url = grid.json()["properties"]["forecastHourly"]

                forecast = await client.get(hourly_url)
                forecast.raise_for_status()
                periods = forecast.json()["properties"]["periods"]
        except Exception as exc:
            logger.warning("source_fetch_failed", extra={"error_type": type(exc).__name__})
            raise SourceUnavailableError(
                "live source is unreachable", details={"error_type": type(exc).__name__}
            ) from exc

        if not periods:
            return RawPage(records=(), next_cursor=None)

        current = periods[0]
        observed = datetime.fromisoformat(str(current["startTime"]))
        humidity = current.get("relativeHumidity") or {}
        return RawPage(
            records=(
                SourceRecord(
                    record_ref=f"nws/{address_id}/{observed.isoformat()}",
                    address_id=address_id,
                    classification=Classification.PUBLIC,
                    fields={
                        "temperature": current.get("temperature"),
                        "temperature_unit": current.get("temperatureUnit"),
                        "wind_speed": current.get("windSpeed"),
                        "wind_direction": current.get("windDirection"),
                        "relative_humidity": humidity.get("value"),
                        "short_forecast": current.get("shortForecast"),
                    },
                    observed_at=observed,
                ),
            ),
            next_cursor=None,
        )
