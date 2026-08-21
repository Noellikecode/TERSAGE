"""Load deterministic demo state into the running process.

Rehydration goes through ``BuildingProfile.model_validate``, so every invariant
-- gapless timelines, provenance, survey-gated verification -- is re-checked on
the way in. State that would fail validation cannot be loaded from disk.
"""

from __future__ import annotations

from firstdue.container import Container
from firstdue.demo.seed import load_seed, profiles_from_seed
from firstdue.observability.logging import get_logger

logger = get_logger(__name__)


async def load_demo_state(container: Container) -> int:
    """Load seeded profiles and their facts. Returns the profile count.

    Absent state is not an error: an unseeded process starts empty and the
    console shows an honest empty district rather than invented rows.
    """
    document = load_seed(container.settings.demo_state_dir)
    if document is None:
        logger.info(
            "demo_state_absent",
            extra={"state_dir": str(container.settings.demo_state_dir)},
        )
        return 0

    profiles = profiles_from_seed(document)
    for profile in profiles:
        await container.profiles.create(profile)
        for fact in profile.all_facts():
            await container.facts.append(fact)
        for conflict in profile.conflicts:
            await container.conflicts.add(conflict)

    logger.info(
        "demo_state_loaded",
        extra={
            "profiles": len(profiles),
            "content_hash": str(document.get("content_hash", "")),
        },
    )
    return len(profiles)
