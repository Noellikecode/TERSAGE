"""NASA adapters: FIRMS active fire, and POWER fire weather.

Two endpoints, one port. FIRMS is the subject -- where things are burning -- and
POWER is context beside it -- how hot, dry and windy it has been. They are
separate modules because they fail separately and on purpose: POWER being down
must cost the commander the weather panel, never the map.

:func:`build_fire_activity` is the whole wiring decision, in one place, so the
container states it once rather than three components each guessing. The third
branch is the one that matters: live mode with no map key gets an adapter that
*refuses*, never the synthetic one. A live process drawing invented wildfires on
a commander's display would be the exact failure this project refuses
everywhere else.
"""

from __future__ import annotations

from firstdue.adapters.nasa.firms import (
    DEFAULT_CITY_BOUNDS,
    DEFAULT_REGION,
    NasaFirmsClient,
    UnconfiguredFireActivityClient,
)
from firstdue.adapters.nasa.power import NasaPowerClient
from firstdue.ports.city import CityAdapter
from firstdue.ports.clock import Clock
from firstdue.ports.fireactivity import BoundingBox, FireActivityClient

__all__ = [
    "DEFAULT_CITY_BOUNDS",
    "DEFAULT_REGION",
    "NasaFirmsClient",
    "NasaPowerClient",
    "UnconfiguredFireActivityClient",
    "build_fire_activity",
]


def build_fire_activity(
    *,
    use_fake: bool,
    map_key: str | None,
    city: CityAdapter,
    clock: Clock,
    region: BoundingBox | None = None,
    city_bounds: BoundingBox | None = None,
) -> FireActivityClient:
    """Pick the fire-activity client for this process. Three states, no fallthrough.

    Takes the settings it needs rather than ``Settings`` itself, the way
    :func:`firstdue.sources.catalog.build_sources` does, so nothing in the
    adapter layer has to depend upwards to answer a question about its own
    input.
    """
    if use_fake:
        # Imported here so a live process never loads the synthetic adapter it
        # must not be able to reach for.
        from firstdue.adapters.fake.fireactivity import FakeFireActivityClient

        return FakeFireActivityClient(
            city=city,
            clock=clock,
            region=region or DEFAULT_REGION,
            city_bounds=city_bounds or DEFAULT_CITY_BOUNDS,
        )

    if not map_key:
        return UnconfiguredFireActivityClient()

    return NasaFirmsClient(
        map_key=map_key,
        city=city,
        clock=clock,
        region=region or DEFAULT_REGION,
        city_bounds=city_bounds or DEFAULT_CITY_BOUNDS,
        # POWER takes no credential, so fire weather is available whenever the
        # detections are. It carries its own deadline, strictly under the FIRMS
        # one, so a slow POWER cannot take the map down with it.
        weather=NasaPowerClient(clock=clock),
    )
