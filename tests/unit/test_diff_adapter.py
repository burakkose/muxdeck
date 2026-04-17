# ruff: noqa: PT009

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest import mock

from copilot_commander.adapters.diff_adapter import DiffAdapter, NullDiffAdapter


class DiffAdapterTests(unittest.TestCase):
    def test_diff_for_path_invokes_git_with_expected_command(self) -> None:
        adapter = DiffAdapter()
        completed = subprocess.CompletedProcess(
            args=["git"], returncode=0, stdout="diff --git a/x b/x\n+hi\n", stderr=""
        )
        with mock.patch(
            "copilot_commander.adapters.diff_adapter.subprocess.run",
            return_value=completed,
        ) as run:
            text = adapter.diff_for_path(
                Path("/repo"),
                "src/foo.py",
                before="abc",
                after="def",
            )

        self.assertIn("+hi", text)
        run.assert_called_once()
        called_command, called_kwargs = run.call_args.args[0], run.call_args.kwargs
        self.assertEqual(
            called_command,
            ["git", "--no-pager", "diff", "--no-color", "abc..def", "--", "src/foo.py"],
        )
        self.assertEqual(called_kwargs["cwd"], Path("/repo"))
        self.assertTrue(called_kwargs["text"])
        self.assertTrue(called_kwargs["capture_output"])
        self.assertFalse(called_kwargs["check"])

    def test_diff_for_path_falls_back_to_working_tree_when_no_revisions(self) -> None:
        adapter = DiffAdapter()
        completed = subprocess.CompletedProcess(args=["git"], returncode=0, stdout="", stderr="")
        with mock.patch(
            "copilot_commander.adapters.diff_adapter.subprocess.run",
            return_value=completed,
        ) as run:
            adapter.diff_for_path(Path("/repo"), "src/foo.py", before=None, after=None)

        called_command = run.call_args.args[0]
        self.assertEqual(
            called_command,
            ["git", "--no-pager", "diff", "--no-color", "--", "src/foo.py"],
        )

    def test_diff_for_path_returns_empty_string_on_git_failure(self) -> None:
        adapter = DiffAdapter()
        completed = subprocess.CompletedProcess(
            args=["git"], returncode=128, stdout="", stderr="fatal: bad revision"
        )
        with mock.patch(
            "copilot_commander.adapters.diff_adapter.subprocess.run",
            return_value=completed,
        ):
            text = adapter.diff_for_path(Path("/repo"), "src/foo.py", before="x", after="y")

        self.assertEqual(text, "")

    def test_diff_for_path_returns_empty_string_on_oserror(self) -> None:
        adapter = DiffAdapter()
        with mock.patch(
            "copilot_commander.adapters.diff_adapter.subprocess.run",
            side_effect=FileNotFoundError("git not found"),
        ):
            text = adapter.diff_for_path(Path("/repo"), "src/foo.py", before=None, after=None)

        self.assertEqual(text, "")

    def test_diff_for_path_returns_empty_string_on_timeout(self) -> None:
        adapter = DiffAdapter()
        with mock.patch(
            "copilot_commander.adapters.diff_adapter.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git", timeout=5.0),
        ):
            text = adapter.diff_for_path(Path("/repo"), "src/foo.py", before=None, after=None)

        self.assertEqual(text, "")

    def test_null_diff_adapter_returns_empty(self) -> None:
        adapter = NullDiffAdapter()

        self.assertEqual(
            adapter.diff_for_path(Path("/repo"), "src/foo.py", before=None, after=None),
            "",
        )


if __name__ == "__main__":
    unittest.main()
