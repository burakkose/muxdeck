# ruff: noqa: PT027

from __future__ import annotations

import unittest
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from muxdeck.adapters.copilot_adapter import (
    CopilotAdapter,
    CopilotCommandError,
    CopilotLaunchParameters,
)
from muxdeck.domain.value_objects import CommandResult
from muxdeck.exceptions import CommandError


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

    def test_interpret_output_leaves_session_id_unset_when_capture_is_ambiguous(self) -> None:
        adapter = CopilotAdapter(FakeRunner(()))
        output = "\n".join(
            (
                "Copilot session id: session-outer",
                "some nested tmux layout",
                "Copilot session id: session-inner",
            )
        )

        evidence = adapter.interpret_output(output)

        assert evidence.session_ids == ("session-outer", "session-inner")
        assert evidence.copilot_session_id is None

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

    def test_interpret_output_drops_errors_outside_tail_window(self) -> None:
        """`error:` lines far above the tail of the capture are noise.

        Regression: a `git rebase` leaving `error: could not apply <sha>`
        deep in the scrollback flipped the agent to ERROR even while the
        agent was sitting idle at a prompt, because `interpret_output`
        surfaced every match from the full capture.  Only errors within
        the tail window are relevant to current state; everything else
        is historical scrollback noise.
        """
        adapter = CopilotAdapter(FakeRunner(()))
        buried_error = "\n".join(
            ["error: could not apply 15c1344", *[f"filler line {i}" for i in range(80)]]
        )

        evidence = adapter.interpret_output(buried_error)

        assert evidence.error_messages == ()

    def test_interpret_output_keeps_errors_inside_tail_window(self) -> None:
        adapter = CopilotAdapter(FakeRunner(()))
        tail_error = "\n".join([*[f"filler line {i}" for i in range(80)], "fatal: build aborted"])

        evidence = adapter.interpret_output(tail_error)

        assert evidence.error_messages == ("fatal: build aborted",)

    def test_launch_in_pane_wraps_runner_failures(self) -> None:
        runner = FakeRunner((CommandError("tmux send-keys", stderr="can't find pane: %9"),))
        adapter = CopilotAdapter(runner)
        parameters = CopilotLaunchParameters(pane_target="%9", prompt="status")

        with self.assertRaises(CopilotCommandError):
            adapter.launch_in_pane(parameters)

    def test_configured_model_reads_copilot_config(self) -> None:
        adapter = CopilotAdapter(
            FakeRunner(()),
            config_path=Path(__file__).resolve().parents[1] / "fixtures" / "copilot_config.json",
        )

        assert adapter.configured_model() == "gpt-5.4"

    def test_configured_model_returns_none_for_missing_file(self) -> None:
        adapter = CopilotAdapter(
            FakeRunner(()),
            config_path=Path(__file__).resolve().parents[1] / "fixtures" / "missing_config.json",
        )

        assert adapter.configured_model() is None

    def test_configured_model_returns_none_for_invalid_json(self) -> None:
        adapter = CopilotAdapter(FakeRunner(()), config_path=Path(__file__))

        assert adapter.configured_model() is None

    def test_constructor_rejects_non_positive_timeout(self) -> None:
        with self.assertRaises(ValueError):
            CopilotAdapter(FakeRunner(()), timeout_sec=0)
        with self.assertRaises(ValueError):
            CopilotAdapter(FakeRunner(()), timeout_sec=-1.0)

    def test_launch_parameters_rejects_empty_pane_target(self) -> None:
        with self.assertRaises(ValueError):
            CopilotLaunchParameters(pane_target="   ", prompt="x")

    def test_launch_parameters_rejects_empty_command_prefix(self) -> None:
        with self.assertRaises(ValueError):
            CopilotLaunchParameters(pane_target="%1", command_prefix=())
        # All-blank prefix → also rejected after stripping.
        with self.assertRaises(ValueError):
            CopilotLaunchParameters(pane_target="%1", command_prefix=("  ", ""))

    def test_build_prompt_submission_rejects_empty_pane_target(self) -> None:
        adapter = CopilotAdapter(FakeRunner(()))
        with self.assertRaises(ValueError):
            adapter.build_prompt_submission("   ", "hello")

    def test_build_prompt_submission_rejects_empty_prompt(self) -> None:
        adapter = CopilotAdapter(FakeRunner(()))
        with self.assertRaises(ValueError):
            adapter.build_prompt_submission("%1", "   ")

    def test_build_prompt_submission_skips_enter_when_disabled(self) -> None:
        adapter = CopilotAdapter(FakeRunner(()))
        submission = adapter.build_prompt_submission("%2", "hi", append_enter=False)

        assert submission.tmux_commands == (("tmux", "send-keys", "-t", "%2", "-l", "hi"),)
        assert submission.append_enter is False

    def test_build_tmux_launch_command_omits_enter_when_disabled(self) -> None:
        adapter = CopilotAdapter(FakeRunner(()))
        params = CopilotLaunchParameters(
            pane_target="%9",
            prompt="status",
            append_enter=False,
        )
        launch = adapter.build_tmux_launch_command(params)
        assert launch.tmux_command[-1] != "Enter"
        assert launch.tmux_command == ("tmux", "send-keys", "-t", "%9", launch.shell_command)

    def test_build_cli_command_passes_through_prefix_when_not_copilot(self) -> None:
        # When the prefix's first token is not "copilot", build_cli_command
        # leaves it untouched (covers the false branch of the
        # ``_basename(prefix[0]) == "copilot"`` check).
        adapter = CopilotAdapter(FakeRunner(()), copilot_binary="/usr/local/bin/copilot")
        params = CopilotLaunchParameters(
            pane_target="%1",
            prompt="status",
            command_prefix=("env", "VAR=1", "copilot", "chat"),
        )
        cmd = adapter.build_cli_command(params)
        # First token must remain "env" — copilot_binary substitution
        # only applies when the first token's basename is "copilot".
        assert cmd[0] == "env"

    def test_build_cli_command_substitutes_configured_copilot_binary(self) -> None:
        adapter = CopilotAdapter(FakeRunner(()), copilot_binary="/opt/copilot")
        params = CopilotLaunchParameters(pane_target="%1", prompt="hi")
        cmd = adapter.build_cli_command(params)
        assert cmd[0] == "/opt/copilot"

    def test_build_cli_command_omits_prompt_when_none(self) -> None:
        adapter = CopilotAdapter(FakeRunner(()))
        params = CopilotLaunchParameters(pane_target="%1")
        cmd = adapter.build_cli_command(params)
        # Prompt absent → no trailing prompt token in the cli command.
        assert cmd == ("copilot", "chat")

    def test_detect_process_name_handles_empty_input(self) -> None:
        adapter = CopilotAdapter(FakeRunner(()))
        detection = adapter.detect_process_name("   ")
        assert detection.is_likely_copilot is False
        assert detection.reason == "empty_command"
        assert detection.candidate == ()

    def test_detect_command_handles_empty_input(self) -> None:
        adapter = CopilotAdapter(FakeRunner(()))
        detection = adapter.detect_command("")
        assert detection.is_likely_copilot is False
        assert detection.reason == "empty_command"

    def test_detect_command_falls_back_to_split_on_shlex_error(self) -> None:
        # An unbalanced quote raises ValueError in shlex.split — the
        # tokenizer must then fall back to plain str.split() and still
        # return useful tokens (covers lines 56-57).
        adapter = CopilotAdapter(FakeRunner(()))
        detection = adapter.detect_command("copilot 'unterminated")
        # First token survives the fallback split.
        assert detection.candidate[0] == "copilot"
        assert detection.is_likely_copilot is True
        assert detection.reason == "copilot_binary"

    def test_configured_model_returns_none_when_payload_is_not_a_dict(self) -> None:
        # Use a fixture that's a JSON array, not an object.
        from tempfile import NamedTemporaryFile

        with NamedTemporaryFile(
            "w",
            suffix=".json",
            dir=Path.cwd(),
            delete=False,
            encoding="utf-8",
        ) as fh:
            fh.write('["not", "a", "dict"]')
            cfg = Path(fh.name)
        try:
            adapter = CopilotAdapter(FakeRunner(()), config_path=cfg)
            assert adapter.configured_model() is None
        finally:
            cfg.unlink(missing_ok=True)

    def test_configured_model_returns_none_when_model_is_not_a_string(self) -> None:
        from tempfile import NamedTemporaryFile

        with NamedTemporaryFile(
            "w",
            suffix=".json",
            dir=Path.cwd(),
            delete=False,
            encoding="utf-8",
        ) as fh:
            fh.write('{"model": 42}')
            cfg = Path(fh.name)
        try:
            adapter = CopilotAdapter(FakeRunner(()), config_path=cfg)
            assert adapter.configured_model() is None
        finally:
            cfg.unlink(missing_ok=True)

    def test_run_command_wraps_non_zero_exit(self) -> None:
        # A succeeded=False CommandResult must be re-raised as
        # CopilotCommandError (covers the line at the bottom of
        # _run_command after the runner returns).
        runner = FakeRunner(
            (_result(("tmux", "send-keys", "-t", "%5", "-l", "x"), exit_code=2, stderr="oops"),)
        )
        adapter = CopilotAdapter(runner)
        with self.assertRaises(CopilotCommandError) as ctx:
            adapter.submit_prompt("%5", "x", append_enter=False)
        assert ctx.exception.exit_code == 2

    def test_launch_in_pane_attaches_evidence_when_output_present(self) -> None:
        # The runner returns stdout containing a Copilot session id —
        # launch_in_pane must populate `evidence` (covers the
        # `if combined_output: evidence = ...` branch).
        runner = FakeRunner(
            (
                _result(
                    ("tmux", "send-keys", "-t", "%1", "cd /repo && copilot chat"),
                    stdout="Copilot session id: session-launch-001",
                ),
            )
        )
        adapter = CopilotAdapter(runner)
        params = CopilotLaunchParameters(pane_target="%1", prompt="hi")
        outcome = adapter.launch_in_pane(params)
        assert outcome.evidence is not None
        assert outcome.evidence.copilot_session_id == "session-launch-001"

    def test_launch_in_pane_leaves_evidence_none_when_no_output(self) -> None:
        runner = FakeRunner(
            (
                _result(
                    ("tmux", "send-keys", "-t", "%1", "cd /repo && copilot chat"),
                    stdout="",
                    stderr="   \n",  # whitespace-only is filtered out
                ),
            )
        )
        adapter = CopilotAdapter(runner)
        params = CopilotLaunchParameters(pane_target="%1", prompt="hi")
        outcome = adapter.launch_in_pane(params)
        assert outcome.evidence is None

    def test_likely_process_names_includes_configured_binary_basename(self) -> None:
        adapter = CopilotAdapter(FakeRunner(()), copilot_binary="/opt/foo/copilot-custom")
        names = adapter.likely_process_names()
        assert "copilot-custom" in names
        # Default executables remain too.
        assert "copilot" in names

    def test_render_shell_command_quotes_paths_with_spaces(self) -> None:
        adapter = CopilotAdapter(FakeRunner(()))
        text = adapter.render_shell_command(("copilot", "chat"), cwd="/var/data/with spaces")
        assert "with spaces" in text
        assert text.startswith("cd ")
        assert "&&" in text


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
