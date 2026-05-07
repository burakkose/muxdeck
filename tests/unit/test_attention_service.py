# ruff: noqa: ANN201

from __future__ import annotations

from datetime import UTC, datetime

from muxdeck.services.attention_service import (
    AttentionInboxService,
    AttentionSignal,
)

_TS = datetime(2025, 1, 1, 12, tzinfo=UTC)


def test_attention_inbox_tracks_unread_and_critical_notifications() -> None:
    service = AttentionInboxService()
    critical = AttentionSignal(
        alert_id="agent-1:failed",
        severity="error",
        title="failed",
        message="tool failed with exit code 1",
        occurred_at=_TS,
    )
    warning = AttentionSignal(
        alert_id="agent-2:stale",
        severity="warning",
        title="stale",
        message="output unchanged",
        occurred_at=_TS,
    )

    first = service.synchronize((critical, warning))
    second = service.synchronize((critical, warning))

    assert first.unread_ids == frozenset({"agent-1:failed", "agent-2:stale"})
    assert [note.alert_id for note in first.notifications] == ["agent-1:failed"]
    assert second.notifications == ()

    service.mark_read(("agent-1:failed",))
    third = service.synchronize((critical, warning))

    assert third.unread_ids == frozenset({"agent-2:stale"})


def test_attention_inbox_can_notify_again_after_resolution() -> None:
    service = AttentionInboxService()
    critical = AttentionSignal(
        alert_id="agent-1:failed",
        severity="error",
        title="failed",
        message="tool failed with exit code 1",
        occurred_at=_TS,
    )

    service.synchronize((critical,))
    service.synchronize(())
    result = service.synchronize((critical,))

    assert [note.alert_id for note in result.notifications] == ["agent-1:failed"]


def test_attention_inbox_observe_alerts() -> None:
    service = AttentionInboxService()
    signal1 = AttentionSignal(
        alert_id="alert-1",
        severity="error",
        title="failed",
        message="error occurred",
        occurred_at=_TS,
    )
    signal2 = AttentionSignal(
        alert_id="alert-2",
        severity="error",
        title="failed2",
        message="error occurred2",
        occurred_at=_TS,
    )

    result = service.observe((signal1, signal2))

    assert len(result) == 2
    assert result[0].alert_id == "alert-1"
    assert result[1].alert_id == "alert-2"


def test_attention_inbox_mark_all_read() -> None:
    service = AttentionInboxService()
    signal = AttentionSignal(
        alert_id="alert-1",
        severity="error",
        title="failed",
        message="error occurred",
        occurred_at=_TS,
    )

    service.synchronize((signal,))
    service.mark_all_read()
    result = service.synchronize((signal,))

    assert "alert-1" not in result.unread_ids
