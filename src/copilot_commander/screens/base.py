from __future__ import annotations

import contextlib
from collections.abc import Iterable
from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen

from copilot_commander.bindings import KeyHint
from copilot_commander.widgets.common import KeyHintFooter, TabBar

if TYPE_CHECKING:
    from copilot_commander.app import CommanderRuntime


class ShellScreen(Screen[None]):
    SCREEN_TITLE = "SCREEN"
    FOOTER_HINTS: tuple[KeyHint, ...] = ()

    def __init__(self, runtime: CommanderRuntime) -> None:
        super().__init__()
        self.runtime = runtime
        self._status = "ready"

    def compose(self) -> ComposeResult:
        yield TabBar(
            active=self.SCREEN_TITLE.lower(),
            badges=getattr(self.app, "tab_badges", None),
            widget_id="shell-tab-bar",
        )
        with Vertical(id="shell-frame"):
            yield from self.compose_body()
        yield KeyHintFooter(
            hints=self.footer_hints(),
            status=self._status,
            widget_id="shell-footer",
        )

    def compose_body(self) -> ComposeResult:
        from textual.widgets import Static

        yield Static()

    def footer_hints(self) -> tuple[KeyHint, ...]:
        # Only screen-specific hints; global nav is in the tab bar.
        return (*self.FOOTER_HINTS, KeyHint("r", "refresh"), KeyHint("q", "quit"))

    def set_status(self, message: str) -> None:
        self._status = message
        if self.is_mounted:
            footer = self.query_one(KeyHintFooter)
            footer.status = message

    def set_hints(self, hints: Iterable[KeyHint]) -> None:
        if self.is_mounted:
            self.query_one(KeyHintFooter).hints = tuple(hints)

    def refresh_data(self) -> None:
        return

    # ── loading indicator helpers ─────────────────────────────────────
    # Textual's ``Widget.loading = True`` overlays a LoadingIndicator on
    # the widget — the canonical way to signal "data is in flight" so
    # screens never flash an empty list while a worker is running.

    def begin_loading(self, *widgets: object) -> None:
        """Mark one or more widgets as loading. Safe to call pre-mount."""
        for widget in widgets:
            # Widget may not be mounted yet or may not support the
            # ``loading`` attribute on older Textual versions — the
            # user just loses the spinner, not correctness.
            with contextlib.suppress(Exception):
                widget.loading = True  # type: ignore[attr-defined]

    def end_loading(self, *widgets: object) -> None:
        for widget in widgets:
            with contextlib.suppress(Exception):
                widget.loading = False  # type: ignore[attr-defined]
