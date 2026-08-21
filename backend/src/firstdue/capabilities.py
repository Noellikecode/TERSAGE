"""What this build actually implements.

The console renders this rather than guessing. A surface that is not built yet
shows as ``PLANNED`` with the phase that delivers it -- an honest empty state,
never a button that does nothing.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class CapabilityStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    PLANNED = "PLANNED"


class CapabilityInfo(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    label: str
    status: CapabilityStatus
    #: The build phase that delivers it. Present for planned capabilities.
    phase: int


#: Ordered by the phase that delivers each capability.
CAPABILITIES: tuple[CapabilityInfo, ...] = (
    CapabilityInfo(
        id="domain-contracts",
        label="Domain contracts and invariants",
        status=CapabilityStatus.AVAILABLE,
        phase=1,
    ),
    CapabilityInfo(
        id="health-readiness",
        label="Health and readiness",
        status=CapabilityStatus.AVAILABLE,
        phase=1,
    ),
    CapabilityInfo(
        id="fake-mode",
        label="Credential-free fake mode",
        status=CapabilityStatus.AVAILABLE,
        phase=1,
    ),
    CapabilityInfo(
        id="durable-memory",
        label="Durable memory: profiles, snapshots, locks, idempotency",
        status=CapabilityStatus.AVAILABLE,
        phase=2,
    ),
    CapabilityInfo(
        id="agent-registry",
        label="Agent registry and version pinning",
        status=CapabilityStatus.AVAILABLE,
        phase=2,
    ),
    CapabilityInfo(
        id="event-fabric",
        label="Versioned events, retries, breakers, dead letters",
        status=CapabilityStatus.AVAILABLE,
        phase=2,
    ),
    CapabilityInfo(
        id="conflict-engine",
        label="Deterministic conflict, decay, and merge engines",
        status=CapabilityStatus.AVAILABLE,
        phase=2,
    ),
    CapabilityInfo(
        id="slow-loop-watchers",
        label="Records, geometry, and hazard watchers",
        status=CapabilityStatus.AVAILABLE,
        phase=3,
    ),
    CapabilityInfo(
        id="delta-ranker",
        label="Survey queue delta ranker",
        status=CapabilityStatus.AVAILABLE,
        phase=3,
    ),
    CapabilityInfo(
        id="autonomous-actions",
        label="Work orders, calendar, crew mail, NFPA 1620 pre-plans",
        status=CapabilityStatus.AVAILABLE,
        phase=3,
    ),
    CapabilityInfo(
        id="approval-gated-referrals",
        label="Approval-gated building referrals",
        status=CapabilityStatus.AVAILABLE,
        phase=3,
    ),
    CapabilityInfo(
        id="console-apis",
        label="District, queue, profile, geometry, and survey APIs",
        status=CapabilityStatus.AVAILABLE,
        phase=3,
    ),
    CapabilityInfo(
        id="gateway",
        label="Default-deny policy gateway and grants",
        status=CapabilityStatus.AVAILABLE,
        phase=4,
    ),
    CapabilityInfo(
        id="phi-derivation",
        label="PHI derivation and jurisdiction filtering",
        status=CapabilityStatus.AVAILABLE,
        phase=4,
    ),
    CapabilityInfo(
        id="audit-replay",
        label="Immutable audit events and incident replay",
        status=CapabilityStatus.AVAILABLE,
        phase=4,
    ),
    CapabilityInfo(
        id="security-controls",
        label="Injection screening, signed callbacks, limits, endpoint authorization",
        status=CapabilityStatus.AVAILABLE,
        phase=4,
    ),
    CapabilityInfo(
        id="incident-loop",
        label="Streaming incident brief: instant, enriched, amendments",
        status=CapabilityStatus.AVAILABLE,
        phase=5,
    ),
    CapabilityInfo(
        id="sensor-fusion",
        label="Thermal fusion, face coverage, void observations",
        status=CapabilityStatus.AVAILABLE,
        phase=5,
    ),
    CapabilityInfo(
        id="incident-record",
        label="Persist-before-transmit log, buffered RMS, NERIS draft",
        status=CapabilityStatus.AVAILABLE,
        phase=5,
    ),
)
