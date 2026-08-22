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


class IntakeKeys:
    """What a 911 call or a CAD narrative is allowed to be read for.

    A closed set, and deliberately namespaced under ``intake.`` rather than
    reusing the structural keys above. ``occupancy.type`` is what a filed record
    says a building *is*; ``intake.reported_occupancy`` is what somebody on a
    phone said they saw. Sharing one key between the two would be the first step
    towards the merge treating them as the same claim -- and a caller's guess
    must never sort against a permit.

    They live here, beside the structural keys, so that every canonical key in
    the system is in one greppable file. What may be *done* with them is in
    :mod:`firstdue.incident.intake`, and the answer is: rendered on the brief
    behind a reported marker, and nothing else.
    """

    REPORTED_OCCUPANCY: Final = "intake.reported_occupancy"
    ENTRAPMENT_REPORTED: Final = "intake.entrapment_reported"
    HAZMAT_REPORTED: Final = "intake.hazardous_material_reported"
    REPORTED_FLOOR_OF_ORIGIN: Final = "intake.reported_floor_of_origin"
    REPORTED_ALARM_LEVEL: Final = "intake.reported_alarm_level"
    ACCESS_NOTE: Final = "intake.access_note"


#: The intake schema, in fixed order. A key outside this tuple is dropped rather
#: than rendered: the brief has nowhere to put it, and an attribute nobody
#: designed a line for is an attribute nobody reviewed. Ordered, because it is
#: handed to a model as a response schema and a reordered schema is a different
#: prompt.
INTAKE_KEYS: Final[tuple[str, ...]] = (
    IntakeKeys.REPORTED_OCCUPANCY,
    IntakeKeys.ENTRAPMENT_REPORTED,
    IntakeKeys.HAZMAT_REPORTED,
    IntakeKeys.REPORTED_FLOOR_OF_ORIGIN,
    IntakeKeys.REPORTED_ALARM_LEVEL,
    IntakeKeys.ACCESS_NOTE,
)


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
