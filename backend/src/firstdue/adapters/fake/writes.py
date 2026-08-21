"""Fake external write targets with real write semantics.

CAD dispatch, the building department's referral intake, and the department
records system are simulated *receiving* systems. What they simulate is the
receiving, not the semantics:

* a replay with the same idempotency key returns the **original receipt** with
  ``replayed=True`` and performs no second write;
* a replay with the same key but a different body is an
  :class:`~firstdue.errors.IdempotencyMismatchError` (HTTP 409);
* every executed write can be compensated, and the compensation is recorded.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from firstdue.domain.enums import Department
from firstdue.domain.work import WriteAction, WriteReceipt
from firstdue.errors import IdempotencyMismatchError, NotFoundError, SourceUnavailableError
from firstdue.ports.clock import Clock, IdGenerator


@dataclass(slots=True)
class _StoredWrite:
    receipt: WriteReceipt
    payload_hash: str
    body: dict[str, Any]
    compensated: bool = False


class FakeWriteTarget:
    """An external system that dedupes on the idempotency key."""

    def __init__(
        self,
        *,
        target_id: str,
        receiving_department: Department,
        clock: Clock,
        ids: IdGenerator,
        external_ref_prefix: str = "EXT",
        unavailable: bool = False,
    ) -> None:
        self._target_id = target_id
        self._receiving_department = receiving_department
        self._clock = clock
        self._ids = ids
        self._prefix = external_ref_prefix
        self.unavailable = unavailable
        self._by_key: dict[str, _StoredWrite] = {}
        self._by_receipt: dict[str, _StoredWrite] = {}
        self._sequence = 0

    @property
    def target_id(self) -> str:
        return self._target_id

    @property
    def receiving_department(self) -> Department:
        return self._receiving_department

    async def execute(self, action: WriteAction, *, body: Mapping[str, Any]) -> WriteReceipt:
        if self.unavailable:
            raise SourceUnavailableError(
                "write target unreachable",
                details={"target": self._target_id},
            )

        existing = self._by_key.get(action.idempotency_key)
        if existing is not None:
            if existing.payload_hash != action.payload_hash:
                raise IdempotencyMismatchError(
                    "this idempotency key was already used with a different request",
                    details={"target": self._target_id},
                )
            return existing.receipt.model_copy(update={"replayed": True})

        self._sequence += 1
        receipt = WriteReceipt(
            receipt_id=self._ids.new_id("receipt"),
            action_id=action.action_id,
            target=self._target_id,
            external_ref=f"{self._prefix}-{self._sequence:05d}",
            accepted_at=self._clock.now(),
            replayed=False,
        )
        stored = _StoredWrite(receipt=receipt, payload_hash=action.payload_hash, body=dict(body))
        self._by_key[action.idempotency_key] = stored
        self._by_receipt[receipt.receipt_id] = stored
        return receipt

    async def compensate(self, receipt: WriteReceipt, *, reason: str) -> WriteReceipt:
        stored = self._by_receipt.get(receipt.receipt_id)
        if stored is None:
            raise NotFoundError(
                "no such write to compensate", details={"receipt_id": receipt.receipt_id}
            )
        stored.compensated = True
        self._sequence += 1
        return WriteReceipt(
            receipt_id=self._ids.new_id("receipt"),
            action_id=receipt.action_id,
            target=self._target_id,
            external_ref=f"{self._prefix}-VOID-{self._sequence:05d}",
            accepted_at=self._clock.now(),
            replayed=False,
        )

    # -------------------------------------------------------- inspection

    def written_count(self) -> int:
        """Distinct writes actually performed -- replays do not increment it."""
        return len(self._by_key)

    def body_for(self, idempotency_key: str) -> dict[str, Any] | None:
        stored = self._by_key.get(idempotency_key)
        return dict(stored.body) if stored else None

    def is_compensated(self, receipt_id: str) -> bool:
        stored = self._by_receipt.get(receipt_id)
        return stored.compensated if stored else False
