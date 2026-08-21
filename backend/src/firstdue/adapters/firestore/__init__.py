"""Firestore-backed durable memory.

Importing this package costs nothing and requires nothing: the Google client is
imported lazily inside :func:`~firstdue.adapters.firestore.client.build_client`,
so a fake-mode process -- and the whole test suite -- never touches it.

Every repository here is held to the same contract suite as its in-memory
counterpart (``tests/contract``). A behavioural difference between the two is a
bug in one of them, not a property of the backend.
"""

from __future__ import annotations

from firstdue.adapters.firestore.audit import FirestoreAuditSink
from firstdue.adapters.firestore.client import (
    Collections,
    FirestoreConfig,
    build_client,
    document_store,
)
from firstdue.adapters.firestore.repositories import (
    FirestoreAgentRunRepository,
    FirestoreApprovalRepository,
    FirestoreCompensationRepository,
    FirestoreConflictRepository,
    FirestoreFactRepository,
    FirestoreGrantRepository,
    FirestoreIdempotencyRepository,
    FirestoreIncidentLogRepository,
    FirestoreIncidentRepository,
    FirestoreLockRepository,
    FirestoreProfileRepository,
    FirestoreQueueRepository,
    FirestoreReferralRepository,
    FirestoreRegistryRepository,
    FirestoreSnapshotRepository,
    FirestoreSurveyRepository,
    FirestoreWriteActionRepository,
)

__all__ = [
    "Collections",
    "FirestoreAgentRunRepository",
    "FirestoreApprovalRepository",
    "FirestoreAuditSink",
    "FirestoreCompensationRepository",
    "FirestoreConfig",
    "FirestoreConflictRepository",
    "FirestoreFactRepository",
    "FirestoreGrantRepository",
    "FirestoreIdempotencyRepository",
    "FirestoreIncidentLogRepository",
    "FirestoreIncidentRepository",
    "FirestoreLockRepository",
    "FirestoreProfileRepository",
    "FirestoreQueueRepository",
    "FirestoreReferralRepository",
    "FirestoreRegistryRepository",
    "FirestoreSnapshotRepository",
    "FirestoreSurveyRepository",
    "FirestoreWriteActionRepository",
    "build_client",
    "document_store",
]
