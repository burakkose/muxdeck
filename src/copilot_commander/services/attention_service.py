from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

AttentionSeverity = Literal["info", "warning", "error"]


@dataclass(frozen=True, slots=True)
class AttentionSignal:
    alert_id: str
    severity: AttentionSeverity
    title: str
    message: str
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class AttentionNotification:
    alert_id: str
    severity: AttentionSeverity
    title: str
    message: str


@dataclass(frozen=True, slots=True)
class AttentionSyncResult:
    unread_ids: frozenset[str]
    notifications: tuple[AttentionNotification, ...]


class AttentionInboxService:
    def __init__(self) -> None:
        self._active_ids: set[str] = set()
        self._unread_ids: set[str] = set()
        self._notified_critical_ids: set[str] = set()

    def observe(self, signals: Sequence[AttentionSignal]) -> tuple[AttentionNotification, ...]:
        notifications: list[AttentionNotification] = []
        for signal in signals:
            if signal.alert_id not in self._active_ids:
                self._active_ids.add(signal.alert_id)
                self._unread_ids.add(signal.alert_id)
            if signal.severity != "error":
                continue
            if signal.alert_id in self._notified_critical_ids:
                continue
            self._notified_critical_ids.add(signal.alert_id)
            notifications.append(
                AttentionNotification(
                    alert_id=signal.alert_id,
                    severity=signal.severity,
                    title=signal.title,
                    message=signal.message,
                )
            )
        return tuple(notifications)

    def synchronize(self, signals: Sequence[AttentionSignal]) -> AttentionSyncResult:
        notifications = self.observe(signals)
        current_ids = {signal.alert_id for signal in signals}
        self._active_ids = current_ids
        self._unread_ids.intersection_update(current_ids)
        self._notified_critical_ids.intersection_update(current_ids)
        return AttentionSyncResult(
            unread_ids=frozenset(self._unread_ids),
            notifications=notifications,
        )

    def mark_read(self, alert_ids: Iterable[str]) -> None:
        self._unread_ids.difference_update(alert_ids)

    def mark_all_read(self) -> None:
        self._unread_ids.clear()


__all__ = [
    "AttentionInboxService",
    "AttentionNotification",
    "AttentionSeverity",
    "AttentionSignal",
    "AttentionSyncResult",
]
