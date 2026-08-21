"""Canonical attribute keys.

A canonical key names *what is being asserted* independently of which source
asserted it -- that is what lets the conflict engine compare a permit against a
lidar measurement without either one having primacy in the data model.
"""

from __future__ import annotations

from typing import Annotated, Final

from pydantic import StringConstraints

#: ``structure.stories``, ``hazard.tier_ii.present`` -- lowercase dotted segments.
CanonicalKey = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=3,
        max_length=120,
        pattern=r"^[a-z][a-z0-9_]*(\.[a-z0-9][a-z0-9_]*)+$",
    ),
]


class Keys:
    """Well-known canonical keys.

    Not a closed set -- extraction may mint new keys -- but everything the
    conflict engine and the instant brief depend on is named here so those
    dependencies are greppable.
    """

    STORIES: Final = "structure.stories"
    CONSTRUCTION_TYPE: Final = "structure.construction_type"
    ROOF_TYPE: Final = "structure.roof_type"
    FLOOR_SYSTEM: Final = "structure.floor_system"
    LIGHTWEIGHT_TRUSS: Final = "structure.lightweight_truss"
    HEIGHT_M: Final = "structure.height_m"
    YEAR_BUILT: Final = "structure.year_built"
    FOOTPRINT_AREA_M2: Final = "structure.footprint_area_m2"

    OCCUPANCY_TYPE: Final = "occupancy.type"
    OCCUPANT_LOAD: Final = "occupancy.load"
    LIFE_SAFETY_NOTE: Final = "occupancy.life_safety_note"

    SUPPRESSION_SPRINKLERED: Final = "suppression.sprinklered"
    SUPPRESSION_STANDPIPE: Final = "suppression.standpipe"
    FDC_LOCATION: Final = "suppression.fdc_location"

    EGRESS_OBSTRUCTION: Final = "egress.obstruction"
    STAIRWELL_COUNT: Final = "egress.stairwell_count"

    HAZARD_TIER_II_PRESENT: Final = "hazard.tier_ii.present"
    HAZARD_TIER_II_LOCATION: Final = "hazard.tier_ii.storage_location"
    HAZARD_SOLAR_ARRAY: Final = "hazard.solar_array"
    HAZARD_EV_CHARGER: Final = "hazard.ev_charger"
    HAZARD_PIPELINE_PROXIMITY_M: Final = "hazard.pipeline_proximity_m"

    UNPERMITTED_CONSTRUCTION: Final = "compliance.unpermitted_construction"
    OPEN_VIOLATION: Final = "compliance.open_violation"

    THERMAL_FACE_C: Final = "thermal.face_temperature_c"
    WEATHER_WIND_KPH: Final = "weather.wind_kph"

    #: Not an attribute anything merges on: the key a screened narrative is
    #: filed under in the semantic index, so a recall result can say what kind
    #: of thing it is pointing at.
    NARRATIVE: Final = "document.narrative"


#: Keys whose change invalidates measured geometry and queues a re-measure.
GEOMETRY_INVALIDATING_KEYS: frozenset[str] = frozenset(
    {
        Keys.STORIES,
        Keys.HEIGHT_M,
        Keys.ROOF_TYPE,
        Keys.FOOTPRINT_AREA_M2,
        Keys.HAZARD_SOLAR_ARRAY,
    }
)
