"""Tests for the tmux-safe driver that disables Kitty keyboard protocol."""

from __future__ import annotations

import os
from unittest.mock import patch

from copilot_commander.app import _get_tmux_safe_driver


class TestTmuxSafeDriver:
    """Verify the driver factory returns the right class based on environment."""

    def test_returns_none_outside_tmux(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            assert _get_tmux_safe_driver() is None

    def test_returns_driver_inside_tmux(self) -> None:
        with patch.dict(os.environ, {"TMUX": "/tmp/tmux-1000/default,123,0"}):
            cls = _get_tmux_safe_driver()
            assert cls is not None
            from textual.drivers.linux_driver import LinuxDriver

            assert issubclass(cls, LinuxDriver)

    def test_driver_has_start_override(self) -> None:
        with patch.dict(os.environ, {"TMUX": "/tmp/tmux-1000/default,123,0"}):
            cls = _get_tmux_safe_driver()
            assert cls is not None
            # The subclass must override start_application_mode
            assert cls.start_application_mode is not cls.__bases__[0].start_application_mode
