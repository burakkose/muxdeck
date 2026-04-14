# ruff: noqa: PTH118,PTH123

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from copilot_commander.adapters.process_adapter import ProcessAdapter
from copilot_commander.exceptions import CommandError


class ProcessAdapterTests(unittest.TestCase):
    def test_run_returns_command_result_for_success(self) -> None:
        adapter = ProcessAdapter()

        result = adapter.run(
            (
                "python3",
                "-c",
                (
                    "import sys; "
                    "print('stdout-line'); "
                    "print('stderr-line', file=sys.stderr)"
                ),
            ),
            timeout_sec=5.0,
        )

        self.assertEqual(result.command, ("python3", "-c", result.command[2]))
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout.strip(), "stdout-line")
        self.assertEqual(result.stderr.strip(), "stderr-line")
        self.assertTrue(result.succeeded)
        self.assertIsNotNone(result.started_at.tzinfo)
        self.assertIsNotNone(result.finished_at.tzinfo)

    def test_run_preserves_non_zero_exit_code_and_streams(self) -> None:
        adapter = ProcessAdapter()

        result = adapter.run(
            (
                "python3",
                "-c",
                (
                    "import sys; "
                    "print('still-captured'); "
                    "print('boom', file=sys.stderr); "
                    "raise SystemExit(7)"
                ),
            ),
            timeout_sec=5.0,
        )

        self.assertEqual(result.exit_code, 7)
        self.assertEqual(result.stdout.strip(), "still-captured")
        self.assertEqual(result.stderr.strip(), "boom")
        self.assertFalse(result.succeeded)

    def test_run_wraps_missing_executable(self) -> None:
        adapter = ProcessAdapter()

        with self.assertRaises(CommandError) as context:
            adapter.run(("definitely-not-a-real-executable",), timeout_sec=1.0)

        self.assertIn("definitely-not-a-real-executable", context.exception.command)
        self.assertIsNone(context.exception.exit_code)
        self.assertIsNotNone(context.exception.stderr)

    def test_run_wraps_timeouts_with_partial_output(self) -> None:
        adapter = ProcessAdapter()

        with self.assertRaises(CommandError) as context:
            adapter.run(
                (
                    "python3",
                    "-c",
                    (
                        "import sys, time; "
                        "print('before-timeout'); "
                        "sys.stdout.flush(); "
                        "time.sleep(0.25)"
                    ),
                ),
                timeout_sec=0.05,
            )

        self.assertIn("timed out after 0.050s", context.exception.stderr or "")
        self.assertEqual((context.exception.stdout or "").strip(), "before-timeout")

    def test_run_merges_env_and_passes_cwd(self) -> None:
        adapter = ProcessAdapter()
        with TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            temp_path = Path(temp_dir)

            result = adapter.run(
                (
                    "python3",
                    "-c",
                    (
                        "import os, pathlib; "
                        "print(os.environ['MUXDECK_PROCESS_TEST']); "
                        "print(pathlib.Path.cwd())"
                    ),
                ),
                cwd=temp_path,
                env={"MUXDECK_PROCESS_TEST": "present"},
                timeout_sec=5.0,
            )

        stdout_lines = result.stdout.splitlines()
        self.assertEqual(stdout_lines[0], "present")
        self.assertEqual(stdout_lines[1], str(temp_path.resolve()))
        self.assertEqual(result.cwd, temp_path.resolve())


if __name__ == "__main__":
    unittest.main()
