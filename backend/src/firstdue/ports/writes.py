"""External write targets.

Five systems receive writes: the building department's referral intake, the
inspection work-order system, Cloud Storage for pre-incident plans, agency
notification, and the department's records system.

Two rules the protocol enforces rather than suggests:

* **Every write carries an idempotency key.** ``WriteAction`` cannot be built
  without one, so ``execute`` cannot be called without one.
* **Every write has a compensating action.** A referral can be withdrawn, a work
  order cancelled -- and the action names how before it executes.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from firstdue.domain.enums import Department
from firstdue.domain.work import WriteAction, WriteReceipt


@runtime_checkable
class ExternalWriteTarget(Protocol):
    @property
    def target_id(self) -> str: ...

    @property
    def receiving_department(self) -> Department: ...

    async def execute(self, action: WriteAction, *, body: Mapping[str, Any]) -> WriteReceipt:
        """Perform the write.

        Implementations must dedupe on ``action.idempotency_key``: a replay with
        the same key returns the original receipt with ``replayed=True`` and
        performs no second write. A replay with the same key but a different
        ``payload_hash`` raises
        :class:`~firstdue.errors.IdempotencyMismatchError` (HTTP 409).
        """
        ...

    async def compensate(self, receipt: WriteReceipt, *, reason: str) -> WriteReceipt:
        """Undo a previously executed write (withdraw, cancel, retract)."""
        ...
