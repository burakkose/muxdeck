# ruff: noqa: PT009

from __future__ import annotations

import unittest
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from copilot_commander.adapters.tmux_adapter import (
    DISPLAY_MESSAGE_FORMAT,
    LIST_PANES_FORMAT,
    TmuxAdapter,
    parse_tmux_socket_path,
)
from copilot_commander.domain.value_objects import CommandResult
from copilot_commander.exceptions import CommandError, TmuxCommandError


def _command_result(
    command: tuple[str, ...],
    *,
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 0,
) -> CommandResult:
    started_at = datetime(2025, 1, 1, tzinfo=UTC)
    return CommandResult(
        command=command,
        exit_code=exit_code,
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=1),
        stdout=stdout,
        stderr=stderr,
    )


@dataclass(slots=True)
class FakeCommandRunner:
    results: list[CommandResult]
    errors: list[CommandError] | None = None
    calls: list[tuple[tuple[str, ...], float | None]] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self.errors = [] if self.errors is None else self.errors

    def run(
        self,
        command: Sequence[str],
        /,
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_sec: float | None = None,
    ) -> CommandResult:
        del cwd, env
        self.calls.append((tuple(command), timeout_sec))
        if self.errors:
            raise self.errors.pop(0)
        return self.results.pop(0)


class TmuxAdapterTests(unittest.TestCase):
    def test_list_panes_uses_all_flag_and_parser(self) -> None:
        runner = FakeCommandRunner(
            results=[
                _command_result(
                    ("tmux", "list-panes"),
                    stdout=(
                        "session_name=muxdeck\tsession_id=$1\twindow_id=@2\twindow_index=3\t"
                        "window_name=editor\twindow_active=1\tpane_id=%9\tpane_index=0\t"
                        "pane_active=1\tpane_pid=101\tpane_tty=/dev/pts/9\t"
                        "pane_current_path=/repo\tpane_current_command=python\n"
                        "garbage"
                    ),
                )
            ]
        )

        result = TmuxAdapter(runner).list_panes()

        self.assertEqual(
            runner.calls,
            [(("tmux", "list-panes", "-a", "-F", LIST_PANES_FORMAT), 10.0)],
        )
        self.assertEqual(len(result.panes), 1)
        self.assertEqual(result.panes[0].pane_id, "%9")
        self.assertEqual(result.ignored_lines, ("garbage",))

    def test_list_panes_prefixes_selected_socket_path(self) -> None:
        runner = FakeCommandRunner(results=[_command_result(("tmux", "list-panes"), stdout="")])

        TmuxAdapter(runner, socket_path="/tmp/tmux-1000/custom").list_panes()

        self.assertEqual(
            runner.calls,
            [
                (
                    (
                        "tmux",
                        "-S",
                        "/tmp/tmux-1000/custom",
                        "list-panes",
                        "-a",
                        "-F",
                        LIST_PANES_FORMAT,
                    ),
                    10.0,
                )
            ],
        )

    def test_display_pane_metadata_parses_dead_state(self) -> None:
        runner = FakeCommandRunner(
            results=[
                _command_result(
                    ("tmux", "display-message"),
                    stdout=(
                        "session_name=muxdeck\tsession_id=$1\twindow_id=@2\twindow_index=3\t"
                        "window_name=editor\twindow_active=1\tpane_id=%9\tpane_index=0\t"
                        "pane_active=1\tpane_pid=101\tpane_tty=/dev/pts/9\t"
                        "pane_current_path=/repo\tpane_current_command=python\tpane_dead=1"
                    ),
                )
            ]
        )

        metadata = TmuxAdapter(runner).display_pane_metadata("%9")

        self.assertEqual(
            runner.calls,
            [
                (
                    (
                        "tmux",
                        "display-message",
                        "-p",
                        "-t",
                        "%9",
                        "-F",
                        DISPLAY_MESSAGE_FORMAT,
                    ),
                    10.0,
                )
            ],
        )
        self.assertEqual(metadata.pane_id, "%9")
        self.assertEqual(metadata.window_name, "editor")
        self.assertTrue(metadata.pane_dead)

    def test_send_keys_builds_literal_command_and_enter(self) -> None:
        runner = FakeCommandRunner(
            results=[
                _command_result(("tmux", "send-keys", "-t", "%7", "-l", "echo hello")),
                _command_result(("tmux", "send-keys", "-t", "%7", "Enter")),
            ]
        )

        TmuxAdapter(runner).send_keys("%7", ("echo hello",), literal=True, append_enter=True)

        self.assertEqual(
            runner.calls,
            [
                (("tmux", "send-keys", "-t", "%7", "-l", "echo hello"), 10.0),
                (("tmux", "send-keys", "-t", "%7", "Enter"), 10.0),
            ],
        )

    def test_split_window_builds_command_and_returns_metadata(self) -> None:
        runner = FakeCommandRunner(
            results=[
                _command_result(
                    ("tmux", "split-window"),
                    stdout=(
                        "session_name=muxdeck\tsession_id=$1\twindow_id=@2\twindow_index=3\t"
                        "window_name=editor\twindow_active=1\tpane_id=%10\tpane_index=1\t"
                        "pane_active=0\tpane_pid=202\tpane_tty=/dev/pts/10\t"
                        "pane_current_path=/repo\tpane_current_command=bash\tpane_dead=0"
                    ),
                )
            ]
        )
        temp_path = Path("/repo/worktree")

        metadata = TmuxAdapter(runner).split_window(
            "%9",
            vertical=False,
            start_directory=temp_path,
            shell_command=("python3", "-V"),
            size=12,
            detached=True,
        )

        self.assertEqual(
            runner.calls,
            [
                (
                    (
                        "tmux",
                        "split-window",
                        "-P",
                        "-F",
                        DISPLAY_MESSAGE_FORMAT,
                        "-t",
                        "%9",
                        "-h",
                        "-c",
                        str(temp_path),
                        "-l",
                        "12",
                        "-d",
                        "python3",
                        "-V",
                    ),
                    10.0,
                )
            ],
        )
        self.assertEqual(metadata.pane_id, "%10")
        self.assertFalse(metadata.pane_dead)

    def test_new_window_builds_command_and_returns_metadata(self) -> None:
        runner = FakeCommandRunner(
            results=[
                _command_result(
                    ("tmux", "new-window"),
                    stdout=(
                        "session_name=muxdeck\tsession_id=$1\twindow_id=@4\twindow_index=4\t"
                        "window_name=logs\twindow_active=0\tpane_id=%20\tpane_index=0\t"
                        "pane_active=1\tpane_pid=404\tpane_tty=/dev/pts/20\t"
                        "pane_current_path=/repo/logs\tpane_current_command=bash\tpane_dead=0"
                    ),
                )
            ]
        )
        temp_path = Path("/repo/logs")

        metadata = TmuxAdapter(runner).new_window(
            "muxdeck:",
            window_name="logs",
            start_directory=temp_path,
            shell_command=("tail", "-f", "app.log"),
            detached=True,
        )

        self.assertEqual(
            runner.calls,
            [
                (
                    (
                        "tmux",
                        "new-window",
                        "-P",
                        "-F",
                        DISPLAY_MESSAGE_FORMAT,
                        "-t",
                        "muxdeck:",
                        "-n",
                        "logs",
                        "-c",
                        str(temp_path),
                        "-d",
                        "tail",
                        "-f",
                        "app.log",
                    ),
                    10.0,
                )
            ],
        )
        self.assertEqual(metadata.window_name, "logs")
        self.assertEqual(metadata.pane_id, "%20")

    def test_pane_exists_returns_false_for_missing_target(self) -> None:
        runner = FakeCommandRunner(
            results=[],
            errors=[TmuxCommandError("tmux display-message", stderr="can't find pane: %99")],
        )

        exists = TmuxAdapter(runner).pane_exists("%99")

        self.assertFalse(exists)

    def test_server_outage_error_propagates(self) -> None:
        runner = FakeCommandRunner(
            results=[],
            errors=[
                TmuxCommandError(
                    "tmux display-message",
                    stderr="no server running on /tmp/tmux",
                )
            ],
        )

        with pytest.raises(TmuxCommandError) as context:
            TmuxAdapter(runner).pane_exists("%99")

        self.assertIn("no server running", context.value.stderr or "")

    def test_non_zero_tmux_exit_raises_tmux_command_error(self) -> None:
        runner = FakeCommandRunner(
            results=[
                _command_result(
                    ("tmux", "select-pane", "-t", "%99"),
                    exit_code=1,
                    stderr="can't find pane: %99",
                )
            ]
        )

        with pytest.raises(TmuxCommandError) as context:
            TmuxAdapter(runner).select_pane("%99")

        self.assertEqual(context.value.exit_code, 1)
        self.assertEqual(context.value.stderr, "can't find pane: %99")

    def test_select_window_builds_command(self) -> None:
        runner = FakeCommandRunner(
            results=[_command_result(("tmux", "select-window", "-t", "=muxdeck:@7"))]
        )

        TmuxAdapter(runner).select_window("=muxdeck:@7")

        self.assertEqual(
            runner.calls,
            [(("tmux", "select-window", "-t", "=muxdeck:@7"), 10.0)],
        )

    def test_runner_command_error_is_wrapped(self) -> None:
        runner = FakeCommandRunner(
            results=[],
            errors=[CommandError("tmux list-panes -a", stderr="timed out after 10.000s")],
        )

        with pytest.raises(TmuxCommandError) as context:
            TmuxAdapter(runner).list_panes()

        self.assertEqual(context.value.command, "tmux list-panes -a")
        self.assertEqual(context.value.stderr, "timed out after 10.000s")

    def test_parse_tmux_socket_path_reads_tmux_env_value(self) -> None:
        self.assertEqual(
            parse_tmux_socket_path("/tmp/tmux-1000/default,1234,0"),
            Path("/tmp/tmux-1000/default"),
        )


if __name__ == "__main__":
    unittest.main()
