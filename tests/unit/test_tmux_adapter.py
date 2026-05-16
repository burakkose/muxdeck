# ruff: noqa: PT009, PT027

from __future__ import annotations

import unittest
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from muxdeck.adapters.tmux_adapter import (
    DISPLAY_MESSAGE_FORMAT,
    LIST_PANES_FORMAT,
    TmuxAdapter,
    parse_tmux_socket_path,
)
from muxdeck.domain.value_objects import CommandResult
from muxdeck.exceptions import CommandError, TmuxCommandError
from muxdeck.perf import summarize


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

    def test_list_windows_groups_panes_by_window(self) -> None:
        runner = FakeCommandRunner(
            results=[
                _command_result(
                    ("tmux", "list-panes"),
                    stdout=(
                        "session_name=muxdeck\tsession_id=$1\twindow_id=@2\twindow_index=2\t"
                        "window_name=editor\twindow_active=1\tpane_id=%9\tpane_index=0\t"
                        "pane_active=1\tpane_pid=101\tpane_tty=/dev/pts/9\t"
                        "pane_current_path=/repo\tpane_current_command=python\n"
                        "session_name=muxdeck\tsession_id=$1\twindow_id=@3\twindow_index=3\t"
                        "window_name=review\twindow_active=0\tpane_id=%10\tpane_index=0\t"
                        "pane_active=1\tpane_pid=102\tpane_tty=/dev/pts/10\t"
                        "pane_current_path=/repo\tpane_current_command=bash\n"
                        "session_name=muxdeck\tsession_id=$1\twindow_id=@3\twindow_index=3\t"
                        "window_name=review\twindow_active=0\tpane_id=%11\tpane_index=1\t"
                        "pane_active=0\tpane_pid=103\tpane_tty=/dev/pts/11\t"
                        "pane_current_path=/repo\tpane_current_command=bash"
                    ),
                )
            ]
        )

        windows = TmuxAdapter(runner).list_windows()

        self.assertEqual(len(windows), 2)
        self.assertEqual(windows[0].window_id, "@2")
        self.assertEqual(windows[0].pane_ids, ("%9",))
        self.assertEqual(windows[1].window_name, "review")
        self.assertEqual(windows[1].pane_count, 2)

    def test_break_pane_builds_command_and_returns_metadata(self) -> None:
        runner = FakeCommandRunner(
            results=[
                _command_result(
                    ("tmux", "break-pane"),
                    stdout=(
                        "session_name=muxdeck\tsession_id=$1\twindow_id=@4\twindow_index=4\t"
                        "window_name=solo\twindow_active=0\tpane_id=%9\tpane_index=0\t"
                        "pane_active=1\tpane_pid=101\tpane_tty=/dev/pts/9\t"
                        "pane_current_path=/repo\tpane_current_command=python\tpane_dead=0"
                    ),
                )
            ]
        )

        metadata = TmuxAdapter(runner).break_pane(
            "%9",
            window_name="solo",
            target_window="muxdeck:",
            detached=True,
        )

        self.assertEqual(
            runner.calls,
            [
                (
                    (
                        "tmux",
                        "break-pane",
                        "-P",
                        "-F",
                        DISPLAY_MESSAGE_FORMAT,
                        "-s",
                        "%9",
                        "-d",
                        "-n",
                        "solo",
                        "-t",
                        "muxdeck:",
                    ),
                    10.0,
                )
            ],
        )
        self.assertEqual(metadata.window_name, "solo")

    def test_join_pane_builds_command_then_reads_metadata(self) -> None:
        runner = FakeCommandRunner(
            results=[
                _command_result(("tmux", "join-pane")),
                _command_result(
                    ("tmux", "display-message"),
                    stdout=(
                        "session_name=muxdeck\tsession_id=$1\twindow_id=@3\twindow_index=3\t"
                        "window_name=review\twindow_active=1\tpane_id=%9\tpane_index=1\t"
                        "pane_active=0\tpane_pid=101\tpane_tty=/dev/pts/9\t"
                        "pane_current_path=/repo\tpane_current_command=python\tpane_dead=0"
                    ),
                ),
            ]
        )

        metadata = TmuxAdapter(runner).join_pane("%9", "@3", detached=True, vertical=False)

        self.assertEqual(
            runner.calls,
            [
                (("tmux", "join-pane", "-s", "%9", "-t", "@3", "-d", "-h"), 10.0),
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
                ),
            ],
        )
        self.assertEqual(metadata.window_id, "@3")

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

    def test_rename_window_builds_command(self) -> None:
        runner = FakeCommandRunner(
            results=[_command_result(("tmux", "rename-window", "-t", "@7", "agent-ui"))]
        )

        TmuxAdapter(runner).rename_window("@7", "agent-ui")

        self.assertEqual(
            runner.calls,
            [(("tmux", "rename-window", "-t", "@7", "agent-ui"), 10.0)],
        )

    def test_kill_pane_builds_command(self) -> None:
        runner = FakeCommandRunner(results=[_command_result(("tmux", "kill-pane", "-t", "%9"))])

        TmuxAdapter(runner).kill_pane("%9")

        self.assertEqual(
            runner.calls,
            [(("tmux", "kill-pane", "-t", "%9"), 10.0)],
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

    def test_capture_pane_with_escape_sequences_passes_minus_e(self) -> None:
        runner = FakeCommandRunner(
            results=[_command_result(("tmux", "capture-pane"), stdout="styled")],
        )
        out = TmuxAdapter(runner).capture_pane(
            "%1",
            join_wrapped_lines=True,
            include_escape_sequences=True,
        )
        self.assertEqual(out, "styled")
        # -J and -e must both be present; -t binds to the pane id.
        args = runner.calls[0][0]
        self.assertIn("-J", args)
        self.assertIn("-e", args)
        self.assertEqual(args[:2], ("tmux", "capture-pane"))
        self.assertIn("-p", args)
        t_index = args.index("-t")
        self.assertEqual(args[t_index + 1], "%1")

    def test_pipe_pane_to_file_builds_append_shell_command(self) -> None:
        runner = FakeCommandRunner(
            results=[_command_result(("tmux", "pipe-pane"), stdout="")],
        )
        TmuxAdapter(runner).pipe_pane_to_file(
            "%1",
            target_path=Path("/ring path/ring.log"),
            append=True,
        )
        args = runner.calls[0][0]
        self.assertEqual(args[:4], ("tmux", "pipe-pane", "-o", "-t"))
        self.assertEqual(args[4], "%1")
        # Path with spaces must be shlex-quoted so the spawned shell
        # doesn't split "/ring path/ring.log" into two tokens.
        self.assertTrue(args[5].startswith("cat >> "))
        self.assertIn("'/ring path/ring.log'", args[5])

    def test_pipe_pane_to_file_truncate_uses_single_redirect(self) -> None:
        runner = FakeCommandRunner(
            results=[_command_result(("tmux", "pipe-pane"), stdout="")],
        )
        TmuxAdapter(runner).pipe_pane_to_file(
            "%1",
            target_path=Path("/tmp/x"),
            append=False,
        )
        args = runner.calls[0][0]
        self.assertTrue(args[5].startswith("cat > "))

    def test_stop_pipe_pane_sends_bare_pipe_pane(self) -> None:
        runner = FakeCommandRunner(
            results=[_command_result(("tmux", "pipe-pane"), stdout="")],
        )
        TmuxAdapter(runner).stop_pipe_pane("%7")
        self.assertEqual(runner.calls[0][0], ("tmux", "pipe-pane", "-t", "%7"))

    def test_stop_pipe_pane_swallows_tmux_error(self) -> None:
        runner = FakeCommandRunner(
            results=[
                _command_result(
                    ("tmux", "pipe-pane"),
                    exit_code=1,
                    stderr="can't find pane: %404",
                ),
            ],
        )
        # Must not raise — a dead pane during teardown is normal.
        TmuxAdapter(runner).stop_pipe_pane("%404")

    def test_list_panes_and_capture_pane_record_perf_spans(self) -> None:
        runner = FakeCommandRunner(
            results=[
                _command_result(("tmux", "list-panes"), stdout=""),
                _command_result(("tmux", "capture-pane"), stdout=""),
            ]
        )
        summarize(reset=True)
        try:
            adapter = TmuxAdapter(runner)
            adapter.list_panes()
            adapter.capture_pane("%1")
        finally:
            spans = {s.name for s in summarize(reset=True)}
        self.assertIn("tmux.list_panes", spans)
        self.assertIn("tmux.capture_pane", spans)


class TmuxAdapterValidationTests(unittest.TestCase):
    """Constructor + validation paths."""

    def test_constructor_rejects_zero_or_negative_timeout(self) -> None:
        runner = FakeCommandRunner(results=[])
        with self.assertRaises(ValueError):
            TmuxAdapter(runner, timeout_sec=0)
        with self.assertRaises(ValueError):
            TmuxAdapter(runner, timeout_sec=-1.0)

    def test_with_socket_path_returns_new_instance_with_overridden_socket(self) -> None:
        runner = FakeCommandRunner(results=[])
        original = TmuxAdapter(runner, binary="tmux", timeout_sec=5.0, socket_path=None)
        cloned = original.with_socket_path(Path("/run/user/1000/tmux.sock"))
        # Same runner, same binary, same timeout, but different socket.
        self.assertIs(cloned._command_runner, runner)
        self.assertEqual(cloned._binary, "tmux")
        self.assertEqual(cloned._timeout_sec, 5.0)
        self.assertIsNotNone(cloned.socket_path)
        # Original is untouched.
        self.assertIsNone(original.socket_path)

    def test_set_socket_path_with_none_clears(self) -> None:
        runner = FakeCommandRunner(results=[])
        adapter = TmuxAdapter(runner, socket_path="/run/socket")
        self.assertIsNotNone(adapter.socket_path)
        adapter.set_socket_path(None)
        self.assertIsNone(adapter.socket_path)


class TmuxAdapterDisplayMetadataTests(unittest.TestCase):
    def test_display_pane_metadata_raises_when_metadata_missing(self) -> None:
        # display-message returns no parseable record → display_pane_metadata
        # converts the get_pane_metadata None into a TmuxCommandError.
        runner = FakeCommandRunner(
            results=[
                _command_result(
                    ("tmux", "display-message"),
                    exit_code=1,
                    stderr="can't find pane: %999",
                ),
            ],
        )
        with self.assertRaises(TmuxCommandError) as ctx:
            TmuxAdapter(runner).display_pane_metadata("%999")
        self.assertIn("pane not found", ctx.exception.stderr or "")

    def test_get_pane_metadata_returns_none_for_missing_target(self) -> None:
        runner = FakeCommandRunner(
            results=[
                _command_result(
                    ("tmux", "display-message"),
                    exit_code=1,
                    stderr="can't find session: foo",
                ),
            ],
        )
        self.assertIsNone(TmuxAdapter(runner).get_pane_metadata("foo"))

    def test_get_pane_metadata_re_raises_other_errors(self) -> None:
        runner = FakeCommandRunner(
            results=[
                _command_result(
                    ("tmux", "display-message"),
                    exit_code=1,
                    stderr="permission denied",
                ),
            ],
        )
        with self.assertRaises(TmuxCommandError):
            TmuxAdapter(runner).get_pane_metadata("%9")


class TmuxAdapterCapturePaneTests(unittest.TestCase):
    def test_capture_pane_with_start_and_end_lines(self) -> None:
        runner = FakeCommandRunner(
            results=[_command_result(("tmux", "capture-pane"), stdout="captured")],
        )
        out = TmuxAdapter(runner).capture_pane(
            "%5",
            start_line=-100,
            end_line=10,
            join_wrapped_lines=True,
            include_escape_sequences=True,
        )
        self.assertEqual(out, "captured")
        cmd = runner.calls[0][0]
        self.assertIn("-S", cmd)
        self.assertIn("-100", cmd)
        self.assertIn("-E", cmd)
        self.assertIn("10", cmd)
        self.assertIn("-J", cmd)
        self.assertIn("-e", cmd)


class TmuxAdapterSendKeysTests(unittest.TestCase):
    def test_send_keys_with_empty_keys_and_no_enter_raises(self) -> None:
        runner = FakeCommandRunner(results=[])
        with self.assertRaises(ValueError):
            TmuxAdapter(runner).send_keys("%1", ())

    def test_send_keys_empty_keys_with_append_enter_only_sends_enter(self) -> None:
        runner = FakeCommandRunner(
            results=[_command_result(("tmux", "send-keys"), stdout="")],
        )
        TmuxAdapter(runner).send_keys("%1", (), append_enter=True)
        # Single send-keys command with Enter appended.
        self.assertEqual(
            runner.calls[0][0],
            ("tmux", "send-keys", "-t", "%1", "Enter"),
        )

    def test_send_keys_literal_without_enter(self) -> None:
        runner = FakeCommandRunner(
            results=[_command_result(("tmux", "send-keys"), stdout="")],
        )
        TmuxAdapter(runner).send_keys("%1", ("abc",), literal=True, append_enter=False)
        self.assertEqual(
            runner.calls[0][0],
            ("tmux", "send-keys", "-t", "%1", "-l", "abc"),
        )


class TmuxAdapterSplitNewWindowTests(unittest.TestCase):
    def test_split_window_minimal_args_horizontal(self) -> None:
        runner = FakeCommandRunner(
            results=[
                _command_result(
                    ("tmux", "split-window"),
                    stdout=(
                        "session_name=s\tsession_id=$1\twindow_id=@2\twindow_index=3\t"
                        "window_name=w\twindow_active=1\tpane_id=%10\tpane_index=1\t"
                        "pane_active=0\tpane_pid=11\tpane_tty=/dev/pts/3\t"
                        "pane_current_path=/p\tpane_current_command=sh\tpane_dead=0"
                    ),
                ),
            ],
        )
        meta = TmuxAdapter(runner).split_window("%9")
        self.assertEqual(meta.pane_id, "%10")
        cmd = runner.calls[0][0]
        # Defaults: vertical=True
        self.assertIn("-v", cmd)
        # No -c, no -l, no -d, no shell_command appended.
        self.assertNotIn("-c", cmd)
        self.assertNotIn("-l", cmd)
        self.assertNotIn("-d", cmd)

    def test_new_window_minimal_args(self) -> None:
        runner = FakeCommandRunner(
            results=[
                _command_result(
                    ("tmux", "new-window"),
                    stdout=(
                        "session_name=s\tsession_id=$1\twindow_id=@22\twindow_index=4\t"
                        "window_name=w\twindow_active=1\tpane_id=%30\tpane_index=0\t"
                        "pane_active=1\tpane_pid=11\tpane_tty=/dev/pts/3\t"
                        "pane_current_path=/p\tpane_current_command=sh\tpane_dead=0"
                    ),
                ),
            ],
        )
        meta = TmuxAdapter(runner).new_window()
        self.assertEqual(meta.pane_id, "%30")
        cmd = runner.calls[0][0]
        # No optional args appended.
        self.assertNotIn("-t", cmd)
        self.assertNotIn("-n", cmd)
        self.assertNotIn("-c", cmd)
        self.assertNotIn("-d", cmd)

    def test_new_window_with_all_options(self) -> None:
        runner = FakeCommandRunner(
            results=[
                _command_result(
                    ("tmux", "new-window"),
                    stdout=(
                        "session_name=s\tsession_id=$1\twindow_id=@99\twindow_index=5\t"
                        "window_name=fancy\twindow_active=1\tpane_id=%99\tpane_index=0\t"
                        "pane_active=1\tpane_pid=11\tpane_tty=/dev/pts/3\t"
                        "pane_current_path=/repo\tpane_current_command=python\tpane_dead=0"
                    ),
                ),
            ],
        )
        TmuxAdapter(runner).new_window(
            "session-x",
            window_name="fancy",
            start_directory=Path("/repo"),
            shell_command=("python", "-V"),
            detached=True,
        )
        cmd = runner.calls[0][0]
        self.assertIn("-t", cmd)
        self.assertIn("session-x", cmd)
        self.assertIn("-n", cmd)
        self.assertIn("fancy", cmd)
        self.assertIn("-c", cmd)
        self.assertIn("/repo", cmd)
        self.assertIn("-d", cmd)
        self.assertIn("python", cmd)


class TmuxAdapterBreakJoinRenameKillTests(unittest.TestCase):
    def test_break_pane_rejects_blank_source(self) -> None:
        runner = FakeCommandRunner(results=[])
        with self.assertRaises(ValueError):
            TmuxAdapter(runner).break_pane("   ")

    def test_break_pane_rejects_blank_window_name(self) -> None:
        runner = FakeCommandRunner(results=[])
        with self.assertRaises(ValueError):
            TmuxAdapter(runner).break_pane("%9", window_name="   ")

    def test_break_pane_rejects_blank_target_window(self) -> None:
        runner = FakeCommandRunner(results=[])
        with self.assertRaises(ValueError):
            TmuxAdapter(runner).break_pane("%9", target_window="   ")

    def test_break_pane_appends_window_name_and_target_when_present(self) -> None:
        runner = FakeCommandRunner(
            results=[
                _command_result(
                    ("tmux", "break-pane"),
                    stdout=(
                        "session_name=s\tsession_id=$1\twindow_id=@9\twindow_index=2\t"
                        "window_name=w\twindow_active=1\tpane_id=%19\tpane_index=0\t"
                        "pane_active=1\tpane_pid=11\tpane_tty=/dev/pts/3\t"
                        "pane_current_path=/p\tpane_current_command=sh\tpane_dead=0"
                    ),
                ),
            ],
        )
        TmuxAdapter(runner).break_pane(
            "%9", window_name="newwin", target_window="@7", detached=False
        )
        cmd = runner.calls[0][0]
        self.assertIn("-n", cmd)
        self.assertIn("newwin", cmd)
        self.assertIn("@7", cmd)
        self.assertNotIn("-d", cmd)

    def test_join_pane_rejects_blank_source_or_target(self) -> None:
        runner = FakeCommandRunner(results=[])
        with self.assertRaises(ValueError):
            TmuxAdapter(runner).join_pane("   ", "%9")
        with self.assertRaises(ValueError):
            TmuxAdapter(runner).join_pane("%9", "   ")

    def test_join_pane_horizontal_no_detach(self) -> None:
        runner = FakeCommandRunner(
            results=[
                _command_result(("tmux", "join-pane"), stdout=""),
                _command_result(
                    ("tmux", "display-message"),
                    stdout=(
                        "session_name=s\tsession_id=$1\twindow_id=@9\twindow_index=2\t"
                        "window_name=w\twindow_active=1\tpane_id=%5\tpane_index=0\t"
                        "pane_active=1\tpane_pid=11\tpane_tty=/dev/pts/3\t"
                        "pane_current_path=/p\tpane_current_command=sh\tpane_dead=0"
                    ),
                ),
            ],
        )
        TmuxAdapter(runner).join_pane("%5", "%9", detached=False, vertical=False)
        join_cmd = runner.calls[0][0]
        self.assertIn("-h", join_cmd)
        self.assertNotIn("-d", join_cmd)

    def test_rename_window_validation(self) -> None:
        runner = FakeCommandRunner(results=[])
        with self.assertRaises(ValueError):
            TmuxAdapter(runner).rename_window("   ", "name")
        with self.assertRaises(ValueError):
            TmuxAdapter(runner).rename_window("@1", "   ")

    def test_rename_window_strips_inputs_and_runs_command(self) -> None:
        runner = FakeCommandRunner(
            results=[_command_result(("tmux", "rename-window"), stdout="")],
        )
        TmuxAdapter(runner).rename_window("  @1  ", "  hello  ")
        self.assertEqual(runner.calls[0][0], ("tmux", "rename-window", "-t", "@1", "hello"))

    def test_kill_pane_validates_and_strips(self) -> None:
        runner = FakeCommandRunner(results=[])
        with self.assertRaises(ValueError):
            TmuxAdapter(runner).kill_pane("  ")

        runner2 = FakeCommandRunner(
            results=[_command_result(("tmux", "kill-pane"), stdout="")],
        )
        TmuxAdapter(runner2).kill_pane("  %5  ")
        self.assertEqual(runner2.calls[0][0], ("tmux", "kill-pane", "-t", "%5"))


class TmuxAdapterClientControlTests(unittest.TestCase):
    def test_switch_client_runs_switch_client(self) -> None:
        runner = FakeCommandRunner(
            results=[_command_result(("tmux", "switch-client"), stdout="")],
        )
        TmuxAdapter(runner).switch_client("$2")
        self.assertEqual(runner.calls[0][0], ("tmux", "switch-client", "-t", "$2"))

    def test_has_attached_client_returns_false_on_tmux_error(self) -> None:
        runner = FakeCommandRunner(
            results=[
                _command_result(("tmux", "list-clients"), exit_code=1, stderr="boom"),
            ],
        )
        self.assertFalse(TmuxAdapter(runner).has_attached_client())

    def test_has_attached_client_returns_true_when_clients_attached(self) -> None:
        runner = FakeCommandRunner(
            results=[_command_result(("tmux", "list-clients"), stdout="client-1\n")],
        )
        self.assertTrue(TmuxAdapter(runner).has_attached_client())

    def test_has_attached_client_returns_false_when_blank_stdout(self) -> None:
        runner = FakeCommandRunner(
            results=[_command_result(("tmux", "list-clients"), stdout="\n")],
        )
        self.assertFalse(TmuxAdapter(runner).has_attached_client())

    def test_pane_is_dead_returns_true_when_metadata_missing(self) -> None:
        runner = FakeCommandRunner(
            results=[
                _command_result(
                    ("tmux", "display-message"),
                    exit_code=1,
                    stderr="can't find pane: %404",
                ),
            ],
        )
        self.assertTrue(TmuxAdapter(runner).pane_is_dead("%404"))

    def test_pane_is_dead_returns_false_when_metadata_alive(self) -> None:
        runner = FakeCommandRunner(
            results=[
                _command_result(
                    ("tmux", "display-message"),
                    stdout=(
                        "session_name=s\tsession_id=$1\twindow_id=@9\twindow_index=2\t"
                        "window_name=w\twindow_active=1\tpane_id=%5\tpane_index=0\t"
                        "pane_active=1\tpane_pid=11\tpane_tty=/dev/pts/3\t"
                        "pane_current_path=/p\tpane_current_command=sh\tpane_dead=0"
                    ),
                ),
            ],
        )
        self.assertFalse(TmuxAdapter(runner).pane_is_dead("%5"))


class TmuxAdapterParseMetadataTests(unittest.TestCase):
    def test_parse_pane_metadata_raises_when_zero_panes(self) -> None:
        # Empty stdout → 0 panes parsed → TmuxCommandError.
        runner = FakeCommandRunner(
            results=[_command_result(("tmux", "split-window"), stdout="")],
        )
        with self.assertRaises(TmuxCommandError):
            TmuxAdapter(runner).split_window("%9")

    def test_run_tmux_wraps_command_error(self) -> None:
        runner = FakeCommandRunner(
            results=[],
            errors=[
                CommandError(
                    "tmux list-clients",
                    exit_code=None,
                    stderr="boom",
                    stdout="",
                ),
            ],
        )
        with self.assertRaises(TmuxCommandError):
            TmuxAdapter(runner).list_panes()


class ParseTmuxSocketPathTests(unittest.TestCase):
    def test_returns_none_when_value_is_none(self) -> None:
        self.assertIsNone(parse_tmux_socket_path(None))

    def test_returns_none_when_value_is_blank(self) -> None:
        self.assertIsNone(parse_tmux_socket_path("   "))

    def test_strips_comma_metadata(self) -> None:
        result = parse_tmux_socket_path("/run/user/1000/tmux.sock,1,extra")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(str(result).endswith("tmux.sock"))

    def test_returns_none_when_socket_value_blank_after_split(self) -> None:
        # Comma immediately after blank → empty leading segment.
        self.assertIsNone(parse_tmux_socket_path(",foo,bar"))


if __name__ == "__main__":
    unittest.main()
