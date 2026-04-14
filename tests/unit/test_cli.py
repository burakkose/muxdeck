"""CLI smoke tests."""

from __future__ import annotations

import pytest

from copilot_commander.__main__ import main


def test_main_runs_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[str] = []

    def fake_run_app() -> int:
        observed.append("ran")
        return 0

    monkeypatch.setattr("copilot_commander.__main__.run_app", fake_run_app)

    assert main() == 0
    assert observed == ["ran"]
