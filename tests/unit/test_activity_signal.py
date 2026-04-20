"""Tests for _has_activity_signal recency-based detection."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from muxdeck.services.monitoring_service import _has_activity_signal


@dataclass(frozen=True, slots=True)
class FakeSpan:
    category: str = "activity:file_read"
    start_line: int = 1
    end_line: int = 1
    text: str = "reading foo.py"
    confidence: Decimal = Decimal("0.85")


@dataclass(frozen=True, slots=True)
class FakeActivityMarker:
    activity: str = "reading foo.py"
    category: str = "file_read"
    span: FakeSpan = field(default_factory=FakeSpan)


@dataclass(frozen=True, slots=True)
class FakeUIMarker:
    kind: str = "slash_commands"
    span: FakeSpan = field(default_factory=FakeSpan)


@dataclass(frozen=True, slots=True)
class FakeParseResult:
    boundaries: tuple[object, ...] = ()
    ui_markers: tuple[FakeUIMarker, ...] = ()
    activity_markers: tuple[FakeActivityMarker, ...] = ()


@dataclass(frozen=True, slots=True)
class FakeEvidence:
    latest_usage: object | None = None
    copilot_session_id: str | None = None
    blocking_issue_kinds: tuple[str, ...] = ()
    error_messages: tuple[str, ...] = ()
    parse_result: FakeParseResult = field(default_factory=FakeParseResult)


class TestActivitySignalNone:
    def test_none_evidence_returns_false(self) -> None:
        assert _has_activity_signal(None) is False

    def test_empty_evidence_returns_false(self) -> None:
        assert _has_activity_signal(FakeEvidence()) is False


class TestActivitySignalIgnoresStaleData:
    """session_id, usage, blocking_issues, and boundaries should NOT count."""

    def test_session_id_alone_not_activity(self) -> None:
        ev = FakeEvidence(copilot_session_id="sess-123")
        assert _has_activity_signal(ev) is False

    def test_usage_alone_not_activity(self) -> None:
        ev = FakeEvidence(latest_usage=object())
        assert _has_activity_signal(ev) is False

    def test_blocking_issues_alone_not_activity(self) -> None:
        ev = FakeEvidence(blocking_issue_kinds=("rate_limit",))
        assert _has_activity_signal(ev) is False

    def test_boundaries_alone_not_activity(self) -> None:
        pr = FakeParseResult(boundaries=(object(),))
        ev = FakeEvidence(parse_result=pr)
        assert _has_activity_signal(ev) is False


class TestActivitySignalRecency:
    """Activity markers only count if they're in the tail of the output."""

    def test_old_activity_marker_not_detected(self) -> None:
        marker = FakeActivityMarker(span=FakeSpan(start_line=10, end_line=10))
        ui_marker = FakeUIMarker(span=FakeSpan(start_line=200, end_line=200))
        pr = FakeParseResult(activity_markers=(marker,), ui_markers=(ui_marker,))
        ev = FakeEvidence(parse_result=pr)
        assert _has_activity_signal(ev) is False

    def test_recent_activity_marker_detected(self) -> None:
        marker = FakeActivityMarker(span=FakeSpan(start_line=195, end_line=195))
        ui_marker = FakeUIMarker(span=FakeSpan(start_line=200, end_line=200))
        pr = FakeParseResult(activity_markers=(marker,), ui_markers=(ui_marker,))
        ev = FakeEvidence(parse_result=pr)
        assert _has_activity_signal(ev) is True

    def test_esc_to_cancel_always_signals_activity(self) -> None:
        esc_marker = FakeUIMarker(
            kind="esc_to_cancel",
            span=FakeSpan(start_line=1, end_line=1),
        )
        pr = FakeParseResult(ui_markers=(esc_marker,))
        ev = FakeEvidence(parse_result=pr)
        assert _has_activity_signal(ev) is True

    def test_non_esc_ui_marker_not_activity(self) -> None:
        marker = FakeUIMarker(
            kind="slash_commands",
            span=FakeSpan(start_line=200, end_line=200),
        )
        pr = FakeParseResult(ui_markers=(marker,))
        ev = FakeEvidence(parse_result=pr)
        assert _has_activity_signal(ev) is False


class TestActivitySignalErrors:
    """Error messages alone must NOT count as activity.

    `_ERROR_PATTERNS` match any `error:` / `fatal:` / `exception` /
    `traceback` line in the scrollback, which routinely appears in
    normal agent work (git output, compiler messages, stack traces
    being reviewed).  Treating those as activity locked agents in
    ERROR status while they were actually idle at a prompt — the
    exact bug this regression guards against.
    """

    def test_error_messages_do_not_signal_activity(self) -> None:
        ev = FakeEvidence(error_messages=("something failed",))
        assert _has_activity_signal(ev) is False

    def test_empty_error_messages_no_activity(self) -> None:
        ev = FakeEvidence(error_messages=())
        assert _has_activity_signal(ev) is False


class TestActivitySignalEdgeCases:
    def test_short_output_marker_at_tail(self) -> None:
        marker = FakeActivityMarker(span=FakeSpan(start_line=3, end_line=3))
        pr = FakeParseResult(activity_markers=(marker,))
        ev = FakeEvidence(parse_result=pr)
        assert _has_activity_signal(ev) is True

    def test_marker_at_boundary(self) -> None:
        marker = FakeActivityMarker(span=FakeSpan(start_line=70, end_line=70))
        ui = FakeUIMarker(span=FakeSpan(start_line=100, end_line=100))
        pr = FakeParseResult(activity_markers=(marker,), ui_markers=(ui,))
        ev = FakeEvidence(parse_result=pr)
        assert _has_activity_signal(ev) is True

    def test_marker_just_outside_boundary(self) -> None:
        marker = FakeActivityMarker(span=FakeSpan(start_line=69, end_line=69))
        ui = FakeUIMarker(span=FakeSpan(start_line=100, end_line=100))
        pr = FakeParseResult(activity_markers=(marker,), ui_markers=(ui,))
        ev = FakeEvidence(parse_result=pr)
        assert _has_activity_signal(ev) is False
