"""Sub-agent value objects.

A sub-agent in Copilot CLI is an agent spawned via the ``task`` tool
inside another agent's session. The parent session's ``events.jsonl``
records each invocation as a pair of ``subagent.started`` /
``subagent.completed`` events keyed by the tool call id.

These types are pure, framework-agnostic data shared by the parser,
the dashboard controller, and the widgets. Keep them immutable so the
same tree can be handed out without defensive copies.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class SubAgentSnapshot:
    """A single sub-agent invocation within a parent session.

    ``tool_call_id`` is the unique key: Copilot CLI reuses ``agent_name``
    (e.g. ``"general-purpose"``) for many invocations, so the tool call
    id is the only reliable identity. ``completed_at`` is ``None`` while
    the sub-agent is still running.
    """

    tool_call_id: str
    agent_name: str
    display_name: str
    description: str | None
    started_at: datetime
    completed_at: datetime | None = None
    # Enriched from the matching ``tool.execution_start``/``_complete``
    # pair for the underlying ``task`` tool call. These are optional
    # because a session that was truncated / corrupted may only have
    # one side of the pair, or neither.
    task_name: str | None = None
    agent_type: str | None = None
    prompt: str | None = None
    mode: str | None = None
    result_content: str | None = None
    success: bool | None = None

    @property
    def is_running(self) -> bool:
        return self.completed_at is None

    @property
    def duration_seconds(self) -> float | None:
        if self.completed_at is None:
            return None
        return (self.completed_at - self.started_at).total_seconds()


@dataclass(frozen=True, slots=True)
class SubAgentTree:
    """All sub-agents currently known for one parent session.

    ``running`` / ``recent`` are separate tuples so the dashboard can
    render them distinctly without re-filtering on every frame. Both
    are ordered newest-first so the most recent activity lands at the
    top of the expanded view.
    """

    session_id: str
    running: tuple[SubAgentSnapshot, ...]
    recent: tuple[SubAgentSnapshot, ...]
    scanned_at: datetime

    @property
    def running_count(self) -> int:
        return len(self.running)

    @property
    def total_count(self) -> int:
        return len(self.running) + len(self.recent)

    def is_empty(self) -> bool:
        return self.total_count == 0


__all__ = ["SubAgentSnapshot", "SubAgentTree"]
