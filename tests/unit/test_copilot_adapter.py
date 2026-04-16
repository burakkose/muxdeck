# ruff: noqa: PT027

from __future__ import annotations

import unittest
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from copilot_commander.adapters.copilot_adapter import (
    CopilotAdapter,
    CopilotCommandError,
    CopilotLaunchParameters,
)
from copilot_commander.domain.value_objects import CommandResult
from copilot_commander.exceptions import CommandError


class FakeRunner:
    def __init__(self, responses: Sequence[CommandResult | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[tuple[str, ...], Path | None, float | None]] = []

    def run(
        self,
        command: Sequence[str],
        /,
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_sec: float | None = None,
    ) -> CommandResult:
        del env
        self.calls.append((tuple(command), cwd, timeout_sec))
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class CopilotAdapterTests(unittest.TestCase):
    def test_build_tmux_launch_command_for_pane(self) -> None:
        adapter = CopilotAdapter(FakeRunner(()))
        parameters = CopilotLaunchParameters(
            pane_target="%7",
            prompt="summarize repo status",
            cwd="/repo/worktrees/task-one",
            model="gpt-5.4",
            extra_args=("--approval-mode", "never"),
        )

        launch_command = adapter.build_tmux_launch_command(parameters)

        assert launch_command.cli_command == (
            "copilot",
            "chat",
            "--model",
            "gpt-5.4",
            "--approval-mode",
            "never",
            "summarize repo status",
        )
        expected_shell = (
            "cd /repo/worktrees/task-one && copilot chat --model gpt-5.4 "
            "--approval-mode never 'summarize repo status'"
        )
        assert launch_command.tmux_command == (
            "tmux",
            "send-keys",
            "-t",
            "%7",
            expected_shell,
            "Enter",
        )

    def test_build_prompt_submission_sends_enter_separately(self) -> None:
        adapter = CopilotAdapter(FakeRunner(()))

        submission = adapter.build_prompt_submission("%3", "fix failing test", append_enter=True)

        assert submission.tmux_commands == (
            ("tmux", "send-keys", "-t", "%3", "-l", "fix failing test"),
            ("tmux", "send-keys", "-t", "%3", "Enter"),
        )
        assert submission.append_enter is True

    def test_submit_prompt_runs_all_tmux_commands(self) -> None:
        runner = FakeRunner(
            (
                _result(("tmux", "send-keys", "-t", "%5", "-l", "status")),
                _result(("tmux", "send-keys", "-t", "%5", "Enter")),
            )
        )
        adapter = CopilotAdapter(runner)

        outcome = adapter.submit_prompt("%5", "status")

        assert outcome.submission.prompt == "status"
        assert outcome.command_results[0].command == (
            "tmux",
            "send-keys",
            "-t",
            "%5",
            "-l",
            "status",
        )
        assert outcome.command_results[1].command == ("tmux", "send-keys", "-t", "%5", "Enter")

    def test_detect_command_heuristics_match_common_invocations(self) -> None:
        adapter = CopilotAdapter(FakeRunner(()))

        detections = adapter.detect_commands(
            (
                "copilot chat",
                ("gh", "copilot", "suggest"),
                "node /opt/bin/github-copilot-cli.js",
                "npm exec gh copilot suggest",
                "python worker.py",
            )
        )

        assert [detection.is_likely_copilot for detection in detections] == [
            True,
            True,
            True,
            True,
            False,
        ]
        assert [detection.reason for detection in detections] == [
            "copilot_binary",
            "gh_extension",
            "wrapped_copilot_binary",
            "wrapped_gh_extension",
            "no_copilot_signature",
        ]
        assert adapter.detect_process_name("github-copilot-cli.js").reason == "process_name"
        assert adapter.detect_process_name("gh").is_likely_copilot is False

    def test_interpret_output_uses_parser_suite(self) -> None:
        adapter = CopilotAdapter(FakeRunner(()))
        output = "\n".join(
            (
                "Copilot session id: session-01HZX9ABCDEF",
                "Prompt: summarize repo status",
                "Response: working on it",
                "waiting for confirmation before applying patch",
                "CONFLICT (content): merge conflict in src/app.py",
                "fatal: build aborted",
                "input_tokens: 1,200",
                "output_tokens: 345",
            )
        )

        evidence = adapter.interpret_output(output)
        latest_usage = evidence.latest_usage

        assert evidence.copilot_session_id == "session-01HZX9ABCDEF"
        assert evidence.session_ids == ("session-01HZX9ABCDEF",)
        assert evidence.blocking_issue_kinds == (
            "waiting_for_confirmation",
            "merge_conflict",
        )
        assert evidence.error_messages == ("fatal: build aborted",)
        assert evidence.parse_result.boundaries[0].kind == "prompt_start"
        assert latest_usage is not None
        assert latest_usage.input_tokens == 1200
        assert evidence.usage_snapshots[0].output_tokens == 345

    def test_interpret_command_result_combines_stdout_and_stderr(self) -> None:
        adapter = CopilotAdapter(FakeRunner(()))
        result = _result(
            ("copilot", "chat"),
            stdout="session id: session-01ABC\ninput_tokens: 3",
            stderr="error: auth expired",
        )

        evidence = adapter.interpret_command_result(result)
        latest_usage = evidence.latest_usage

        assert evidence.copilot_session_id == "session-01ABC"
        assert evidence.error_messages == ("error: auth expired",)
        assert latest_usage is not None
        assert latest_usage.input_tokens == 3

    def test_launch_in_pane_wraps_runner_failures(self) -> None:
        runner = FakeRunner((CommandError("tmux send-keys", stderr="can't find pane: %9"),))
        adapter = CopilotAdapter(runner)
        parameters = CopilotLaunchParameters(pane_target="%9", prompt="status")

        with self.assertRaises(CopilotCommandError):
            adapter.launch_in_pane(parameters)


def _result(
    command: tuple[str, ...],
    *,
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 0,
    cwd: Path | None = None,
) -> CommandResult:
    started_at = datetime(2024, 1, 1, tzinfo=UTC)
    return CommandResult(
        command=command,
        exit_code=exit_code,
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=1),
        stdout=stdout,
        stderr=stderr,
        cwd=cwd,
    )


if __name__ == "__main__":
    unittest.main()
