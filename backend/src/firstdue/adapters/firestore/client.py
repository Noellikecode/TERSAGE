"""Firestore client construction and the collection map.

Every Firestore-specific call in the codebase goes through :class:`DocumentStore`
below. Keeping the vendor surface to one small class is what makes the adapter
reviewable: the interesting behaviour -- optimistic concurrency, gapless
sequences, fenced locks -- is written against ``get``, ``create``, ``put``, and
``transaction``, not against the client's full API.

The client import is deferred to first use. A fake-mode process never imports
``google.cloud.firestore``, and a checkout without the ``google`` extra installed
still type-checks, tests, and runs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from firstdue.errors import ConfigurationError

#: Every collection the system writes. Named here so the set is greppable and so
#: Terraform's index definitions have one source to follow.
COLLECTION_NAMES: Final[tuple[str, ...]] = (
    "profiles",
    "snapshots",
    "facts",
    "conflicts",
    "registry_agents",
    "registry_subscriptions",
    "incidents",
    "incident_logs",
    "incident_log_entries",
    "incident_grants",
    "standing_grants",
    "queue_entries",
    "referrals",
    "approvals",
    "surveys",
    "write_actions",
    "write_receipts",
    "policy_decisions",
    "audit_events",
    "locks",
    "idempotency",
    "agent_runs",
    "compensations",
)


@dataclass(frozen=True, slots=True)
class FirestoreConfig:
    """Where the data lives.

    ``namespace`` prefixes every collection name. Production leaves it empty;
    the contract suite sets a per-run value so parallel test runs cannot see
    each other's documents, and so a run can delete exactly what it wrote.
    """

    project_id: str
    database: str = "(default)"
    namespace: str = ""

    def collection(self, name: str) -> str:
        if name not in COLLECTION_NAMES:
            raise ConfigurationError("unknown Firestore collection", details={"collection": name})
        return f"{self.namespace}{name}" if self.namespace else name


class Collections:
    """Typed access to the collection names, so a typo is an error not a new table."""

    def __init__(self, config: FirestoreConfig) -> None:
        self._config = config

    def __getattr__(self, name: str) -> str:
        if name.startswith("_"):
            raise AttributeError(name)
        return self._config.collection(name)


def safe_document_id(raw: str) -> str:
    """Firestore-safe document id.

    Document ids may not contain ``/``, may not be ``.`` or ``..``, and may not
    match ``__.*__``. Everything the domain mints is already safe; this exists so
    an operator-supplied lock id cannot break the path.
    """
    if not raw:
        raise ConfigurationError("document id must not be empty")
    cleaned = raw.replace("/", "_")
    if cleaned in (".", "..") or (cleaned.startswith("__") and cleaned.endswith("__")):
        cleaned = f"id_{cleaned}"
    return cleaned[:1500]


def build_client(config: FirestoreConfig) -> Any:
    """Construct the async Firestore client.

    Authenticates through Application Default Credentials, which on Cloud Run
    is the service's own service account and locally is
    ``gcloud auth application-default login``. There is no emulator branch:
    every caller of this function reaches a real Firestore.

    Raises:
        ConfigurationError: when ``google-cloud-firestore`` is not installed.
            Live mode fails loudly rather than falling back to in-memory
            repositories that would lose every write on restart.
    """
    try:
        from google.cloud.firestore import AsyncClient
    except ImportError as exc:  # pragma: no cover - exercised only in live mode
        raise ConfigurationError(
            "google-cloud-firestore is not installed; install the 'google' extra "
            "or run with STORAGE_BACKEND=memory",
            details={"package": "google-cloud-firestore"},
        ) from exc
    return AsyncClient(project=config.project_id, database=config.database)


class DocumentStore:
    """One Firestore collection, with the operations the repositories need.

    ``create`` is distinct from ``put`` on purpose: append-only stores use
    ``create`` so a second write of the same id fails at the database rather
    than at a Python guard that a concurrent instance could race past.
    """

    def __init__(self, client: Any, collection: str) -> None:
        self._client = client
        self._collection = collection

    @property
    def client(self) -> Any:
        return self._client

    @property
    def name(self) -> str:
        return self._collection

    def ref(self, document_id: str) -> Any:
        return self._client.collection(self._collection).document(safe_document_id(document_id))

    async def get(self, document_id: str) -> dict[str, Any] | None:
        snapshot = await self.ref(document_id).get()
        if not snapshot.exists:
            return None
        data: dict[str, Any] = snapshot.to_dict() or {}
        return data

    async def create(self, document_id: str, data: Mapping[str, Any]) -> bool:
        """Create, returning False if the document already exists."""
        from google.api_core.exceptions import AlreadyExists

        try:
            await self.ref(document_id).create(dict(data))
        except AlreadyExists:
            return False
        return True

    async def put(self, document_id: str, data: Mapping[str, Any]) -> None:
        await self.ref(document_id).set(dict(data))

    async def delete(self, document_id: str) -> None:
        await self.ref(document_id).delete()

    async def stream(
        self, filters: Sequence[tuple[str, str, Any]] = ()
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream documents matching equality (or other) filters.

        Ordering and limiting happen in Python. Firestore would need a composite
        index for every filter/order pair, and a prototype that silently returned
        an unordered page would be worse than one that sorts a small result set
        in memory. See ``docs/build-notes.md`` for the scale limit this carries.
        """
        from google.cloud.firestore_v1.base_query import FieldFilter

        query: Any = self._client.collection(self._collection)
        for field, op, value in filters:
            query = query.where(filter=FieldFilter(field, op, value))
        async for snapshot in query.stream():
            data: dict[str, Any] = snapshot.to_dict() or {}
            yield data

    async def list(self, filters: Sequence[tuple[str, str, Any]] = ()) -> list[dict[str, Any]]:
        return [doc async for doc in self.stream(filters)]

    def transaction(self) -> Any:
        return self._client.transaction()


def document_store(client: Any, config: FirestoreConfig, collection: str) -> DocumentStore:
    return DocumentStore(client, config.collection(collection))
