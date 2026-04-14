from __future__ import annotations


class CopilotCommanderError(Exception):
    """Base exception for all copilot-commander failures."""


class ApplicationError(CopilotCommanderError):
    """Base exception for application-level failures."""


class ValidationError(CopilotCommanderError):
    """Raised when input data violates an invariant."""


class ConfigError(ApplicationError):
    """Raised when configuration cannot be loaded or interpreted."""


class ConfigValidationError(ConfigError, ValidationError):
    """Raised when configuration data is syntactically valid but invalid."""


class DomainError(ApplicationError):
    """Raised when domain state cannot be represented safely."""


class DomainValidationError(DomainError, ValidationError):
    """Raised when a domain model or value object receives invalid data."""


class InvalidStatusTransitionError(DomainError):
    """Raised when a lifecycle transition is not allowed."""


class PersistenceError(ApplicationError):
    """Raised when state cannot be loaded or persisted."""


class IntegrationError(ApplicationError):
    """Raised when an external dependency or command fails."""


class ParseError(IntegrationError):
    """Raised when external output cannot be parsed safely."""


class CopilotParseError(ParseError):
    """Raised when Copilot output cannot be parsed into domain objects."""


class CommandError(IntegrationError):
    """Raised when a shell command returns an unexpected result."""

    def __init__(
        self,
        command: str,
        *,
        exit_code: int | None = None,
        stderr: str | None = None,
        stdout: str | None = None,
    ) -> None:
        self.command = command
        self.exit_code = exit_code
        self.stderr = stderr
        self.stdout = stdout
        parts = [f"command failed: {command}"]
        if exit_code is not None:
            parts.append(f"exit_code={exit_code}")
        if stderr:
            parts.append(f"stderr={stderr}")
        super().__init__(", ".join(parts))


class TmuxCommandError(CommandError):
    """Raised when a tmux command fails."""


class GitCommandError(CommandError):
    """Raised when a git command fails."""
