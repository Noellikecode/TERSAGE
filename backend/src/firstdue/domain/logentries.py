"""The incident log -- append-only, written during the incident, not after.

What the commander saw at 03:14:22 is stored at 03:14:22 rather than
reconstructed later. Entries are immutable and their sequence is monotonic and
gapless; :class:`AppendOnlyLog` is the only sanctioned way to grow one.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from firstdue.domain.enums import LogEntryType
from firstdue.errors import AppendOnlyViolationError, ValidationError


class IncidentLogEntry(BaseModel):
    """One immutable record in the department's operational log."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entry_id: str = Field(min_length=1, max_length=120)
    incident_id: str = Field(min_length=1, max_length=120)
    sequence: int = Field(ge=0, description="monotonic, gapless")
    entry_type: LogEntryType
    occurred_at: datetime = Field(description="server timestamp")

    profile_snapshot_id: str = Field(min_length=1, max_length=120)
    agent_versions: dict[str, str] = Field(default_factory=dict)
    content: dict[str, Any] = Field(default_factory=dict)
    content_hash: str = Field(default="", max_length=64)

    correlation_id: str | None = Field(default=None, max_length=120)
    causation_id: str | None = Field(default=None, max_length=120)

    #: Null while buffered. The incident is never blocked by a logging failure;
    #: entries queue here and flush when the records system recovers.
    written_to_rms_at: datetime | None = None

    def compute_content_hash(self) -> str:
        payload = {
            "incident_id": self.incident_id,
            "sequence": self.sequence,
            "entry_type": str(self.entry_type),
            "occurred_at": self.occurred_at.isoformat(),
            "profile_snapshot_id": self.profile_snapshot_id,
            "agent_versions": self.agent_versions,
            "content": self.content,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def sealed(self) -> IncidentLogEntry:
        return self.model_copy(update={"content_hash": self.compute_content_hash()})

    def mark_written_to_rms(self, *, at: datetime) -> IncidentLogEntry:
        if self.written_to_rms_at is not None:
            return self
        return self.model_copy(update={"written_to_rms_at": at})


class AppendOnlyLog(BaseModel):
    """A sealed, gapless sequence of log entries for one incident."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    incident_id: str = Field(min_length=1, max_length=120)
    entries: tuple[IncidentLogEntry, ...] = ()
    sealed_at: datetime | None = None

    @model_validator(mode="after")
    def _check_sequence(self) -> Self:
        for index, entry in enumerate(self.entries):
            if entry.sequence != index:
                raise AppendOnlyViolationError(
                    "incident log sequence must be gapless and monotonic",
                    details={"expected": index, "found": entry.sequence},
                )
            if entry.incident_id != self.incident_id:
                raise ValidationError(
                    "log entry belongs to a different incident",
                    details={"entry_id": entry.entry_id},
                )
        return self

    @property
    def next_sequence(self) -> int:
        return len(self.entries)

    def append(self, entry: IncidentLogEntry) -> AppendOnlyLog:
        """Append one entry. There is no update and no delete."""
        if self.sealed_at is not None:
            raise AppendOnlyViolationError(
                "the incident log is sealed", details={"incident_id": self.incident_id}
            )
        if entry.sequence != self.next_sequence:
            raise AppendOnlyViolationError(
                "log entries must be appended in sequence",
                details={"expected": self.next_sequence, "found": entry.sequence},
            )
        stored = entry if entry.content_hash else entry.sealed()
        return self.model_copy(update={"entries": (*self.entries, stored)})

    def seal(self, *, at: datetime) -> AppendOnlyLog:
        """Seal at incident close. Sealing twice keeps the first seal time."""
        if self.sealed_at is not None:
            return self
        return self.model_copy(update={"sealed_at": at})

    @property
    def unflushed(self) -> tuple[IncidentLogEntry, ...]:
        """Entries still buffered because the records system was unreachable."""
        return tuple(e for e in self.entries if e.written_to_rms_at is None)
