# ruff: noqa: ANN001,ANN201,ANN202,ANN003,E501

"""Tests for discovery filtering: shell detection, self-pane exclusion, strong evidence."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock

from muxdeck.adapters.copilot_adapter import (
    CopilotCommandDetection,
    CopilotOutputParseResult,
    CopilotSessionEvidence,
)
from muxdeck.services.discovery_service import (
    DiscoveryPaneSnapshot,
    _has_session_signal,
    _has_strong_session_signal,
    _is_shell_command,
    classify_pane,
)

_TS = datetime(2025, 1, 1, tzinfo=UTC)
_NO_COPILOT = CopilotCommandDetection(
    candidate=("zsh",), is_likely_copilot=False, reason="no match"
)
_YES_COPILOT = CopilotCommandDetection(
    candidate=("copilot",), is_likely_copilot=True, reason="copilot"
)
_EMPTY_PARSE = CopilotOutputParseResult(
    session_ids=(),
    boundaries=(),
    usage_snapshots=(),
    blocking_issues=(),
    errors=(),
    ui_markers=(),
    evidence_spans=(),
)


def _snapshot(*, command: str = "node", pane_id: str = "%1") -> DiscoveryPaneSnapshot:
    return DiscoveryPaneSnapshot(
        pane_id=pane_id,
        tmux_session_name="main",
        tmux_window_id="@0",
        pane_current_command=command,
    )


def _weak_evidence() -> CopilotSessionEvidence:
    """Evidence with only UI markers — weak signal."""
    parse = CopilotOutputParseResult(
        session_ids=(),
        boundaries=(MagicMock(),),
        usage_snapshots=(),
        blocking_issues=(),
        errors=(),
        ui_markers=(MagicMock(),),
        evidence_spans=(),
    )
    return CopilotSessionEvidence(
        parse_result=parse,
        copilot_session_id=None,
        session_ids=(),
        usage_snapshots=(),
        latest_usage=None,
        blocking_issue_kinds=(),
        error_messages=(),
    )


def _strong_evidence() -> CopilotSessionEvidence:
    """Evidence with a session ID — strong signal."""
    return CopilotSessionEvidence(
        parse_result=_EMPTY_PARSE,
        copilot_session_id="abc-123",
        session_ids=(),
        usage_snapshots=(),
        latest_usage=None,
        blocking_issue_kinds=(),
        error_messages=(),
    )


class TestShellCommandDetection(unittest.TestCase):
    def test_common_shells_are_detected(self):
        for shell in ("bash", "zsh", "fish", "sh", "dash", "ksh"):
            assert _is_shell_command(shell), f"{shell} should be detected as shell"

    def test_non_shells(self):
        for cmd in ("node", "python", "copilot", "vim", None):
            assert not _is_shell_command(cmd), f"{cmd!r} should not be a shell"

    def test_case_insensitive(self):
        assert _is_shell_command("ZSH")
        assert _is_shell_command("Bash")


class TestStrongVsWeakEvidence(unittest.TestCase):
    def test_weak_evidence_has_signal(self):
        assert _has_session_signal(_weak_evidence())

    def test_weak_evidence_not_strong(self):
        assert not _has_strong_session_signal(_weak_evidence())

    def test_strong_evidence_is_strong(self):
        assert _has_strong_session_signal(_strong_evidence())

    def test_none_evidence(self):
        assert not _has_session_signal(None)
        assert not _has_strong_session_signal(None)


class TestShellPaneClassification(unittest.TestCase):
    def test_shell_with_weak_evidence_is_non_agent(self):
        """A zsh pane with only old scrollback markers should be non_agent."""
        result = classify_pane(
            _snapshot(command="zsh"),
            discovered_at=_TS,
            command_detection=_NO_COPILOT,
            session_evidence=_weak_evidence(),
        )
        assert result.classification == "non_agent_pane"

    def test_shell_with_copilot_command_is_agent(self):
        """A shell pane where process tree matched copilot should be agent."""
        result = classify_pane(
            _snapshot(command="zsh"),
            discovered_at=_TS,
            command_detection=_YES_COPILOT,
        )
        assert result.classification == "unmanaged_probable_agent"

    def test_non_shell_with_weak_evidence_is_agent(self):
        """Non-shell panes with any signal (including markers) are agents."""
        result = classify_pane(
            _snapshot(command="node"),
            discovered_at=_TS,
            command_detection=_NO_COPILOT,
            session_evidence=_weak_evidence(),
        )
        assert result.classification == "unmanaged_probable_agent"

    def test_non_shell_with_strong_evidence_is_agent(self):
        """Non-shell panes with session ID are agents."""
        result = classify_pane(
            _snapshot(command="node"),
            discovered_at=_TS,
            command_detection=_NO_COPILOT,
            session_evidence=_strong_evidence(),
        )
        assert result.classification == "unmanaged_probable_agent"

    def test_copilot_command_always_wins(self):
        """Direct copilot command detection is strongest signal."""
        result = classify_pane(
            _snapshot(command="node"),
            discovered_at=_TS,
            command_detection=_YES_COPILOT,
        )
        assert result.classification == "unmanaged_probable_agent"
        assert result.confidence == Decimal("0.8400")


class TestSelfPaneFiltering(unittest.TestCase):
    def test_ignore_pane_ids_filters_self(self):
        from muxdeck.adapters.tmux_adapter import TmuxPaneMetadata
        from muxdeck.services.discovery_service import DiscoveryService

        pane0 = TmuxPaneMetadata(
            pane_id="%0",
            session_name="main",
            window_id="@0",
            pane_pid=1000,
            pane_current_command="python",
        )
        pane1 = TmuxPaneMetadata(
            pane_id="%1",
            session_name="main",
            window_id="@0",
            pane_pid=2000,
            pane_current_command="node",
        )

        class FakeTmux:
            def list_panes(self):
                return [pane0, pane1]

            def capture_pane(self, target, /, **kw):
                return ""

        class FakeCopilot:
            def detect_command(self, candidate, /):
                return _NO_COPILOT

            def interpret_output(self, output, /):
                return None

        class FakeStore:
            def get_agent_by_pane_id(self, pane_id, /):
                return None

            def get_agent_by_copilot_session_id(self, sid, /):
                return None

            def get_session_by_copilot_session_id(self, sid, /):
                return None

            def get_session_context_by_tmux_pane_id(self, pane_id, /):
                return None

        svc = DiscoveryService(
            FakeTmux(),
            FakeCopilot(),
            FakeStore(),
            ignore_pane_ids=frozenset({"%0"}),
        )
        report = svc.discover_panes()
        pane_ids = [p.snapshot.pane_id for p in report.panes]
        assert "%0" not in pane_ids
        assert "%1" in pane_ids
