# ruff: noqa: PT009, PT027

from __future__ import annotations

import unittest
from typing import cast

from muxdeck.parsers.tmux_parser import parse_tmux_list_panes_output


class TmuxParserTests(unittest.TestCase):
    def test_parse_tmux_list_panes_output_with_tab_separated_metadata(self) -> None:
        output = "\n".join(
            (
                (
                    "session_name=muxdeck\tsession_id=$1\twindow_id=@3\twindow_index=2\t"
                    "window_name=editor\twindow_active=1\tpane_id=%11\tpane_index=0\t"
                    "pane_active=1\tpane_pid=4242\tpane_tty=/dev/pts/7\t"
                    "pane_current_path=/repo/worktrees/task\tpane_current_command=python"
                ),
                "noise without pane metadata",
                (
                    "session_name=muxdeck\twindow_id=@4\tpane_id=%12\tpane_index=1\t"
                    "pane_active=0\tcwd=/repo\tcurrent_command="
                ),
            )
        )

        result = parse_tmux_list_panes_output(output)

        assert len(result.panes) == 2
        assert result.ignored_lines == ("noise without pane metadata",)
        first, second = result.panes
        assert first.session_name == "muxdeck"
        assert first.window_index == 2
        assert first.window_active
        assert first.pane_pid == 4242
        assert first.pane_current_path == "/repo/worktrees/task"
        assert first.pane_current_command == "python"
        assert second.pane_current_path == "/repo"
        assert second.pane_current_command is None

    def test_parse_tmux_list_panes_output_supports_pipe_separated_aliases(self) -> None:
        output = (
            "session=ops | window_id=@9 | window=logs | pane_id=%22 | tty=/dev/pts/9 | "
            "pane_active=yes"
        )

        result = parse_tmux_list_panes_output(output)

        assert len(result.panes) == 1
        pane = result.panes[0]
        assert pane.session_name == "ops"
        assert pane.window_name == "logs"
        assert pane.pane_tty == "/dev/pts/9"
        assert pane.pane_active
        assert cast("dict[str, str]", pane.raw_fields)["session_name"] == "ops"

    def test_parse_tmux_list_panes_output_ignores_blank_and_unparseable_lines(self) -> None:
        output = "\n\npane_id=%1\twindow_id=@1\nmalformed-token"

        result = parse_tmux_list_panes_output(output)

        assert len(result.panes) == 1
        assert result.ignored_lines == ("malformed-token",)


class TmuxParserHelperBranchTests(unittest.TestCase):
    """Direct tests for the small helpers that the integration tests miss."""

    def test_normalize_optional_text_strips_matching_quotes(self) -> None:
        from muxdeck.parsers.tmux_parser import _normalize_optional_text

        self.assertEqual(_normalize_optional_text('"  inside  "'), "  inside  ")
        self.assertEqual(_normalize_optional_text("'value'"), "value")
        self.assertEqual(_normalize_optional_text("plain"), "plain")
        self.assertIsNone(_normalize_optional_text("   "))
        self.assertIsNone(_normalize_optional_text(None))

    def test_parse_optional_int_returns_none_for_non_numeric(self) -> None:
        from muxdeck.parsers.tmux_parser import _parse_optional_int

        self.assertIsNone(_parse_optional_int("hello"))
        self.assertIsNone(_parse_optional_int("  "))
        self.assertEqual(_parse_optional_int(" 42 "), 42)

    def test_parse_optional_bool_handles_unknown_falsy_truthy(self) -> None:
        from muxdeck.parsers.tmux_parser import _parse_optional_bool

        self.assertTrue(_parse_optional_bool("YES"))
        self.assertFalse(_parse_optional_bool("Off"))
        self.assertIsNone(_parse_optional_bool("maybe"))
        self.assertIsNone(_parse_optional_bool(None))

    def test_parse_with_pipe_and_multiple_equals_uses_pipe_split(self) -> None:
        # Line with single-pipe separators (no " | " spacing) and >1 "=" should
        # still be parsed via the third branch in _split_segments.
        line = "pane_id=%99|window_id=@99|pane_active=1"

        result = parse_tmux_list_panes_output(line)

        self.assertEqual(len(result.panes), 1)
        pane = result.panes[0]
        self.assertEqual(pane.pane_id, "%99")
        self.assertEqual(pane.window_id, "@99")
        self.assertTrue(pane.pane_active)


if __name__ == "__main__":
    unittest.main()
