# ruff: noqa: PT009, PT027

from __future__ import annotations

import unittest

from muxdeck.exceptions import (
    ApplicationError,
    CommandError,
    ConfigError,
    ConfigValidationError,
    CopilotParseError,
    DomainError,
    DomainValidationError,
    GitCommandError,
    IntegrationError,
    InvalidStatusTransitionError,
    MuxdeckError,
    ParseError,
    PersistenceError,
    TmuxCommandError,
    ValidationError,
)


class CommandErrorMessageTests(unittest.TestCase):
    """Cover the message-formatting branches of CommandError.__init__."""

    def test_command_only_message(self) -> None:
        err = CommandError("git status")
        self.assertEqual(err.command, "git status")
        self.assertIsNone(err.exit_code)
        self.assertIsNone(err.stderr)
        self.assertIsNone(err.stdout)
        self.assertEqual(str(err), "command failed: git status")

    def test_includes_exit_code_when_provided(self) -> None:
        err = CommandError("git push", exit_code=1)
        self.assertIn("exit_code=1", str(err))
        self.assertIn("command failed: git push", str(err))
        self.assertNotIn("stderr=", str(err))

    def test_exit_code_zero_is_still_serialized(self) -> None:
        err = CommandError("noop", exit_code=0)
        self.assertIn("exit_code=0", str(err))

    def test_includes_stderr_when_truthy(self) -> None:
        err = CommandError("rm -rf", exit_code=2, stderr="permission denied")
        rendered = str(err)
        self.assertIn("exit_code=2", rendered)
        self.assertIn("stderr=permission denied", rendered)

    def test_empty_stderr_is_skipped(self) -> None:
        err = CommandError("ok", exit_code=0, stderr="")
        self.assertNotIn("stderr=", str(err))

    def test_stdout_is_attached_but_not_rendered(self) -> None:
        # The current implementation stores stdout but does not include it
        # in the message. Verify the attribute round-trips so callers can
        # surface it themselves.
        err = CommandError("ls", stdout="hi")
        self.assertEqual(err.stdout, "hi")
        self.assertNotIn("stdout=", str(err))

    def test_tmux_and_git_subclasses_inherit_command_error(self) -> None:
        tmux_err = TmuxCommandError("tmux ls", exit_code=1, stderr="no server")
        git_err = GitCommandError("git status", exit_code=128, stderr="not a repo")
        self.assertIsInstance(tmux_err, CommandError)
        self.assertIsInstance(git_err, CommandError)
        self.assertIn("stderr=no server", str(tmux_err))
        self.assertIn("stderr=not a repo", str(git_err))


class ExceptionHierarchyTests(unittest.TestCase):
    """Lock the inheritance tree so future refactors do not silently
    break ``isinstance`` checks scattered across the code base."""

    def test_base_hierarchy(self) -> None:
        self.assertTrue(issubclass(ApplicationError, MuxdeckError))
        self.assertTrue(issubclass(ValidationError, MuxdeckError))
        self.assertTrue(issubclass(ConfigError, ApplicationError))
        self.assertTrue(issubclass(DomainError, ApplicationError))
        self.assertTrue(issubclass(IntegrationError, ApplicationError))
        self.assertTrue(issubclass(PersistenceError, ApplicationError))

    def test_validation_subclasses_are_dual_inherited(self) -> None:
        # Catching ValidationError must catch both config and domain
        # validation errors regardless of the originating subsystem.
        self.assertTrue(issubclass(ConfigValidationError, ConfigError))
        self.assertTrue(issubclass(ConfigValidationError, ValidationError))
        self.assertTrue(issubclass(DomainValidationError, DomainError))
        self.assertTrue(issubclass(DomainValidationError, ValidationError))

    def test_invalid_status_transition_is_a_domain_error(self) -> None:
        self.assertTrue(issubclass(InvalidStatusTransitionError, DomainError))

    def test_parse_error_lineage(self) -> None:
        self.assertTrue(issubclass(ParseError, IntegrationError))
        self.assertTrue(issubclass(CopilotParseError, ParseError))


if __name__ == "__main__":
    unittest.main()
