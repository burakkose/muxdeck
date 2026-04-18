"""Full-screen compose-with-live-mirror screen.

Opened from the Dashboard via ``v``. The layout:

* **Top (most of the screen)** — a live mirror of the agent's tmux
  pane. We seed it with the pane's current rendered contents, follow
  new bytes via ``tmux pipe-pane``, and periodically resync with
  ``capture-pane`` so carriage-return heavy / dynamically redrawn output
  stays faithful to what tmux actually shows.
* **Bottom** — a multi-line :class:`TextArea` composer backed by
  Textual's full editor bindings (cursor motion, selection, copy, cut,
  paste, delete, undo).

Key bindings:

* ``tab`` / ``shift+tab`` — move focus between the editor and mirror.
* ``ctrl+s`` / ``ctrl+enter`` / ``ctrl+j`` — send the composed text.
* ``i`` (while the mirror is focused) — enter live-input mode; keys go
  directly to the tmux pane until ``escape``.
* ``alt+up`` / ``alt+down`` — shrink / grow the compose editor.
* ``escape`` — leave live-input mode, otherwise close the screen.

Closing the screen tears the ``pipe-pane`` down and removes the per-pane
ring file.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import TYPE_CHECKING

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Label, TextArea

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


# Ring-file polling stays fast for fresh line-oriented output; the
# snapshot poll is slower and corrects dynamic redraws so the mirror
# matches the current tmux screen.
_POLL_INTERVAL_SEC = 0.1
_SNAPSHOT_SYNC_INTERVAL_SEC = 0.25
_DEFAULT_EDITOR_HEIGHT = 10
_MIN_EDITOR_HEIGHT = 7
_MAX_EDITOR_HEIGHT = 24
_EDITOR_HEIGHT_STEP = 2
_RING_DIR_NAME = "pane-mirror"


COMPOSE_MIRROR_BINDINGS: list[BindingSpec] = [
    Binding("ctrl+s", "send", "Send", show=False),
    Binding("ctrl+enter", "send", "Send", show=False),
    Binding("ctrl+j", "send", "Send", show=False),
    Binding("alt+up", "shrink_editor", "More mirror", show=False),
    Binding("alt+down", "grow_editor", "More editor", show=False),
]

COMPOSE_MIRROR_HINTS: tuple[KeyHint, ...] = (
    KeyHint("ctrl+s", "send"),
    KeyHint("tab", "focus"),
    KeyHint("i", "interact"),
    KeyHint("alt+up/down", "resize"),
    KeyHint("r", "resync"),
    KeyHint("esc", "back"),
)


class ComposeWithMirrorScreen(ShellScreen):
    """Pane mirror on top, compose editor on bottom, single screen."""

    SCREEN_TITLE = "COMPOSE"
    BINDINGS = COMPOSE_MIRROR_BINDINGS
    FOOTER_HINTS = COMPOSE_MIRROR_HINTS

    DEFAULT_CSS = """
    ComposeWithMirrorScreen #compose-container {
        height: 1fr;
        width: 1fr;
    }

    ComposeWithMirrorScreen #compose-mirror-wrap {
        height: 1fr;
        width: 1fr;
    }

    ComposeWithMirrorScreen LivePaneViewer {
        height: 1fr;
        width: 1fr;
    }

    ComposeWithMirrorScreen #compose-editor-wrap {
        height: 10;
        width: 1fr;
        margin-top: 1;
    }

    ComposeWithMirrorScreen #compose-editor-label {
        height: 1;
        width: 1fr;
        color: #a89984;
    }

    ComposeWithMirrorScreen #compose-editor {
        height: 1fr;
        width: 1fr;
        background: #1d2021;
        border: solid #504945;
    }

    ComposeWithMirrorScreen #compose-editor:focus {
        border: solid #b8bb26;
    }
    """

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
        resolved_root = (
            ring_dir if ring_dir is not None else runtime.config.paths.state_dir / _RING_DIR_NAME
        )
        self._ring_path = ring_file_for_pane(resolved_root, pane_id)
        self._adapter: PaneStreamAdapter | None = runtime.pane_stream
        self._reader = PaneRingReader(self._ring_path)
        self._pipe_started = False
        self._loading_cleared = False
        self._mirror_input_active = False
        self._editor_height = _DEFAULT_EDITOR_HEIGHT
        self._last_snapshot = ""
        self._capture_error: str | None = None
        self._stream_warning: str | None = None
        self._sync_warning: str | None = None

    @property
    def editor_height(self) -> int:
        return self._editor_height

    @property
    def mirror_input_active(self) -> bool:
        return self._mirror_input_active

    # ── composition & mount ──────────────────────────────────────────

    def compose_body(self) -> ComposeResult:
        with Vertical(id="compose-container"):
            with Vertical(id="compose-mirror-wrap"):
                viewer = LivePaneViewer(widget_id="compose-mirror")
                viewer.border_title = self._format_mirror_title()
                yield viewer
            with Vertical(id="compose-editor-wrap"):
                yield Label("loading pane mirror…", id="compose-editor-label")
                editor = TextArea(
                    id="compose-editor",
                    show_line_numbers=False,
                    soft_wrap=True,
                )
                editor.border_title = f"message → {self._display_name}"
                yield editor

    def on_mount(self) -> None:
        viewer = self.query_one(LivePaneViewer)
        self.begin_loading(viewer)
        viewer.border_subtitle = "loading tmux snapshot…"
        self.set_status("loading pane snapshot… live mirror will follow tmux output")
        self._apply_editor_height()
        if self._adapter is None:
            self._capture_error = "✗ pane streaming unavailable"
            self.end_loading(viewer)
            self._loading_cleared = True
        else:
            self._seed_and_stream(viewer)
        # Land focus in the editor so typing just works. The mirror can
        # be reached with tab.
        self.query_one("#compose-editor", TextArea).focus()
        self._refresh_guidance()

    def on_unmount(self) -> None:
        self._teardown_pipe()

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        del event
        mirror = self.query_one(LivePaneViewer)
        if self._mirror_input_active and not mirror.has_focus:
            self._mirror_input_active = False
        self._refresh_guidance()

    def _seed_and_stream(self, viewer: LivePaneViewer) -> None:
        assert self._adapter is not None
        try:
            snapshot = self._adapter.capture_snapshot(self._pane_id)
        except TmuxCommandError as exc:
            self._capture_error = f"✗ capture failed: {exc.stderr or 'tmux error'}"
            self.end_loading(viewer)
            self._loading_cleared = True
            return
        except OSError as exc:
            self._capture_error = f"✗ capture failed: {exc}"
            self.end_loading(viewer)
            self._loading_cleared = True
            return
        self._capture_error = None
        self._sync_warning = None
        viewer.set_snapshot(snapshot)
        self._last_snapshot = snapshot
        self.end_loading(viewer)
        self._loading_cleared = True
        try:
            self._adapter.start_pipe(self._pane_id, self._ring_path)
            self._pipe_started = True
            self._stream_warning = None
        except TmuxCommandError as exc:
            detail = exc.stderr or "tmux error"
            self._stream_warning = f"⚠ live stream unavailable ({detail}); snapshot sync only"
        except OSError as exc:
            self._stream_warning = f"⚠ live stream unavailable ({exc}); snapshot sync only"
        self.set_interval(_POLL_INTERVAL_SEC, self._drain_ring)
        self.set_interval(_SNAPSHOT_SYNC_INTERVAL_SEC, self._sync_snapshot)

    # ── polling ──────────────────────────────────────────────────────

    def _drain_ring(self) -> None:
        if self._adapter is None:
            return
        chunk = self._reader.read_new()
        if not chunk:
            return
        viewer = self.query_one(LivePaneViewer)
        viewer.append(chunk)
        if not self._loading_cleared:
            self.end_loading(viewer)
            self._loading_cleared = True
            self._refresh_guidance()

    def _sync_snapshot(self, *, force: bool = False) -> None:
        if self._adapter is None:
            return
        viewer = self.query_one(LivePaneViewer)
        try:
            snapshot = self._adapter.capture_snapshot(self._pane_id)
        except TmuxCommandError as exc:
            self._sync_warning = f"⚠ snapshot sync failed: {exc.stderr or 'tmux error'}"
            if force and not viewer.has_content:
                self._capture_error = self._sync_warning
            if force or not self._loading_cleared:
                self.end_loading(viewer)
                self._loading_cleared = True
            self._refresh_guidance(update_status=True)
            return
        except OSError as exc:
            self._sync_warning = f"⚠ snapshot sync failed: {exc}"
            if force and not viewer.has_content:
                self._capture_error = self._sync_warning
            if force or not self._loading_cleared:
                self.end_loading(viewer)
                self._loading_cleared = True
            self._refresh_guidance(update_status=True)
            return
        had_warning = self._capture_error is not None or self._sync_warning is not None
        self._capture_error = None
        self._sync_warning = None
        if snapshot != self._last_snapshot:
            if viewer.has_content:
                viewer.replace_tail(snapshot)
            else:
                viewer.set_snapshot(snapshot)
            self._last_snapshot = snapshot
        if force or not self._loading_cleared:
            self.end_loading(viewer)
            self._loading_cleared = True
        self._refresh_guidance(update_status=force or had_warning)

    def refresh_data(self) -> None:
        viewer = self.query_one(LivePaneViewer)
        self.begin_loading(viewer)
        self._sync_snapshot(force=True)

    # ── key handling ─────────────────────────────────────────────────

    async def on_key(self, event: events.Key) -> None:
        if self._handle_live_input_key(event):
            return
        mirror = self.query_one(LivePaneViewer)
        if event.key == "i" and mirror.has_focus and self._adapter is not None:
            self._set_mirror_input_mode(True)
            event.stop()
            event.prevent_default()
            return
        # Tab/Shift+Tab cycles focus between editor and mirror. The
        # editor consumes tab for indentation by default, so we handle
        # the swap at the screen level *before* it gets there.
        if event.key == "tab":
            self._swap_focus(forward=True)
            event.stop()
            event.prevent_default()
            return
        if event.key == "shift+tab":
            self._swap_focus(forward=False)
            event.stop()
            event.prevent_default()
            return
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            self.action_close()
            return

    def _handle_live_input_key(self, event: events.Key) -> bool:
        if not self._mirror_input_active or self._adapter is None:
            return False
        mirror = self.query_one(LivePaneViewer)
        if not mirror.has_focus:
            return False
        event.stop()
        event.prevent_default()
        if event.key == "escape":
            self._set_mirror_input_mode(False)
            return True
        translation = translate_textual_key(event.key)
        if translation is None:
            self.set_status(f"live input ignores {event.key!r} · esc stops live input")
            return True
        self._adapter.send_keys(self._pane_id, translation)
        return True

    def _swap_focus(self, *, forward: bool) -> None:
        del forward  # two-widget cycle — direction doesn't matter
        editor = self.query_one("#compose-editor", TextArea)
        mirror = self.query_one(LivePaneViewer)
        if editor.has_focus:
            mirror.focus()
        else:
            editor.focus()

    # ── actions ──────────────────────────────────────────────────────

    def action_send(self) -> None:
        if self.runtime.actions is None:
            self.set_status("✗ action service unavailable")
            return
        editor = self.query_one("#compose-editor", TextArea)
        text = editor.text.rstrip()
        if not text:
            self.set_status("nothing to send — type a message first")
            editor.focus()
            return
        result = self.runtime.actions.send_message(self._pane_id, text)
        if result.success:
            editor.text = ""
            editor.focus()
            self.set_status(f"✓ sent to {self._display_name} ({self._pane_id})")
        else:
            self.set_status(f"✗ {result.message}")

    def action_grow_editor(self) -> None:
        self._set_editor_height(self._editor_height + _EDITOR_HEIGHT_STEP)

    def action_shrink_editor(self) -> None:
        self._set_editor_height(self._editor_height - _EDITOR_HEIGHT_STEP)

    def action_close(self) -> None:
        self._teardown_pipe()
        self.app.pop_screen()

    # ── helpers ──────────────────────────────────────────────────────

    def _set_editor_height(self, next_height: int) -> None:
        clamped = max(_MIN_EDITOR_HEIGHT, min(_MAX_EDITOR_HEIGHT, next_height))
        if clamped == self._editor_height:
            return
        self._editor_height = clamped
        self._apply_editor_height()
        self._refresh_guidance()

    def _apply_editor_height(self) -> None:
        editor_wrap = self.query_one("#compose-editor-wrap", Vertical)
        editor_wrap.styles.height = self._editor_height

    def _set_mirror_input_mode(self, enabled: bool) -> None:
        if self._mirror_input_active == enabled:
            return
        self._mirror_input_active = enabled
        if enabled:
            self.query_one(LivePaneViewer).focus()
        self._refresh_guidance()

    def _refresh_guidance(self, *, update_status: bool = True) -> None:
        if not self.is_mounted:
            return
        viewer = self.query_one(LivePaneViewer)
        label = self.query_one("#compose-editor-label", Label)
        label.update(self._editor_label(viewer))
        if self._mirror_input_active and viewer.has_focus:
            viewer.add_class("-input-on")
        else:
            viewer.remove_class("-input-on")
        viewer.border_subtitle = self._viewer_subtitle(viewer)
        if update_status:
            self.set_status(self._status_message(viewer))

    def _editor_label(self, viewer: LivePaneViewer) -> str:
        if self._mirror_input_active and viewer.has_focus:
            return (
                f"live input on · keys go to tmux · esc stop · editor {self._editor_height} lines"
            )
        editor = self.query_one("#compose-editor", TextArea)
        if editor.has_focus:
            return (
                f"compose ({self._editor_height} lines) · ctrl+s send · "
                "tab mirror · i on mirror to interact"
            )
        return (
            f"mirror focus · i interact · tab editor · alt+up/down resize · "
            f"editor {self._editor_height} lines"
        )

    def _viewer_subtitle(self, viewer: LivePaneViewer) -> str:
        if self._mirror_input_active and viewer.has_focus:
            return "live input · esc stops"
        if self._capture_error is not None:
            return "capture failed"
        if self._sync_warning is not None:
            return "snapshot sync warning · r retry"
        if self._stream_warning is not None:
            return "snapshot sync only · r resync"
        if not viewer.has_content:
            return "waiting for pane output · live mirror armed"
        return "live mirror + snapshot sync · r resync"

    def _status_message(self, viewer: LivePaneViewer) -> str:
        if self._capture_error is not None:
            return self._capture_error
        if self._mirror_input_active and viewer.has_focus:
            guidance = (
                f"live input → {self._display_name} ({self._pane_id}) · keys go to tmux · esc stops"
            )
        elif viewer.has_focus:
            guidance = (
                f"mirror → {self._display_name} ({self._pane_id}) · "
                "scroll freely · i interact · tab editor"
            )
        else:
            guidance = (
                f"compose → {self._display_name} ({self._pane_id}) · "
                "ctrl+s send · tab focus · alt+up/down resize · i interact"
            )
        warning = self._sync_warning or self._stream_warning
        if warning is not None:
            return f"{warning} · {guidance}"
        if not viewer.has_content:
            return f"{guidance} · waiting for pane output"
        return guidance

    def _teardown_pipe(self) -> None:
        if self._adapter is not None and self._pipe_started:
            self._pipe_started = False
            try:
                self._adapter.stop_pipe(self._pane_id)
            except TmuxCommandError:
                pass
            except OSError:
                pass
        with contextlib.suppress(FileNotFoundError, OSError):
            self._ring_path.unlink()

    def _format_mirror_title(self) -> str:
        return f"mirror · pane {self._pane_id} — {self._display_name}"


__all__ = [
    "COMPOSE_MIRROR_BINDINGS",
    "COMPOSE_MIRROR_HINTS",
    "ComposeWithMirrorScreen",
]
