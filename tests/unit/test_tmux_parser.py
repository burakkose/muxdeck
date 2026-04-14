from __future__ import annotations

import unittest

from copilot_commander.parsers.tmux_parser import parse_tmux_list_panes_output


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

        self.assertEqual(len(result.panes), 2)
        self.assertEqual(result.ignored_lines, ("noise without pane metadata",))
        first, second = result.panes
        self.assertEqual(first.session_name, "muxdeck")
        self.assertEqual(first.window_index, 2)
        self.assertTrue(first.window_active)
        self.assertEqual(first.pane_pid, 4242)
        self.assertEqual(first.pane_current_path, "/repo/worktrees/task")
        self.assertEqual(first.pane_current_command, "python")
        self.assertEqual(second.pane_current_path, "/repo")
        self.assertIsNone(second.pane_current_command)

    def test_parse_tmux_list_panes_output_supports_pipe_separated_aliases(self) -> None:
        output = (
            "session=ops | window_id=@9 | window=logs | pane_id=%22 | tty=/dev/pts/9 | "
            "pane_active=yes"
        )

        result = parse_tmux_list_panes_output(output)

        self.assertEqual(len(result.panes), 1)
        pane = result.panes[0]
        self.assertEqual(pane.session_name, "ops")
        self.assertEqual(pane.window_name, "logs")
        self.assertEqual(pane.pane_tty, "/dev/pts/9")
        self.assertTrue(pane.pane_active)
        self.assertEqual(pane.raw_fields["session_name"], "ops")

    def test_parse_tmux_list_panes_output_ignores_blank_and_unparseable_lines(self) -> None:
        output = "\n\npane_id=%1\twindow_id=@1\nmalformed-token"

        result = parse_tmux_list_panes_output(output)

        self.assertEqual(len(result.panes), 1)
        self.assertEqual(result.ignored_lines, ("malformed-token",))


if __name__ == "__main__":
    unittest.main()
