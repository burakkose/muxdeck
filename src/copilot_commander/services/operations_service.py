"""Services supporting bulk operations and operator audit history."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class OperationAuditEntry:
    """Immutable audit entry for an operator-triggered action."""

    occurred_at: datetime
    action: str
    agent_id: str
    agent_name: str
    success: bool
    message: str


class OperationAuditService:
    """In-memory audit trail for bulk operator actions."""

    def __init__(self, *, max_entries: int = 200) -> None:
        self._entries: deque[OperationAuditEntry] = deque(maxlen=max_entries)

    def record(self, entry: OperationAuditEntry) -> None:
        self._entries.appendleft(entry)

    def record_batch(self, entries: Iterable[OperationAuditEntry]) -> None:
        for entry in entries:
            self.record(entry)

    def list_entries(self, *, limit: int = 20) -> Sequence[OperationAuditEntry]:
        if limit <= 0:
            return ()
        return tuple(entry for index, entry in enumerate(self._entries) if index < limit)


__all__ = ["OperationAuditEntry", "OperationAuditService"]
