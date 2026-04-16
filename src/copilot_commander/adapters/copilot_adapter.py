from __future__ import annotations

import shlex
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal

from copilot_commander.domain.value_objects import CommandResult
from copilot_commander.exceptions import CommandError
from copilot_commander.parsers import CopilotOutputParseResult, parse_copilot_output
from copilot_commander.parsers.copilot_output_parser import CopilotTaskEvidence
from copilot_commander.types import CommandRunner, PathLike

_COPILOT_EXECUTABLE_NAMES = frozenset(
    {
        "copilot",
        "copilot-chat",
        "copilot-cli",
        "github-copilot-cli",
        "github-copilot-cli.js",
    }
)
_WRAPPER_EXECUTABLE_NAMES = frozenset({"bun", "node", "npm", "npx", "pnpm", "yarn"})
_GH_EXTENSION_PREFIX = ("gh", "copilot")


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _normalize_path(path: PathLike | None) -> Path | None:
    if path is None:
        return None
    return Path(path).expanduser().resolve(strict=False)


def _basename(token: str) -> str:
    return Path(token).name.casefold()


def _tokenize_command(candidate: str | tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(candidate, tuple):
        return tuple(token for token in candidate if token.strip())
    try:
        parsed = shlex.split(candidate)
    except ValueError:
        parsed = candidate.split()
    return tuple(token for token in parsed if token.strip())


def _contains_adjacent_pair(tokens: tuple[str, ...], expected: tuple[str, str]) -> bool:
    return any(tokens[index : index + 2] == expected for index in range(len(tokens) - 1))


type CopilotDetectionReason = Literal[
    "copilot_binary",
    "empty_command",
    "gh_extension",
    "no_copilot_signature",
    "process_name",
    "wrapped_copilot_binary",
    "wrapped_gh_extension",
]


class CopilotCommandError(CommandError):
    """Raised when a Copilot CLI or tmux launch command fails."""


@dataclass(frozen=True, slots=True)
class CopilotLaunchParameters:
    pane_target: str
    prompt: str | None = None
    cwd: PathLike | None = None
    model: str | None = None
    command_prefix: tuple[str, ...] = ("copilot", "chat")
    extra_args: tuple[str, ...] = ()
    append_enter: bool = True

    def __post_init__(self) -> None:
        normalized_target = self.pane_target.strip()
        if not normalized_target:
            msg = "pane_target must not be empty"
            raise ValueError(msg)
        object.__setattr__(self, "pane_target", normalized_target)
        object.__setattr__(self, "prompt", _normalize_optional_text(self.prompt))
        object.__setattr__(self, "cwd", _normalize_path(self.cwd))
        if not self.command_prefix:
            msg = "command_prefix must not be empty"
            raise ValueError(msg)
        normalized_prefix = tuple(token.strip() for token in self.command_prefix if token.strip())
        if not normalized_prefix:
            msg = "command_prefix must not be empty"
            raise ValueError(msg)
        object.__setattr__(self, "command_prefix", normalized_prefix)
        object.__setattr__(self, "model", _normalize_optional_text(self.model))
        object.__setattr__(
            self,
            "extra_args",
            tuple(token.strip() for token in self.extra_args if token.strip()),
        )


@dataclass(frozen=True, slots=True)
class CopilotLaunchCommand:
    cli_command: tuple[str, ...]
    shell_command: str
    tmux_command: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CopilotPromptSubmission:
    pane_target: str
    prompt: str
    append_enter: bool
    tmux_commands: tuple[tuple[str, ...], ...]


@dataclass(frozen=True, slots=True)
class CopilotPromptSubmissionOutcome:
    submission: CopilotPromptSubmission
    command_results: tuple[CommandResult, ...]


@dataclass(frozen=True, slots=True)
class CopilotCommandDetection:
    candidate: tuple[str, ...]
    is_likely_copilot: bool
    reason: CopilotDetectionReason


@dataclass(frozen=True, slots=True)
class CopilotUsageSummary:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cost: Decimal | None = None
    currency: str | None = None


@dataclass(frozen=True, slots=True)
class CopilotSessionEvidence:
    parse_result: CopilotOutputParseResult
    copilot_session_id: str | None
    session_ids: tuple[str, ...]
    usage_snapshots: tuple[CopilotUsageSummary, ...]
    latest_usage: CopilotUsageSummary | None
    blocking_issue_kinds: tuple[str, ...]
    error_messages: tuple[str, ...]
    background_task_count: int = 0
    task_evidence: tuple[CopilotTaskEvidence, ...] = ()


@dataclass(frozen=True, slots=True)
class CopilotLaunchOutcome:
    parameters: CopilotLaunchParameters
    launch_command: CopilotLaunchCommand
    command_result: CommandResult
    evidence: CopilotSessionEvidence | None = None


class CopilotAdapter:
    def __init__(
        self,
        command_runner: CommandRunner,
        *,
        copilot_binary: str = "copilot",
        tmux_binary: str = "tmux",
        timeout_sec: float = 10.0,
    ) -> None:
        if timeout_sec <= 0:
            msg = "timeout_sec must be greater than zero"
            raise ValueError(msg)
        self._command_runner = command_runner
        self._copilot_binary = copilot_binary
        self._tmux_binary = tmux_binary
        self._timeout_sec = timeout_sec

    def likely_process_names(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    *(_basename(name) for name in _COPILOT_EXECUTABLE_NAMES),
                    _basename(self._copilot_binary),
                }
            )
        )

    def build_cli_command(self, parameters: CopilotLaunchParameters, /) -> tuple[str, ...]:
        prefix = parameters.command_prefix
        if _basename(prefix[0]) == "copilot":
            prefix = (self._copilot_binary, *prefix[1:])
        command = list(prefix)
        if parameters.model is not None:
            command.extend(("--model", parameters.model))
        command.extend(parameters.extra_args)
        if parameters.prompt is not None:
            command.append(parameters.prompt)
        return tuple(command)

    def build_tmux_launch_command(
        self,
        parameters: CopilotLaunchParameters,
        /,
    ) -> CopilotLaunchCommand:
        cli_command = self.build_cli_command(parameters)
        shell_command = self.render_shell_command(cli_command, cwd=parameters.cwd)
        tmux_command = [self._tmux_binary, "send-keys", "-t", parameters.pane_target, shell_command]
        if parameters.append_enter:
            tmux_command.append("Enter")
        return CopilotLaunchCommand(
            cli_command=cli_command,
            shell_command=shell_command,
            tmux_command=tuple(tmux_command),
        )

    def build_prompt_submission(
        self,
        pane_target: str,
        prompt: str,
        /,
        *,
        append_enter: bool = True,
    ) -> CopilotPromptSubmission:
        normalized_target = pane_target.strip()
        normalized_prompt = _normalize_optional_text(prompt)
        if not normalized_target:
            msg = "pane_target must not be empty"
            raise ValueError(msg)
        if normalized_prompt is None:
            msg = "prompt must not be empty"
            raise ValueError(msg)
        tmux_commands: list[tuple[str, ...]] = [
            (
                self._tmux_binary,
                "send-keys",
                "-t",
                normalized_target,
                "-l",
                normalized_prompt,
            )
        ]
        if append_enter:
            tmux_commands.append(
                (
                    self._tmux_binary,
                    "send-keys",
                    "-t",
                    normalized_target,
                    "Enter",
                )
            )
        return CopilotPromptSubmission(
            pane_target=normalized_target,
            prompt=normalized_prompt,
            append_enter=append_enter,
            tmux_commands=tuple(tmux_commands),
        )

    def launch_in_pane(self, parameters: CopilotLaunchParameters, /) -> CopilotLaunchOutcome:
        launch_command = self.build_tmux_launch_command(parameters)
        result = self._run_command(launch_command.tmux_command)
        evidence: CopilotSessionEvidence | None = None
        combined_output = "\n".join(part for part in (result.stdout, result.stderr) if part.strip())
        if combined_output:
            evidence = self.interpret_output(combined_output)
        return CopilotLaunchOutcome(
            parameters=parameters,
            launch_command=launch_command,
            command_result=result,
            evidence=evidence,
        )

    def submit_prompt(
        self,
        pane_target: str,
        prompt: str,
        /,
        *,
        append_enter: bool = True,
    ) -> CopilotPromptSubmissionOutcome:
        submission = self.build_prompt_submission(
            pane_target,
            prompt,
            append_enter=append_enter,
        )
        results = tuple(self._run_command(command) for command in submission.tmux_commands)
        return CopilotPromptSubmissionOutcome(submission=submission, command_results=results)

    def detect_process_name(self, candidate: str, /) -> CopilotCommandDetection:
        normalized_candidate = candidate.strip()
        if not normalized_candidate:
            return CopilotCommandDetection(
                candidate=(),
                is_likely_copilot=False,
                reason="empty_command",
            )
        if _basename(normalized_candidate) in self.likely_process_names():
            return CopilotCommandDetection(
                candidate=(normalized_candidate,),
                is_likely_copilot=True,
                reason="process_name",
            )
        return CopilotCommandDetection(
            candidate=(normalized_candidate,),
            is_likely_copilot=False,
            reason="no_copilot_signature",
        )

    def detect_command(self, candidate: str | tuple[str, ...], /) -> CopilotCommandDetection:
        tokens = _tokenize_command(candidate)
        if not tokens:
            return CopilotCommandDetection(
                candidate=(),
                is_likely_copilot=False,
                reason="empty_command",
            )
        token_basenames = tuple(_basename(token) for token in tokens)
        if token_basenames[0] in _COPILOT_EXECUTABLE_NAMES:
            return CopilotCommandDetection(
                candidate=tokens,
                is_likely_copilot=True,
                reason="copilot_binary",
            )
        if token_basenames[:2] == _GH_EXTENSION_PREFIX:
            return CopilotCommandDetection(
                candidate=tokens,
                is_likely_copilot=True,
                reason="gh_extension",
            )
        if token_basenames[0] in _WRAPPER_EXECUTABLE_NAMES:
            if _contains_adjacent_pair(token_basenames[1:], _GH_EXTENSION_PREFIX):
                return CopilotCommandDetection(
                    candidate=tokens,
                    is_likely_copilot=True,
                    reason="wrapped_gh_extension",
                )
            if any(name in _COPILOT_EXECUTABLE_NAMES for name in token_basenames[1:]):
                return CopilotCommandDetection(
                    candidate=tokens,
                    is_likely_copilot=True,
                    reason="wrapped_copilot_binary",
                )
        return CopilotCommandDetection(
            candidate=tokens,
            is_likely_copilot=False,
            reason="no_copilot_signature",
        )

    def detect_commands(
        self,
        candidates: tuple[str | tuple[str, ...], ...],
        /,
    ) -> tuple[CopilotCommandDetection, ...]:
        return tuple(self.detect_command(candidate) for candidate in candidates)

    def interpret_output(self, output: str, /) -> CopilotSessionEvidence:
        parse_result = parse_copilot_output(output)
        session_ids = tuple(candidate.value for candidate in parse_result.session_ids)
        usage_snapshots = tuple(
            CopilotUsageSummary(
                input_tokens=snapshot.input_tokens,
                output_tokens=snapshot.output_tokens,
                total_tokens=snapshot.total_tokens,
                cost=snapshot.cost,
                currency=snapshot.currency,
            )
            for snapshot in parse_result.usage_snapshots
        )
        return CopilotSessionEvidence(
            parse_result=parse_result,
            copilot_session_id=session_ids[0] if session_ids else None,
            session_ids=session_ids,
            usage_snapshots=usage_snapshots,
            latest_usage=usage_snapshots[-1] if usage_snapshots else None,
            blocking_issue_kinds=tuple(issue.kind for issue in parse_result.blocking_issues),
            error_messages=tuple(error.message for error in parse_result.errors),
            background_task_count=parse_result.background_task_count,
            task_evidence=parse_result.task_evidence,
        )

    def interpret_command_result(self, result: CommandResult, /) -> CopilotSessionEvidence:
        combined_output = "\n".join(part for part in (result.stdout, result.stderr) if part.strip())
        return self.interpret_output(combined_output)

    def render_shell_command(
        self,
        cli_command: tuple[str, ...],
        /,
        *,
        cwd: PathLike | None = None,
    ) -> str:
        command_text = shlex.join(cli_command)
        normalized_cwd = _normalize_path(cwd)
        if normalized_cwd is None:
            return command_text
        return f"cd {shlex.quote(str(normalized_cwd))} && {command_text}"

    def _run_command(self, command: tuple[str, ...]) -> CommandResult:
        try:
            result = self._command_runner.run(command, timeout_sec=self._timeout_sec)
        except CommandError as exc:
            raise CopilotCommandError(
                exc.command,
                exit_code=exc.exit_code,
                stderr=exc.stderr,
                stdout=exc.stdout,
            ) from exc
        if result.succeeded:
            return result
        raise CopilotCommandError(
            shlex.join(result.command),
            exit_code=result.exit_code,
            stderr=result.stderr,
            stdout=result.stdout,
        )
