# ruff: noqa: PTH118,PTH123

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from muxdeck.adapters.process_adapter import ProcessAdapter
from muxdeck.exceptions import CommandError


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

    def test_run_rejects_empty_command(self) -> None:
        adapter = ProcessAdapter()
        with pytest.raises(ValueError, match="command must not be empty"):
            adapter.run(())

    def test_run_rejects_zero_or_negative_timeout(self) -> None:
        adapter = ProcessAdapter()
        with pytest.raises(ValueError, match="timeout_sec must be greater than zero"):
            adapter.run(("python3", "-c", "pass"), timeout_sec=0)
        with pytest.raises(ValueError, match="timeout_sec must be greater than zero"):
            adapter.run(("python3", "-c", "pass"), timeout_sec=-1.0)

    def test_constructor_rejects_zero_or_negative_default_timeout(self) -> None:
        with pytest.raises(ValueError, match="timeout_sec must be greater than zero"):
            ProcessAdapter(default_timeout_sec=0)
        with pytest.raises(ValueError, match="timeout_sec must be greater than zero"):
            ProcessAdapter(default_timeout_sec=-3.0)

    def test_get_child_cmdlines_returns_empty_for_unknown_pid(self) -> None:
        adapter = ProcessAdapter()
        # PID 0 is the scheduler; /proc/0/task/0/children doesn't exist
        # on Linux, so the adapter must swallow the OSError and return ().
        assert adapter.get_child_cmdlines(0) == ()

    def test_get_child_cmdlines_returns_self_descendants(self) -> None:
        # The current Python process has PID > 0 and a /proc entry on
        # Linux. The pytest worker doesn't usually fork children, so the
        # result is typically empty — but the call must not raise. This
        # exercises the happy-path read of /proc/<pid>/task/<pid>/children.
        import os as _os

        adapter = ProcessAdapter()
        result = adapter.get_child_cmdlines(_os.getpid())
        assert isinstance(result, tuple)
        for entry in result:
            assert isinstance(entry, str)

    def test_get_child_cmdlines_handles_non_int_tokens_and_unreadable_cmdline(self) -> None:
        # Build a fake /proc tree under a temp dir. process_adapter
        # constructs Path(f"/proc/...") directly, so we monkey-patch
        # the module-level Path symbol with a wrapper that rewrites
        # absolute /proc paths into our temp tree.
        import muxdeck.adapters.process_adapter as mod

        with TemporaryDirectory(dir=Path.cwd()) as tmp_dir:
            base = Path(tmp_dir)
            # Set up children file for pid=100 with a non-int token,
            # an empty-cmdline child, an unreadable-cmdline child, and
            # a healthy child.
            (base / "proc" / "100" / "task" / "100").mkdir(parents=True)
            (base / "proc" / "100" / "task" / "100" / "children").write_text(
                "not-a-pid 200 300 400\n"
            )

            # pid=200 → empty cmdline (should be skipped)
            (base / "proc" / "200").mkdir(parents=True)
            (base / "proc" / "200" / "cmdline").write_text("")
            # pid=200 has no children file → OSError when recursing.

            # pid=300 → cmdline cannot be read (no file)
            (base / "proc" / "300").mkdir(parents=True)

            # pid=400 → healthy command line, no further children
            (base / "proc" / "400").mkdir(parents=True)
            (base / "proc" / "400" / "cmdline").write_text("real-cmd\0--flag\0value")

            real_path_cls = mod.Path  # type: ignore[attr-defined]

            def _path_factory(arg: object, /) -> Path:
                text = str(arg)
                if text.startswith("/proc/"):
                    return base / text.lstrip("/")
                return real_path_cls(arg)  # type: ignore[arg-type, no-any-return]

            try:
                mod.Path = _path_factory  # type: ignore[attr-defined, assignment, misc]
                adapter = ProcessAdapter()
                result = adapter.get_child_cmdlines(100)
            finally:
                mod.Path = real_path_cls  # type: ignore[attr-defined, misc]

            # Only the healthy child contributes a cmdline; the empty
            # and unreadable children are silently skipped.
            assert "real-cmd --flag value" in result
            # Empty pid=200 cmdline must NOT appear as a blank entry.
            assert "" not in result


if __name__ == "__main__":
    unittest.main()
