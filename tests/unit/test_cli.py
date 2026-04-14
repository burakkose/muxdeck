"""CLI smoke tests."""

from __future__ import annotations

import pytest

from copilot_commander.__main__ import main


def test_main_smoke(capsys: pytest.CaptureFixture[str]) -> None:
    """Keep the console entrypoint wired while the runtime surface evolves."""
    assert main() == 0
    captured = capsys.readouterr()
    assert captured.out.strip()
    assert captured.err == ""
