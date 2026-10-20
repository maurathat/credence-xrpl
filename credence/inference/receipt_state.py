"""Minimal in-process receipt state for Checkpoint 1: chain heads and the settled-receipt fact.

Not persistence. Both stores live in memory for one gateway process. Settlement belongs to a later
checkpoint (C3B), so the gateway only reads SettledReceipts and never writes it.

Owner: Inference.
"""

from __future__ import annotations

from typing import Protocol

from credence.core.primitives import FacilityId, Sha256Digest


class ReceiptChainError(RuntimeError):
    """The chain head moved between building a receipt and recording it."""


class ReceiptChain:
    """The digest of each facility's last accepted receipt; None before the first one."""

    def __init__(self) -> None:
        self._heads: dict[FacilityId, Sha256Digest] = {}

    def head(self, facility_id: FacilityId) -> Sha256Digest | None:
        return self._heads.get(facility_id)

    def advance(
        self, facility_id: FacilityId, previous: Sha256Digest | None, receipt: Sha256Digest
    ) -> None:
        """Make `receipt` the head, only if the head is still `previous`."""
        if self.head(facility_id) != previous:
            raise ReceiptChainError(f"chain head of {facility_id.value} moved")
        self._heads[facility_id] = receipt


class SettledReceipts(Protocol):
    """Answers the verifier's `receipt_already_settled` fact for a receipt digest."""

    def is_settled(self, receipt_digest: Sha256Digest) -> bool: ...


class InMemorySettledReceipts:
    """In-memory settled-receipt set. The Checkpoint 1 gateway never marks anything settled."""

    def __init__(self) -> None:
        self._settled: set[Sha256Digest] = set()

    def is_settled(self, receipt_digest: Sha256Digest) -> bool:
        return receipt_digest in self._settled

    def mark_settled(self, receipt_digest: Sha256Digest) -> None:
        self._settled.add(receipt_digest)
