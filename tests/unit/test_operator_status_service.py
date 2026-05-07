# ruff: noqa: ANN201

from __future__ import annotations

from muxdeck.domain.enums import AgentStatus
from muxdeck.services.operator_status_service import (
    OperatorStatusKind,
    describe_operator_status,
)


def test_default_operator_status() -> None:
    from muxdeck.services.operator_status_service import OperatorStatusKind, default_operator_status

    status = default_operator_status()
    assert status.kind == OperatorStatusKind.WORKING
    assert status.label == "working"
    assert status.tone == "info"
    assert status.needs_attention is False


def test_describe_operator_status_distinguishes_operator_states() -> None:
    working = describe_operator_status(
        agent_status=AgentStatus.RUNNING,
        needs_attention=False,
        attention_reason=None,
        idle_seconds=5,
        is_potentially_stuck=False,
        task_title="Implement inbox",
        current_activity="building widgets",
    )
    waiting = describe_operator_status(
        agent_status=AgentStatus.WAITING_INPUT,
        needs_attention=True,
        attention_reason="waiting for confirmation input",
        idle_seconds=75,
        is_potentially_stuck=False,
        task_title=None,
        current_activity=None,
    )
    blocked = describe_operator_status(
        agent_status=AgentStatus.BLOCKED,
        needs_attention=True,
        attention_reason="merge conflict requires intervention",
        idle_seconds=12,
        is_potentially_stuck=False,
        task_title=None,
        current_activity=None,
    )
    review_ready = describe_operator_status(
        agent_status=AgentStatus.RUNNING,
        needs_attention=True,
        attention_reason="waiting for operator review",
        idle_seconds=20,
        is_potentially_stuck=False,
        task_title=None,
        current_activity="writing tests",
    )
    failed = describe_operator_status(
        agent_status=AgentStatus.ERROR,
        needs_attention=True,
        attention_reason="tool failed with exit code 1",
        idle_seconds=0,
        is_potentially_stuck=False,
        task_title=None,
        current_activity=None,
    )
    stale = describe_operator_status(
        agent_status=AgentStatus.IDLE,
        needs_attention=True,
        attention_reason="output unchanged for 180s — may be stuck",
        idle_seconds=180,
        is_potentially_stuck=True,
        task_title=None,
        current_activity=None,
    )
    completed = describe_operator_status(
        agent_status=AgentStatus.COMPLETED,
        needs_attention=False,
        attention_reason=None,
        idle_seconds=0,
        is_potentially_stuck=False,
        task_title="Ship it",
        current_activity=None,
    )
    starting = describe_operator_status(
        agent_status=AgentStatus.STARTING,
        needs_attention=False,
        attention_reason=None,
        idle_seconds=0,
        is_potentially_stuck=False,
        task_title=None,
        current_activity=None,
    )

    assert working.kind == OperatorStatusKind.WORKING
    assert working.reason == "building widgets"
    assert starting.kind == OperatorStatusKind.STARTING
    assert starting.headline == "launching"
    assert waiting.kind == OperatorStatusKind.WAITING_INPUT
    assert waiting.headline == "waiting for input"
    assert blocked.kind == OperatorStatusKind.BLOCKED
    assert blocked.is_critical is True
    assert review_ready.kind == OperatorStatusKind.REVIEW_READY
    assert review_ready.headline == "review-ready"
    assert failed.kind == OperatorStatusKind.FAILED
    assert failed.is_critical is True
    assert stale.kind == OperatorStatusKind.STALE
    assert completed.kind == OperatorStatusKind.COMPLETED
    assert completed.reason == "Ship it"


def test_dead_agents_are_terminated_not_failed() -> None:
    # DEAD agents are historical records, not active errors.
    # Reserve the FAILED kind (and its red/error tone) for actual
    # ERROR-status agents; render DEAD as a calmer warning so the
    # dashboard does not scream about panes the operator already
    # killed on purpose.
    terminated = describe_operator_status(
        agent_status=AgentStatus.DEAD,
        needs_attention=True,
        attention_reason="tmux pane no longer exists",
        idle_seconds=0,
        is_potentially_stuck=False,
        task_title=None,
        current_activity=None,
    )
    failed = describe_operator_status(
        agent_status=AgentStatus.ERROR,
        needs_attention=True,
        attention_reason="tool failed with exit code 1",
        idle_seconds=0,
        is_potentially_stuck=False,
        task_title=None,
        current_activity=None,
    )

    assert terminated.kind == OperatorStatusKind.TERMINATED
    assert terminated.tone == "warning"
    assert terminated.is_critical is False
    assert terminated.needs_attention is False
    assert failed.kind == OperatorStatusKind.FAILED
    assert failed.tone == "error"
    assert failed.is_critical is True


def test_stale_agent_with_idle_and_needs_attention() -> None:
    """Idle agent with needs_attention flag should be classified as stale."""
    status = describe_operator_status(
        agent_status=AgentStatus.IDLE,
        needs_attention=True,
        attention_reason=None,
        idle_seconds=300,
        is_potentially_stuck=False,
        task_title=None,
        current_activity=None,
    )

    assert status.kind == OperatorStatusKind.STALE


def test_stale_markers_in_attention_reason() -> None:
    """Attention reason with stale markers should trigger STALE status."""
    for marker_text in [
        "idle for 5 minutes",
        "output unchanged for 30s",
        "STALE connection",
        "no activity detected",
    ]:
        status = describe_operator_status(
            agent_status=AgentStatus.RUNNING,
            needs_attention=True,
            attention_reason=marker_text,
            idle_seconds=100,
            is_potentially_stuck=False,
            task_title=None,
            current_activity=None,
        )
        assert status.kind == OperatorStatusKind.STALE, f"Failed for marker: {marker_text}"


def test_working_reason_with_current_activity() -> None:
    """Current activity should be used as working reason."""
    status = describe_operator_status(
        agent_status=AgentStatus.RUNNING,
        needs_attention=False,
        attention_reason=None,
        idle_seconds=5,
        is_potentially_stuck=False,
        task_title="Task title",
        current_activity="  parsing code  ",  # With whitespace
    )

    assert status.reason == "parsing code"


def test_working_reason_fallback_to_task_title() -> None:
    """Task title should be used when current_activity is empty."""
    status = describe_operator_status(
        agent_status=AgentStatus.RUNNING,
        needs_attention=False,
        attention_reason=None,
        idle_seconds=5,
        is_potentially_stuck=False,
        task_title="Implement feature",
        current_activity="",  # Empty
    )

    assert status.reason == "Implement feature"


def test_working_reason_with_discovered_status() -> None:
    """DISCOVERED status should have specific working reason."""
    status = describe_operator_status(
        agent_status=AgentStatus.DISCOVERED,
        needs_attention=False,
        attention_reason=None,
        idle_seconds=0,
        is_potentially_stuck=False,
        task_title=None,
        current_activity=None,
    )

    assert status.reason == "starting up"


def test_working_reason_with_idle_status() -> None:
    """IDLE status should show formatted duration."""
    status = describe_operator_status(
        agent_status=AgentStatus.IDLE,
        needs_attention=False,
        attention_reason=None,
        idle_seconds=125,
        is_potentially_stuck=False,
        task_title=None,
        current_activity=None,
    )

    assert "2m" in status.reason


def test_working_reason_with_unknown_status() -> None:
    """UNKNOWN status should have generic reason."""
    status = describe_operator_status(
        agent_status=AgentStatus.UNKNOWN,
        needs_attention=False,
        attention_reason=None,
        idle_seconds=0,
        is_potentially_stuck=False,
        task_title=None,
        current_activity=None,
    )

    assert status.reason == "status not yet classified"


def test_format_duration_seconds() -> None:
    """Short durations should be formatted in seconds."""
    from muxdeck.services.operator_status_service import _format_duration

    assert _format_duration(0) == "0s"
    assert _format_duration(45) == "45s"


def test_format_duration_minutes() -> None:
    """Medium durations should be formatted in minutes."""
    from muxdeck.services.operator_status_service import _format_duration

    assert _format_duration(60) == "1m"
    assert _format_duration(150) == "2m"


def test_format_duration_hours() -> None:
    """Long durations should be formatted in hours and minutes."""
    from muxdeck.services.operator_status_service import _format_duration

    assert _format_duration(3600) == "1h0m"
    assert _format_duration(3900) == "1h5m"


def test_first_text_returns_first_non_empty() -> None:
    """_first_text should return first non-empty, non-None string."""
    from muxdeck.services.operator_status_service import _first_text

    assert _first_text(None, "  ", "third", "fourth") == "third"
    assert _first_text("", "second") == "second"
    assert _first_text(None, None, None) is None


def test_failure_reason_for_dead_status() -> None:
    """Failure reason for DEAD status should mention pane exit."""
    from muxdeck.services.operator_status_service import _failure_reason

    reason = _failure_reason(AgentStatus.DEAD)
    assert "pane" in reason.lower() or "exit" in reason.lower()
