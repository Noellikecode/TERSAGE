"""Stable snapshot identity.

A NIOSH line-of-duty-death investigation asks what the incident commander knew
at 03:14. Answering it means the brief must replay from the exact state it was
built on, which requires two things this module provides.

**A content hash of the profile.** Canonical JSON over the whole profile,
excluding nothing, so two profiles that hash alike are the same profile.

**A snapshot id derived from identity, not from a counter.** The id is a
function of ``(address_id, profile_version, content_hash)``. Snapshotting the
same profile version twice therefore yields the same id, and the snapshot store
treats the second write as a replay rather than a new snapshot -- so the moment
the incident recorded is the moment the replay reproduces, not a second reading
taken microseconds later with a different staleness column.

Nothing here reads a clock. The id contains no timestamp, deliberately: an id
that varied with the read time would make every snapshot unique and the whole
mechanism pointless.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, types only
    from firstdue.domain.profiles import BuildingProfile

SNAPSHOT_ID_PREFIX: Final[str] = "snap"
#: Long enough that a collision across a department's history is not a concern.
_DIGEST_LENGTH: Final[int] = 24


def canonical_hash(payload: Any) -> str:
    """SHA-256 over canonical JSON. The one hashing convention in the codebase."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def profile_content_hash(profile: BuildingProfile) -> str:
    """Hash of everything the profile asserts.

    Uses ``mode="json"`` so the hash is over the same bytes that Firestore, the
    demo seed, and the API all round-trip -- not over Python object identity.
    """
    return canonical_hash(profile.model_dump(mode="json"))


def stable_snapshot_id(address_id: str, profile_version: int, *, content_hash: str) -> str:
    """The id a given profile version always produces.

    Includes the content hash as well as the version so that a version reused
    after a restore -- the only way two different states can share a version --
    cannot silently collide with the original snapshot.
    """
    material = f"{address_id}|{profile_version}|{content_hash}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:_DIGEST_LENGTH]
    return f"{SNAPSHOT_ID_PREFIX}_{digest}"


def snapshot_id_for(profile: BuildingProfile) -> str:
    """Convenience wrapper: the stable id for this profile as it stands."""
    return stable_snapshot_id(
        profile.address_id,
        profile.profile_version,
        content_hash=profile_content_hash(profile),
    )
