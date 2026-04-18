from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from copilot_commander.domain.enums import AgentStatus

OperatorStatusTone = Literal["info", "warning", "error", "success"]


class OperatorStatusKind(StrEnum):
    WORKING = "working"
    WAITING_INPUT = "waiting_input"
    BLOCKED = "blocked"
    REVIEW_READY = "review_ready"
    FAILED = "failed"
    TERMINATED = "terminated"
    STALE = "stale"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class OperatorStatus:
    kind: OperatorStatusKind
    label: str
    headline: str
    reason: str
    tone: OperatorStatusTone
    needs_attention: bool
    is_critical: bool = False


def default_operator_status() -> OperatorStatus:
    return OperatorStatus(
        kind=OperatorStatusKind.WORKING,
        label="working",
        headline="working",
        reason="work in progress",
        tone="info",
        needs_attention=False,
    )


def describe_operator_status(
    *,
    agent_status: AgentStatus,
    needs_attention: bool,
    attention_reason: str | None,
    idle_seconds: int,
    is_potentially_stuck: bool,
    task_title: str | None,
    current_activity: str | None,
) -> OperatorStatus:
    reason = _first_text(attention_reason, current_activity, task_title)
    if agent_status is AgentStatus.COMPLETED:
        return OperatorStatus(
            kind=OperatorStatusKind.COMPLETED,
            label="done",
            headline="completed",
            reason=reason or "task completed",
            tone="success",
            needs_attention=False,
        )
    if agent_status is AgentStatus.DEAD:
        # A dead/terminated agent is a historical record, not an
        # error condition. Reserve red/error styling for actual
        # failures (AgentStatus.ERROR); use a calmer warning tone
        # for "copilot exited" / "pane gone" so the dashboard
        # doesn't scream at the operator about something they
        # already chose to terminate.
        return OperatorStatus(
            kind=OperatorStatusKind.TERMINATED,
            label="terminated",
            headline="terminated",
            reason=reason or _failure_reason(agent_status),
            tone="warning",
            needs_attention=False,
            is_critical=False,
        )
    if agent_status is AgentStatus.ERROR:
        return OperatorStatus(
            kind=OperatorStatusKind.FAILED,
            label="failed",
            headline="failed",
            reason=reason or _failure_reason(agent_status),
            tone="error",
            needs_attention=True,
            is_critical=True,
        )
    if agent_status is AgentStatus.WAITING_INPUT:
        return OperatorStatus(
            kind=OperatorStatusKind.WAITING_INPUT,
            label="waiting",
            headline="waiting for input",
            reason=reason or "operator input required",
            tone="warning",
            needs_attention=True,
        )
    if agent_status is AgentStatus.BLOCKED:
        return OperatorStatus(
            kind=OperatorStatusKind.BLOCKED,
            label="blocked",
            headline="blocked",
            reason=reason or "progress is blocked",
            tone="error",
            needs_attention=True,
            is_critical=True,
        )
    if is_potentially_stuck or _looks_stale(
        agent_status=agent_status,
        needs_attention=needs_attention,
        attention_reason=attention_reason,
    ):
        return OperatorStatus(
            kind=OperatorStatusKind.STALE,
            label="stale",
            headline="stale",
            reason=reason or f"quiet for {_format_duration(idle_seconds)}",
            tone="warning",
            needs_attention=True,
        )
    if needs_attention:
        return OperatorStatus(
            kind=OperatorStatusKind.REVIEW_READY,
            label="review",
            headline="review-ready",
            reason=reason or "ready for operator review",
            tone="warning",
            needs_attention=True,
        )
    return OperatorStatus(
        kind=OperatorStatusKind.WORKING,
        label="working",
        headline="working",
        reason=_working_reason(
            agent_status=agent_status,
            idle_seconds=idle_seconds,
            task_title=task_title,
            current_activity=current_activity,
        ),
        tone="info",
        needs_attention=False,
    )


def _first_text(*values: str | None) -> str | None:
    for value in values:
        if value is None:
            continue
        normalized = value.strip()
        if normalized:
            return normalized
    return None


def _failure_reason(agent_status: AgentStatus) -> str:
    if agent_status is AgentStatus.DEAD:
        return "agent pane exited unexpectedly"
    return "error detected"


def _looks_stale(
    *,
    agent_status: AgentStatus,
    needs_attention: bool,
    attention_reason: str | None,
) -> bool:
    if agent_status is AgentStatus.IDLE and needs_attention:
        return True
    if attention_reason is None:
        return False
    normalized = attention_reason.casefold()
    stale_markers = (
        "idle for ",
        "output unchanged",
        "stale",
        "no activity",
    )
    return any(marker in normalized for marker in stale_markers)


def _working_reason(
    *,
    agent_status: AgentStatus,
    idle_seconds: int,
    task_title: str | None,
    current_activity: str | None,
) -> str:
    if current_activity is not None and current_activity.strip():
        return current_activity.strip()
    if task_title is not None and task_title.strip():
        return task_title.strip()
    if agent_status in {AgentStatus.DISCOVERED, AgentStatus.STARTING}:
        return "starting up"
    if agent_status is AgentStatus.IDLE:
        return f"quiet for {_format_duration(idle_seconds)}"
    if agent_status is AgentStatus.UNKNOWN:
        return "status not yet classified"
    return "work in progress"


def _format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    return f"{hours}h{minutes}m"


__all__ = [
    "OperatorStatus",
    "OperatorStatusKind",
    "OperatorStatusTone",
    "default_operator_status",
    "describe_operator_status",
]
