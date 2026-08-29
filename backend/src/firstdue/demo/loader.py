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
    # Records yes, findings no -- see `Settings.demo_rebuild_findings`. The
    # facts are the months of reading the fleet is supposed to have already
    # done; the conflicts between them are this afternoon's work, and seeding
    # those is what left the slow loop with nothing to show.
    seed_conflicts = not container.settings.demo_rebuild_findings
    loaded = 0
    already = 0
    withheld = 0
    for profile in profiles:
        # A profile that is already there is the ordinary case on any restart
        # against a durable store, and it used to be fatal: `create` refuses a
        # duplicate -- correctly, it is the append-only guard -- so the second
        # boot of a Firestore-backed process died in the lifespan hook with
        # "profile already exists" and the service never became ready.
        #
        # Skipped rather than overwritten. The seed is the *starting* state; a
        # district that has since been polled has facts and conflicts the seed
        # does not know about, and rewriting it from the seed would throw away
        # everything the fleet had learned since.
        if await container.profiles.get(profile.address_id) is not None:
            already += 1
            continue
        await container.profiles.create(profile)
        loaded += 1
        for fact in profile.all_facts():
            await container.facts.append(fact)
        if seed_conflicts:
            for conflict in profile.conflicts:
                await container.conflicts.add(conflict)
        else:
            withheld += len(profile.conflicts)

    logger.info(
        "demo_state_loaded",
        extra={
            "profiles": loaded,
            "already_present": already,
            "conflicts_withheld": withheld,
            "content_hash": str(document.get("content_hash", "")),
        },
    )
    return len(profiles)
