"""Full-screen live tmux pane viewer.

Opened from the Dashboard via ``v``. The screen:

* Seeds the viewer with the current pane scrollback (coloured, with
  wrapped lines joined) so the user sees context immediately.
* Wires ``tmux pipe-pane`` so every byte written to the real pane is
  appended to a session-scoped ring file and streamed into the viewer.
* On unmount / Escape, stops piping so tmux doesn't keep a dangling
  shell open against a vanished screen.
* Press ``f2`` to toggle **input mode**: all keystrokes (including
  ``esc`` and arrows) are forwarded to the real pane via
  ``send-keys``. Press ``f2`` again to return to view mode. Input is
  off by default so a broken key path can't break viewing — read-only
  observation always works.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from textual import events
from textual.app import ComposeResult

from copilot_commander.adapters.pane_stream import (
    PaneRingReader,
    PaneStreamAdapter,
    ring_file_for_pane,
    translate_textual_key,
)
from copilot_commander.bindings import BindingSpec, KeyHint
from copilot_commander.exceptions import TmuxCommandError
from copilot_commander.screens.base import ShellScreen
from copilot_commander.widgets.live_pane_viewer import LivePaneViewer

if TYPE_CHECKING:
    from copilot_commander.app import CommanderRuntime


# Poll the ring file often enough that the viewer feels live (100ms)
# without pummelling the filesystem. The reader short-circuits on an
# unchanged ``st_size`` so idle panes are nearly free.
_POLL_INTERVAL_SEC = 0.1

# f2 is intentionally chosen because it won't collide with printable
# characters the user may want to forward to the pane while input is
# enabled — letter-based toggles (``x``, ``i``) prevent typing those
# characters through to the agent.
_INPUT_TOGGLE_KEY = "f2"


PANE_VIEWER_BINDINGS: list[BindingSpec] = []


def _hints_for_mode(*, input_on: bool) -> tuple[KeyHint, ...]:
    if input_on:
        return (
            KeyHint("f2", "exit input"),
            KeyHint("all keys", "→ pane"),
        )
    return (
        KeyHint("esc", "close"),
        KeyHint("f2", "send input"),
        KeyHint("pgup/pgdn", "scroll"),
    )


PANE_VIEWER_HINTS: tuple[KeyHint, ...] = _hints_for_mode(input_on=False)


class PaneViewerScreen(ShellScreen):
    """Live, streaming view of one tmux pane."""

    SCREEN_TITLE = "PANE"
    BINDINGS = PANE_VIEWER_BINDINGS
    FOOTER_HINTS = PANE_VIEWER_HINTS

    def __init__(
        self,
        runtime: CommanderRuntime,
        *,
        pane_id: str,
        display_name: str,
        ring_dir: Path | None = None,
    ) -> None:
        super().__init__(runtime)
        self._pane_id = pane_id
        self._display_name = display_name
        resolved_root = ring_dir if ring_dir is not None else Path(tempfile.gettempdir())
        self._ring_path = ring_file_for_pane(resolved_root, pane_id)
        self._adapter: PaneStreamAdapter | None = runtime.pane_stream
        self._reader = PaneRingReader(self._ring_path)
        self._pipe_started = False
        self._write_through = False
        self._loading_cleared = False
        self._first_chunk_seen = False

    # ── composition & mount ──────────────────────────────────────────

    def compose_body(self) -> ComposeResult:
        # Yield the viewer as a direct child of ``#shell-frame`` (which
        # is ``height: 1fr``) so it fills the full screen. A wrapper
        # container without explicit sizing used to collapse the
        # viewer into a narrow strip.
        viewer = LivePaneViewer(widget_id="pane-viewer")
        viewer.border_title = self._border_title()
        yield viewer

    def on_mount(self) -> None:
        viewer = self.query_one(LivePaneViewer)
        self.begin_loading(viewer)
        if self._adapter is None:
            self.end_loading(viewer)
            self.set_status("✗ pane streaming unavailable")
            return
        # Seed with the current scrollback so the viewer isn't empty
        # until the first streamed byte arrives.
        try:
            seed = self._adapter.seed(self._pane_id)
        except TmuxCommandError as exc:
            self.end_loading(viewer)
            self.set_status(f"✗ capture failed: {exc.stderr or 'tmux error'}")
            return
        except OSError as exc:
            self.end_loading(viewer)
            self.set_status(f"✗ capture failed: {exc}")
            return
        if seed:
            viewer.append(seed)
            self._first_chunk_seen = True
            self.end_loading(viewer)
            self._loading_cleared = True
        # Start streaming; if this fails we keep the seeded view
        # rather than tear the whole screen down.
        try:
            self._adapter.start_pipe(self._pane_id, self._ring_path)
            self._pipe_started = True
        except TmuxCommandError as exc:
            self.set_status(f"⚠ pipe-pane failed: {exc.stderr or 'tmux error'} — read-only")
        except OSError as exc:
            self.set_status(f"⚠ pipe-pane failed: {exc} — read-only")
        self.set_interval(_POLL_INTERVAL_SEC, self._drain_ring)
        # Focus the viewer so PgUp/PgDn scroll and every key routes
        # through its event chain to this screen's ``on_key``.
        viewer.focus()
        self._update_mode_chrome()

    def on_unmount(self) -> None:
        self._teardown_pipe()

    # ── polling ──────────────────────────────────────────────────────

    def _drain_ring(self) -> None:
        if self._adapter is None:
            return
        try:
            chunk = self._reader.read_new()
        except OSError as exc:
            # Ring reader already swallows OSError internally, but be
            # defensive — a corrupted file shouldn't kill the timer.
            self.set_status(f"⚠ ring read failed: {exc}")
            return
        if not chunk:
            return
        viewer = self.query_one(LivePaneViewer)
        viewer.append(chunk)
        if not self._first_chunk_seen:
            self._first_chunk_seen = True
        if not self._loading_cleared:
            self.end_loading(viewer)
            self._loading_cleared = True

    # ── key handling ─────────────────────────────────────────────────
    #
    # All key handling flows through ``on_key`` rather than
    # ``BINDINGS``. Bindings fire *before* ``on_key`` gets a chance to
    # ``stop()`` the event, which would make the toggle key and
    # ``escape`` forever un-forwardable to the pane. Handling keys
    # ourselves gives us a single choke-point where input mode can
    # decide what to intercept.

    async def on_key(self, event: events.Key) -> None:
        # F2 always toggles, in both modes, before anything else.
        if event.key == _INPUT_TOGGLE_KEY:
            event.stop()
            event.prevent_default()
            self._toggle_write_through()
            return

        if not self._write_through or self._adapter is None:
            # View mode: escape closes, everything else is left for
            # Textual's default handling (RichLog scroll keys, tab
            # navigation, etc.).
            if event.key == "escape":
                event.stop()
                event.prevent_default()
                self.action_close_viewer()
            return

        # Input mode: forward every key we know how to translate. Keys
        # we can't translate (unmapped function keys, complex shift
        # combos) fall through to Textual so the user isn't trapped.
        translation = translate_textual_key(event.key)
        if translation is None:
            return
        event.stop()
        event.prevent_default()
        try:
            self._adapter.send_keys(self._pane_id, translation)
        except TmuxCommandError as exc:
            self.set_status(f"✗ send-keys failed: {exc.stderr or 'tmux error'}")
        except OSError as exc:
            self.set_status(f"✗ send-keys failed: {exc}")

    # ── actions ──────────────────────────────────────────────────────

    def action_close_viewer(self) -> None:
        self._teardown_pipe()
        self.app.pop_screen()

    def _toggle_write_through(self) -> None:
        if self._adapter is None:
            self.set_status("✗ pane streaming unavailable")
            return
        self._write_through = not self._write_through
        self._update_mode_chrome()

    # ── helpers ──────────────────────────────────────────────────────

    def _teardown_pipe(self) -> None:
        if self._adapter is None or not self._pipe_started:
            return
        self._pipe_started = False
        try:
            self._adapter.stop_pipe(self._pane_id)
        except TmuxCommandError:
            # Pane may already be dead — nothing to unpipe.
            return
        except OSError:
            return

    def _border_title(self) -> str:
        mode = "● INPUT" if self._write_through else "VIEW"
        return f"[{mode}] Pane {self._pane_id} — {self._display_name}"

    def _update_mode_chrome(self) -> None:
        """Reflect the current mode in title, border, footer, status."""
        if self.is_mounted:
            viewer = self.query_one(LivePaneViewer)
            viewer.border_title = self._border_title()
            viewer.set_class(self._write_through, "-input-on")
            self.set_hints(_hints_for_mode(input_on=self._write_through))
        pipe = "streaming" if self._pipe_started else "static"
        if self._write_through:
            self.set_status(
                f"● INPUT ON · pane {self._pane_id} · {pipe} · "
                "every key goes to the pane — press f2 to exit",
            )
        else:
            self.set_status(
                f"VIEW · pane {self._pane_id} · {pipe} · press f2 to send input · esc to close",
            )


__all__ = ["PANE_VIEWER_BINDINGS", "PANE_VIEWER_HINTS", "PaneViewerScreen"]
