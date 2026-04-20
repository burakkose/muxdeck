"""Volatile in-memory registry for sub-task / background-agent tracking."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from muxdeck.parsers.copilot_output_parser import CopilotTaskEvidence, TaskStatus


@dataclass(frozen=True, slots=True)
class SubTaskInfo:
    """Reconciled snapshot of a sub-task attached to a parent agent."""

    task_key: str
    agent_type_label: str
    model: str | None
    description: str
    status: TaskStatus
    first_seen_at: datetime
    last_seen_at: datetime


def _make_task_key(evidence: CopilotTaskEvidence) -> str:
    model_part = evidence.model or ""
    return f"{evidence.agent_type_label}|{model_part}|{evidence.description}"


_DEFAULT_TTL = timedelta(seconds=30)
_COMPLETED_TTL = timedelta(seconds=15)


class SubTaskRegistry:
    """Tracks live sub-tasks per parent pane, with TTL-based expiry."""

    def __init__(
        self,
        *,
        ttl: timedelta = _DEFAULT_TTL,
        completed_ttl: timedelta = _COMPLETED_TTL,
    ) -> None:
        self._ttl = ttl
        self._completed_ttl = completed_ttl
        self._tasks: dict[str, dict[str, SubTaskInfo]] = {}

    def update(
        self,
        pane_id: str,
        evidence: tuple[CopilotTaskEvidence, ...],
        background_task_count: int,
        *,
        now: datetime | None = None,
    ) -> None:
        now = now or datetime.now(UTC)
        existing = self._tasks.get(pane_id, {})
        updated: dict[str, SubTaskInfo] = {}

        seen_keys: set[str] = set()
        for ev in evidence:
            key = _make_task_key(ev)
            seen_keys.add(key)
            prev = existing.get(key)
            updated[key] = SubTaskInfo(
                task_key=key,
                agent_type_label=ev.agent_type_label,
                model=ev.model or (prev.model if prev else None),
                description=ev.description,
                status=ev.status,
                first_seen_at=prev.first_seen_at if prev else now,
                last_seen_at=now,
            )

        for key, task in existing.items():
            if key in seen_keys:
                continue
            ttl = (
                self._completed_ttl
                if task.status in ("completed", "failed", "cancelled")
                else self._ttl
            )
            if now - task.last_seen_at < ttl:
                updated[key] = task

        if background_task_count == 0 and not evidence:
            updated = {
                k: v for k, v in updated.items() if v.status in ("completed", "failed", "cancelled")
            }

        if updated:
            self._tasks[pane_id] = updated
        else:
            self._tasks.pop(pane_id, None)

    def get_tasks(self, pane_id: str) -> tuple[SubTaskInfo, ...]:
        tasks = self._tasks.get(pane_id, {})
        return tuple(sorted(tasks.values(), key=lambda t: t.first_seen_at))

    def remove_pane(self, pane_id: str) -> None:
        self._tasks.pop(pane_id, None)

    def all_pane_ids(self) -> frozenset[str]:
        return frozenset(self._tasks.keys())

    def expire_all(self, *, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        for pane_id in list(self._tasks):
            tasks = self._tasks[pane_id]
            live = {}
            for key, task in tasks.items():
                ttl = (
                    self._completed_ttl
                    if task.status in ("completed", "failed", "cancelled")
                    else self._ttl
                )
                if now - task.last_seen_at < ttl:
                    live[key] = task
            if live:
                self._tasks[pane_id] = live
            else:
                del self._tasks[pane_id]


__all__ = ["SubTaskInfo", "SubTaskRegistry"]
