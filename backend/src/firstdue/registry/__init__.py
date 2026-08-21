"""The cross-department agent registry: descriptors and seeding."""

from __future__ import annotations

from firstdue.registry.descriptors import (
    FLEET,
    FLEET_VERSION,
    HOME_DEPARTMENT,
    descriptor_for,
    fleet_descriptors,
)
from firstdue.registry.seed import seed_registry

__all__ = [
    "FLEET",
    "FLEET_VERSION",
    "HOME_DEPARTMENT",
    "descriptor_for",
    "fleet_descriptors",
    "seed_registry",
]
