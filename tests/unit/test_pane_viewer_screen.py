"""Lightweight tests for :mod:`copilot_commander.screens.pane_viewer`.

These only cover the pure-Python helpers: mode-dependent footer hints
and the border title formatter. Full Textual mount behaviour is
exercised by the existing adapter and widget suites.
"""

from __future__ import annotations

from copilot_commander.screens.pane_viewer import (
    PANE_VIEWER_BINDINGS,
    PANE_VIEWER_HINTS,
    _hints_for_mode,
)


class TestHintsForMode:
    def test_view_mode_advertises_close_and_toggle(self) -> None:
        hints = _hints_for_mode(input_on=False)
        keys = [h.key for h in hints]
        assert "esc" in keys
        assert "f2" in keys
        # PgUp/PgDn scroll is surfaced so users know how to review
        # scrollback without entering input mode.
        assert any("pgup" in k.lower() for k in keys)

    def test_input_mode_advertises_exit_and_forwarding(self) -> None:
        hints = _hints_for_mode(input_on=True)
        keys = [h.key for h in hints]
        labels = [h.label for h in hints]
        assert "f2" in keys
        # Something should make clear every key is forwarded.
        assert any("pane" in label.lower() for label in labels)
        # Escape must not be advertised as "close" in input mode — it
        # gets forwarded to the pane instead.
        assert not any(h.key == "esc" and "close" in h.label.lower() for h in hints)

    def test_default_hints_are_view_mode(self) -> None:
        assert _hints_for_mode(input_on=False) == PANE_VIEWER_HINTS


class TestBindings:
    def test_no_class_bindings_consume_printable_keys(self) -> None:
        # All key handling flows through ``on_key`` so no printable
        # character (``x``, ``i``, ...) is stolen by a class-level
        # binding before the input-forwarding path sees it.
        assert PANE_VIEWER_BINDINGS == []
