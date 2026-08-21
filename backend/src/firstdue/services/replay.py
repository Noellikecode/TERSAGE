"""Incident replay.

A NIOSH line-of-duty-death investigation asks what the incident commander was
shown, and when. Answering it means reconstructing the brief from the state it
was built on -- not from today's state, and not from a summary somebody wrote
afterwards.

Five things make that possible, and all five are recorded at the time:

* the **profile snapshot id**, so the facts are the facts as they stood;
* the **pinned agent versions**, so the code that produced each emission is
  identified rather than assumed to be current;
* the **policy version**, so an allow that would be a deny today still reads as
  the allow it was;
* the **ordered log entries**, gapless, written during the incident;
* a **content hash** per entry, so tampering is detectable rather than arguable.

Replay is a pure read. It writes nothing, and it re-derives no facts: an
investigation that changed the record it was investigating would be worthless.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from firstdue.domain.logentries import AppendOnlyLog, IncidentLogEntry
from firstdue.errors import NotFoundError
from firstdue.observability.logging import get_logger
from firstdue.ports.audit import AuditSink
from firstdue.ports.repositories import (
    IncidentLogRepository,
    IncidentRepository,
    SnapshotRepository,
)

logger = get_logger(__name__)


class ReplayedEntry(BaseModel):
    """One log entry as the replay reconstructs it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sequence: int = Field(ge=0)
    entry_id: str
    entry_type: str
    occurred_at: datetime
    profile_snapshot_id: str
    agent_versions: dict[str, str] = Field(default_factory=dict)
    content: dict[str, object] = Field(default_factory=dict)
    content_hash: str
    #: False when the stored hash does not match the stored content.
    intact: bool = True


class ReplayResult(BaseModel):
    """The reconstructed incident, plus what it can and cannot vouch for."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    incident_id: str
    profile_snapshot_id: str
    #: True when the snapshot the brief was built from is still readable.
    snapshot_available: bool
    entries: tuple[ReplayedEntry, ...] = ()
    #: Every agent version that produced an entry, and every policy version that
    #: decided one. Both are recorded, never inferred from today's build.
    agent_versions: dict[str, str] = Field(default_factory=dict)
    policy_versions: tuple[str, ...] = ()
    sealed_at: datetime | None = None
    #: Entries whose stored hash does not match their stored content.
    tampered_sequences: tuple[int, ...] = ()

    @property
    def is_intact(self) -> bool:
        return not self.tampered_sequences

    @property
    def digest(self) -> str:
        """A hash over the ordered entry hashes.

        Two replays of an untouched incident produce the same digest. One
        changed byte anywhere in the log changes it, which is the whole point.
        """
        material = "|".join(f"{e.sequence}:{e.content_hash}" for e in self.entries)
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


class IncidentReplay:
    """Reconstructs what a commander was shown, from what was recorded."""

    def __init__(
        self,
        *,
        incidents: IncidentRepository,
        incident_log: IncidentLogRepository,
        snapshots: SnapshotRepository,
        audit: AuditSink,
    ) -> None:
        self._incidents = incidents
        self._log = incident_log
        self._snapshots = snapshots
        self._audit = audit

    async def replay(self, incident_id: str) -> ReplayResult:
        """Replay one incident.

        Raises:
            NotFoundError: when no such incident was ever opened. A missing
                *log* is not an error -- an incident that produced no entries
                replays as an empty, intact record, which is the truth.
        """
        incident = await self._incidents.get(incident_id)
        if incident is None:
            raise NotFoundError("incident not found", details={"incident_id": incident_id})

        log: AppendOnlyLog = await self._log.get_log(incident_id)
        entries = tuple(self._replay_entry(entry) for entry in log.entries)

        agent_versions: dict[str, str] = {}
        for entry in log.entries:
            agent_versions.update(entry.agent_versions)

        policy_versions = await self._policy_versions(incident_id)
        snapshot = await self._snapshots.get(incident.profile_snapshot_id)

        result = ReplayResult(
            incident_id=incident_id,
            profile_snapshot_id=incident.profile_snapshot_id,
            snapshot_available=snapshot is not None,
            entries=entries,
            agent_versions=agent_versions,
            policy_versions=policy_versions,
            sealed_at=log.sealed_at,
            tampered_sequences=tuple(e.sequence for e in entries if not e.intact),
        )
        logger.info(
            "incident_replayed",
            extra={
                "incident_id": incident_id,
                "entries": len(entries),
                "intact": result.is_intact,
                "snapshot_available": result.snapshot_available,
            },
        )
        return result

    async def _policy_versions(self, incident_id: str) -> tuple[str, ...]:
        """Every policy version that decided something during this incident."""
        decisions = await self._audit.list_decisions(incident_id=incident_id, limit=1000)
        return tuple(sorted({decision.policy_version for decision in decisions}))

    @staticmethod
    def _replay_entry(entry: IncidentLogEntry) -> ReplayedEntry:
        """Rebuild one entry and check it against its own hash."""
        recomputed = entry.compute_content_hash()
        return ReplayedEntry(
            sequence=entry.sequence,
            entry_id=entry.entry_id,
            entry_type=str(entry.entry_type),
            occurred_at=entry.occurred_at,
            profile_snapshot_id=entry.profile_snapshot_id,
            agent_versions=dict(entry.agent_versions),
            content=dict(entry.content),
            content_hash=entry.content_hash or recomputed,
            intact=(not entry.content_hash) or entry.content_hash == recomputed,
        )


def compare(first: ReplayResult, second: ReplayResult) -> bool:
    """Whether two replays reconstructed the same ordered output."""
    return first.digest == second.digest and [e.sequence for e in first.entries] == [
        e.sequence for e in second.entries
    ]


def ordered_summary(result: ReplayResult) -> Sequence[str]:
    """One line per entry, in order. What an investigator reads."""
    return [
        f"{e.sequence:04d} {e.occurred_at.isoformat()} {e.entry_type} "
        f"snapshot={e.profile_snapshot_id} hash={e.content_hash[:12]}"
        for e in result.entries
    ]
