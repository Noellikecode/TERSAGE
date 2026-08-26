"""City adapter -- the only place municipality-specific behaviour lives.

Default municipality is San Francisco. Everything that differs between cities --
address normalisation, district resolution, which sources exist, how hydrants
are identified -- sits behind this protocol so the core never learns a city's
name.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class NormalizedAddress(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    address_id: str = Field(min_length=1, max_length=120)
    display: str = Field(min_length=1, max_length=200)
    district_id: str = Field(min_length=1, max_length=120)
    jurisdiction_id: str = Field(min_length=1, max_length=120)
    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)
    parcel_ref: str | None = Field(default=None, max_length=120)
    #: The parcel's real outline, in metres relative to its own centroid.
    #:
    #: Reference data, like the coordinates beside it: the city publishes the
    #: polygon and this is where the municipality's own facts about a place
    #: live. What a *building* is -- how tall, how many storeys, what it is made
    #: of -- is still a fact with provenance, derived by an agent and merged
    #: like any other.
    #:
    #: ``None`` where the feed has no usable ring, and the massing model falls
    #: back to a rectangle. A box is worse than the real shape and better than
    #: nothing to draw.
    footprint: tuple[tuple[float, float], ...] | None = None


@runtime_checkable
class CityAdapter(Protocol):
    @property
    def municipality_id(self) -> str:
        """Stable identifier, e.g. ``san-francisco-ca``."""
        ...

    @property
    def default_jurisdiction_id(self) -> str: ...

    def normalize_address(self, raw: str) -> NormalizedAddress | None:
        """Resolve a free-text address to a canonical one, or ``None``."""
        ...

    def get_address(self, address_id: str) -> NormalizedAddress | None: ...

    def list_districts(self) -> Sequence[str]: ...

    def source_ids(self) -> Sequence[str]:
        """Source adapters configured for this municipality."""
        ...
