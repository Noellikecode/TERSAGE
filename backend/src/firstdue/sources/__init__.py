"""The source-adapter framework.

Eleven municipal and federal sources, one set of behaviours. Rather than eleven
adapters that each cache a little differently and each report availability in
their own words, there is one :class:`ManagedSource` that does caching, rate
limiting, circuit breaking, snapshotting, and health reporting, and one small
:class:`PageFetcher` per source that knows only how to get a page of records.

The consequence that matters: swapping a source from its fixture to its live
feed changes the fetcher and nothing else. Provenance, pagination, breaker
behaviour, and what the console shows stay identical.
"""

from __future__ import annotations

from firstdue.sources.backfill import BackfillResult, DistrictBackfill
from firstdue.sources.framework import (
    ManagedSource,
    PageFetcher,
    RateLimiter,
    RawPage,
    SourceConfig,
)

__all__ = [
    "BackfillResult",
    "DistrictBackfill",
    "ManagedSource",
    "PageFetcher",
    "RateLimiter",
    "RawPage",
    "SourceConfig",
]
