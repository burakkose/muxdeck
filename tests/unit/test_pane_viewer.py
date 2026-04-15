"""Tests for the PaneOutputPanel widget."""

from __future__ import annotations

from unittest.mock import patch

from rich.text import Text

from copilot_commander import theme
from copilot_commander.widgets.pane_viewer import PaneOutputPanel


def _make_panel() -> PaneOutputPanel:
    """Create a panel with ``update`` stubbed out (no running app needed)."""
    return PaneOutputPanel()


class TestSetOutput:
    def test_set_output_with_text(self) -> None:
        panel = _make_panel()
        with patch.object(panel, "update"):
            panel.set_output("hello world")
        assert panel._has_content is True

    def test_set_output_empty(self) -> None:
        panel = _make_panel()
        with patch.object(panel, "update"):
            panel.set_output("   \n  ")
        assert panel._has_content is False

    def test_set_output_with_pane_id(self) -> None:
        panel = _make_panel()
        with patch.object(panel, "update"):
            panel.set_output("some output", pane_id="%3")
        assert panel.border_title == "Agent Output [%3]"


class TestClearOutput:
    def test_clear_output(self) -> None:
        panel = _make_panel()
        with patch.object(panel, "update"):
            panel.set_output("data", pane_id="%1")
            assert panel._has_content is True

            panel.clear_output()
            assert panel._has_content is False
            assert panel.border_title == "Agent Output"


class TestStyleOutput:
    def test_style_output_command_lines(self) -> None:
        panel = PaneOutputPanel()
        result = panel._style_output("$ npm test")
        assert isinstance(result, Text)
        assert "npm test" in result.plain
        spans = result._spans
        assert any(f"bold {theme.GREEN}" in str(span.style) for span in spans)

    def test_style_output_error_lines(self) -> None:
        panel = PaneOutputPanel()
        result = panel._style_output("ERROR: something broke")
        assert "something broke" in result.plain
        spans = result._spans
        assert any(theme.RED in str(span.style) for span in spans)

    def test_style_output_warning_lines(self) -> None:
        panel = PaneOutputPanel()
        result = panel._style_output("WARNING: check config")
        assert "check config" in result.plain
        spans = result._spans
        assert any(theme.YELLOW in str(span.style) for span in spans)

    def test_style_output_plain_lines(self) -> None:
        panel = PaneOutputPanel()
        result = panel._style_output("just a normal line")
        assert "just a normal line" in result.plain
        spans = result._spans
        assert any(theme.FG in str(span.style) for span in spans)

    def test_style_output_mixed(self) -> None:
        panel = PaneOutputPanel()
        raw = "$ deploy\nAll good\nERROR: oops\nWARNING: careful"
        result = panel._style_output(raw)
        assert "deploy" in result.plain
        assert "All good" in result.plain
        assert "oops" in result.plain
        assert "careful" in result.plain

    def test_style_output_empty_lines(self) -> None:
        panel = PaneOutputPanel()
        result = panel._style_output("line1\n\nline3")
        assert result.plain == "line1\n\nline3"

    def test_style_output_box_drawing(self) -> None:
        panel = PaneOutputPanel()
        result = panel._style_output("╭─ header ─╮")
        spans = result._spans
        assert any(theme.BORDER in str(span.style) for span in spans)

    def test_style_output_marker_lines(self) -> None:
        panel = PaneOutputPanel()
        result = panel._style_output("⏺ Running task")
        spans = result._spans
        assert any(theme.BLUE in str(span.style) for span in spans)
