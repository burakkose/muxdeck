"""Tests for :class:`LivePaneViewer`.

The widget wraps :class:`textual.widgets.RichLog`; we only poke its
pure-Python bookkeeping here (line cap, ``append`` semantics) so the
tests don't need a running Textual app.
"""

from __future__ import annotations

from rich.text import Text

from copilot_commander.widgets.live_pane_viewer import LivePaneViewer


class TestAppend:
    def test_plain_string_populates_buffer(self) -> None:
        viewer = LivePaneViewer()
        viewer.append("alpha\nbeta\n")
        assert viewer.buffer_line_count == 2
        assert viewer.has_content is True

    def test_ansi_string_is_decoded(self) -> None:
        viewer = LivePaneViewer()
        viewer.append("\x1b[31mred\x1b[0m line\n")
        # Buffer stores plain lines — styles are on the RichLog side.
        assert viewer.buffer_line_count == 1

    def test_text_object_is_accepted(self) -> None:
        viewer = LivePaneViewer()
        viewer.append(Text("typed\nrich\n"))
        assert viewer.buffer_line_count == 2
        assert viewer.has_content is True

    def test_empty_string_is_noop(self) -> None:
        viewer = LivePaneViewer()
        viewer.append("")
        assert viewer.buffer_line_count == 0
        assert viewer.has_content is False

    def test_empty_text_object_is_noop(self) -> None:
        viewer = LivePaneViewer()
        viewer.append(Text(""))
        assert viewer.buffer_line_count == 0
        assert viewer.has_content is False


class TestLineCap:
    def test_retains_only_last_n_lines(self) -> None:
        viewer = LivePaneViewer(max_lines=5)
        for i in range(20):
            viewer.append(f"line {i}\n")
        assert viewer.buffer_line_count == 5

    def test_cap_respected_across_multi_line_chunks(self) -> None:
        viewer = LivePaneViewer(max_lines=3)
        viewer.append("one\ntwo\nthree\nfour\nfive\n")
        assert viewer.buffer_line_count == 3

    def test_clear_buffer_resets_state(self) -> None:
        viewer = LivePaneViewer(max_lines=4)
        viewer.append("a\nb\nc\n")
        assert viewer.has_content is True
        viewer.clear_buffer()
        assert viewer.buffer_line_count == 0
        assert viewer.has_content is False
