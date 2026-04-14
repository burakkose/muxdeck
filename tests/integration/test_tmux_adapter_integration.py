# ruff: noqa: PTH101,PTH102,PTH103,PTH118,PTH123

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import stat
import textwrap
import unittest

from copilot_commander.adapters.process_adapter import ProcessAdapter
from copilot_commander.adapters.tmux_adapter import TmuxAdapter


class TmuxAdapterIntegrationTests(unittest.TestCase):
    def test_tmux_adapter_round_trips_through_process_adapter(self) -> None:
        with TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            temp_path = Path(temp_dir)
            fake_tmux = temp_path / "tmux"
            fake_tmux.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import sys

                    command = sys.argv[1]
                    if command == "list-panes":
                        print(
                            "session_name=muxdeck\\tsession_id=$1\\twindow_id=@2\\t"
                            "window_index=0\\twindow_name=editor\\twindow_active=1\\t"
                            "pane_id=%1\\tpane_index=0\\tpane_active=1\\tpane_pid=4242\\t"
                            "pane_tty=/dev/pts/7\\tpane_current_path=/repo\\t"
                            "pane_current_command=python"
                        )
                    elif command == "display-message":
                        print(
                            "session_name=muxdeck\\tsession_id=$1\\twindow_id=@2\\t"
                            "window_index=0\\twindow_name=editor\\twindow_active=1\\t"
                            "pane_id=%1\\tpane_index=0\\tpane_active=1\\tpane_pid=4242\\t"
                            "pane_tty=/dev/pts/7\\tpane_current_path=/repo\\t"
                            "pane_current_command=python\\tpane_dead=0"
                        )
                    elif command == "capture-pane":
                        print("pane line one\\npane line two")
                    elif command == "send-keys":
                        sys.stderr.write("sent")
                    else:
                        sys.stderr.write(f"unsupported command: {command}")
                        raise SystemExit(2)
                    """
                )
            )
            fake_tmux.chmod(fake_tmux.stat().st_mode | stat.S_IEXEC)
            adapter = TmuxAdapter(ProcessAdapter(), binary=str(fake_tmux))

            panes = adapter.list_panes()
            metadata = adapter.display_pane_metadata("%1")
            capture = adapter.capture_pane("%1")
            send_result = adapter.send_keys("%1", ("echo hi",), literal=True, append_enter=True)

        self.assertEqual(len(panes.panes), 1)
        self.assertEqual(panes.panes[0].pane_id, "%1")
        self.assertEqual(metadata.window_name, "editor")
        self.assertFalse(metadata.pane_dead)
        self.assertEqual(capture.splitlines(), ["pane line one", "pane line two"])
        self.assertEqual(send_result.stderr, "sent")


if __name__ == "__main__":
    unittest.main()
