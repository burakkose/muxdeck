# ruff: noqa: PTH118,PTH123

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from copilot_commander.adapters.process_adapter import ProcessAdapter
from copilot_commander.exceptions import CommandError


class ProcessAdapterTests(unittest.TestCase):
    def test_run_returns_command_result_for_success(self) -> None:
        adapter = ProcessAdapter()

        result = adapter.run(
            (
                "python3",
                "-c",
                ("import sys; print('stdout-line'); print('stderr-line', file=sys.stderr)"),
            ),
            timeout_sec=5.0,
        )

        assert result.command == ("python3", "-c", result.command[2])
        assert result.exit_code == 0
        assert result.stdout.strip() == "stdout-line"
        assert result.stderr.strip() == "stderr-line"
        assert result.succeeded
        assert result.started_at.tzinfo is not None
        assert result.finished_at.tzinfo is not None

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

        assert result.exit_code == 7
        assert result.stdout.strip() == "still-captured"
        assert result.stderr.strip() == "boom"
        assert not result.succeeded

    def test_run_wraps_missing_executable(self) -> None:
        adapter = ProcessAdapter()

        with pytest.raises(CommandError) as context:
            adapter.run(("definitely-not-a-real-executable",), timeout_sec=1.0)

        assert "definitely-not-a-real-executable" in context.value.command
        assert context.value.exit_code is None
        assert context.value.stderr is not None

    def test_run_wraps_timeouts_with_partial_output(self) -> None:
        adapter = ProcessAdapter()

        with pytest.raises(CommandError) as context:
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
                timeout_sec=0.1,
            )

        assert "timed out after 0.100s" in (context.value.stderr or "")
        assert (context.value.stdout or "").strip() in {"", "before-timeout"}

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
        assert stdout_lines[0] == "present"
        assert stdout_lines[1] == str(temp_path.resolve())
        assert result.cwd == temp_path.resolve()


if __name__ == "__main__":
    unittest.main()
