# ruff: noqa: ANN201

from __future__ import annotations

from muxdeck.domain.enums import AgentStatus
from muxdeck.services.operator_status_service import (
    OperatorStatusKind,
    describe_operator_status,
)


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
