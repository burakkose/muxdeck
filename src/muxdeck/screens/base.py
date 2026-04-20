from __future__ import annotations

import contextlib
import re
from collections.abc import Iterable
from typing import TYPE_CHECKING

from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widget import Widget

from muxdeck.bindings import KeyHint
from muxdeck.widgets.common import KeyHintFooter, TabBar

if TYPE_CHECKING:
    from muxdeck.app import MuxdeckRuntime


class ShellScreen(Screen[None]):
    SCREEN_TITLE = "SCREEN"
    FOOTER_HINTS: tuple[KeyHint, ...] = ()

    def __init__(self, runtime: MuxdeckRuntime) -> None:
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
        return (
            *self.FOOTER_HINTS,
            KeyHint("ctrl+p", "commands"),
            KeyHint("r", "refresh"),
            KeyHint("q", "quit"),
        )

    def set_status(self, message: str) -> None:
        self._status = message
        if self.is_mounted:
            footer = self.query_one(KeyHintFooter)
            footer.status = message

    def set_hints(self, hints: Iterable[KeyHint]) -> None:
        if self.is_mounted:
            self.query_one(KeyHintFooter).hints = tuple(hints)

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        if not self.is_mounted:
            return
        footer = self.query_one(KeyHintFooter)
        footer.focus_label = self._describe_focus(event.widget)

    def apply_ui_preferences(self) -> bool:
        return False

    @staticmethod
    def _render_widget_text(widget: Widget) -> str:
        renderable = widget.render()
        plain: object = getattr(renderable, "plain", None)
        if isinstance(plain, str):
            return plain
        inner_renderable: object = getattr(renderable, "_renderable", None)
        inner_plain: object = getattr(inner_renderable, "plain", None)
        if isinstance(inner_plain, str):
            return inner_plain
        if inner_renderable is not None:
            return str(inner_renderable)
        return str(renderable)

    def copy_rendered_text(self, label: str, *widgets: Widget) -> None:
        parts = [self._render_widget_text(widget).strip() for widget in widgets]
        text = "\n\n".join(part for part in parts if part)
        if not text:
            self.set_status(f"no {label} available")
            return
        self.app.copy_to_clipboard(text)
        self.set_status(f"copied {label} to clipboard")

    def refresh_data(self) -> None:
        return

    def _describe_focus(self, widget: Widget) -> str:
        widget_id = widget.id or ""
        explicit = {
            "compose-editor": "message editor",
            "compose-mirror": "live mirror",
            "dashboard-agents": "agent list",
            "fleet-stories": "story lanes",
            "help-content": "help content",
            "help-filter-input": "help search",
            "operations-agents": "agent list",
            "replay-marker-list": "markers",
            "replay-transcript-list": "transcript",
            "sessions-list": "session list",
        }
        if widget_id in explicit:
            return explicit[widget_id]
        if widget_id.endswith("-filter-input"):
            prefix = widget_id.removesuffix("-filter-input").split("-")[-1]
            return f"{prefix} filter" if prefix != "help" else "help search"
        border_title = getattr(widget, "border_title", None)
        if isinstance(border_title, str):
            cleaned = border_title.strip()
            if cleaned:
                return cleaned.lower()
        if widget_id:
            cleaned_id = widget_id.replace("_", "-")
            parts = [
                part
                for part in cleaned_id.split("-")
                if part not in {"row", "root", "main", "panel", "list", "input", "wrap"}
            ]
            if parts:
                return " ".join(parts)
        label = re.sub(r"(?<!^)(?=[A-Z])", " ", widget.__class__.__name__).strip().lower()
        return label.replace(" panel", "").replace(" screen", "")

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
