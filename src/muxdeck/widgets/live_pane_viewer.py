"""Live streaming tmux pane viewer widget.

Thin wrapper around :class:`textual.widgets.RichLog` that exposes an
``append`` API taking either raw ANSI-bearing strings or pre-decoded
:class:`rich.text.Text` objects. The widget keeps a bounded line
history (via ``RichLog.max_lines``) and an explicit
:class:`RingLineBuffer` so plain-Python callers (and tests) can
inspect what the viewer would show without a running Textual app.

Business logic — ANSI decoding, line cap enforcement, scroll-follow
decisions — lives here rather than in the screen so the screen stays
a thin controller that wires the adapter, reader, and widget together.
"""

from __future__ import annotations

from collections import deque

from rich.ansi import AnsiDecoder
from rich.text import Text
from textual.widgets import RichLog

from muxdeck import theme
from muxdeck.adapters.pane_stream import RingLineBuffer

# Cap on how many lines the viewer retains. 2 000 lines is ~80 full
# terminal screens which is plenty for reviewing recent agent output
# without letting the buffer grow without bound on a long-running
# session.
DEFAULT_MAX_LINES = 2000


class LivePaneViewer(RichLog):
    """Streams decoded pane output into a scrollable, auto-following log.

    Uses RichLog's built-in ``auto_scroll`` semantics: when the user
    scrolls up the widget stops following the tail; scrolling back to
    the bottom resumes follow.  Callers push new output via
    :meth:`append`.

    The widget maintains a :class:`RingLineBuffer` in parallel with
    RichLog's internal queue so tests can assert on the line cap
    without running the Textual event loop.
    """

    DEFAULT_CSS = f"""
    LivePaneViewer {{
        height: 1fr;
        width: 1fr;
        background: {theme.BG_HARD};
        border: solid {theme.BORDER};
        border-title-color: {theme.PANEL_TITLE};
        border-title-style: bold;
        padding: 0 1;
        scrollbar-color: {theme.BG3} {theme.BG_HARD};
        scrollbar-color-hover: {theme.BG4} {theme.BG_HARD};
        scrollbar-color-active: {theme.BORDER_FOCUS} {theme.BG_HARD};
    }}

    LivePaneViewer:focus {{
        border: solid {theme.BORDER_FOCUS};
    }}

    LivePaneViewer.-input-on {{
        border: double {theme.YELLOW};
        border-title-color: {theme.YELLOW};
    }}

    LivePaneViewer.-input-on:focus {{
        border: double {theme.YELLOW};
    }}
    """

    def __init__(
        self,
        *,
        max_lines: int = DEFAULT_MAX_LINES,
        widget_id: str | None = None,
    ) -> None:
        super().__init__(
            id=widget_id,
            max_lines=max_lines,
            wrap=False,
            highlight=False,
            markup=False,
            auto_scroll=True,
        )
        self.border_title = "Pane Viewer"
        self._decoder = AnsiDecoder()
        self._buffer = RingLineBuffer(max_lines)
        # Parallel deque of pre-decoded Rich ``Text`` lines aligned to
        # ``self._buffer``. Re-rendering the viewer (e.g. when a tmux
        # snapshot corrects the tail) used to call ``AnsiDecoder.decode``
        # over every buffered line, which on a full 2000-line buffer
        # translates to a multi-hundred-millisecond hitch on the UI
        # thread. By caching decoded lines we replay them straight into
        # ``RichLog.write`` without re-parsing ANSI.
        self._decoded: deque[Text] = deque(maxlen=max_lines)
        self._has_content = False

    @property
    def buffer_line_count(self) -> int:
        return len(self._buffer)

    @property
    def buffer_lines(self) -> tuple[str, ...]:
        return self._buffer.lines()

    @property
    def has_content(self) -> bool:
        return self._has_content

    @property
    def wrap_enabled(self) -> bool:
        return bool(self.wrap)

    @property
    def follow_enabled(self) -> bool:
        return bool(self.auto_scroll)

    @property
    def follow_state(self) -> str:
        if not self.follow_enabled:
            return "off"
        if self.is_vertical_scroll_end:
            return "on"
        return "paused"

    def set_wrap_mode(self, enabled: bool) -> None:
        if self.wrap == enabled:
            return
        self.wrap = enabled
        self._rerender_from_buffer()

    def set_follow_mode(self, enabled: bool) -> None:
        if self.auto_scroll == enabled:
            return
        self.auto_scroll = enabled
        if enabled and self.is_mounted:
            self.scroll_end(animate=False, immediate=True, force=True, x_axis=False)

    def append(self, payload: str | Text) -> None:
        """Append a chunk of pane output.

        Strings are treated as raw terminal output and run through
        :class:`rich.ansi.AnsiDecoder` so SGR sequences render as
        styled text. ``Text`` objects are written as-is. Empty /
        whitespace-only chunks are a no-op (avoids flashing blank
        lines for heartbeat writes).
        """
        if isinstance(payload, Text):
            text = payload.plain
            if not text:
                return
            self._buffer.append_text(text)
            self._decoded.append(payload)
            self._write_styled(payload)
            self._has_content = True
            return

        if not payload:
            return
        self._buffer.append_text(payload)
        self._render_string(payload)
        self._has_content = True

    def set_snapshot(self, payload: str | Text) -> None:
        """Replace the entire viewer contents with a fresh pane snapshot."""
        self.clear_buffer()
        self.append(payload)
        if self.is_mounted:
            self.scroll_end(animate=False, immediate=True, force=True, x_axis=False)

    def replace_tail(self, payload: str | Text) -> None:
        """Replace only the newest lines with a corrected tmux snapshot.

        Snapshot sync runs independently from the pipe-pane stream. When
        the live stream has already appended the same buffered tail we
        avoid a full RichLog replay so the mirror doesn't visibly jump
        during routine resyncs.
        """
        text = payload.plain if isinstance(payload, Text) else payload
        if not text:
            return
        if self.matches_snapshot_tail(payload):
            return
        self._buffer.replace_tail_text(text)
        # Invalidate the decoded cache; the buffer's tail content has
        # changed so the cached Text deque must be rebuilt from scratch
        # in ``_rerender_from_buffer``. We force the rebuild by making
        # the lengths disagree with the buffer.
        self._decoded.clear()
        after = self._buffer.lines()
        self._has_content = len(after) > 0
        self._rerender_from_buffer()

    def matches_snapshot_tail(self, payload: str | Text) -> bool:
        """Return whether the viewer already ends with ``payload``."""
        snapshot_lines = self._payload_lines(payload)
        buffer_lines = self._buffer.lines()
        if not snapshot_lines:
            return len(buffer_lines) == 0
        if len(snapshot_lines) > len(buffer_lines):
            return False
        return buffer_lines[-len(snapshot_lines) :] == snapshot_lines

    def clear_buffer(self) -> None:
        """Drop all rendered content and reset the ring buffer."""
        self._buffer = RingLineBuffer(self._buffer.max_lines)
        self._decoded = deque(maxlen=self._buffer.max_lines)
        self._decoder = AnsiDecoder()
        self._has_content = False
        self.clear()

    # ── internals ────────────────────────────────────────────────────

    def _render_string(self, payload: str) -> None:
        # AnsiDecoder emits one Text per newline-separated line.  Feed
        # them in order; RichLog adds its own newline separation.
        for line in self._decoder.decode(payload.rstrip("\n")):
            self._decoded.append(line)
            self._write_styled(line)

    @staticmethod
    def _payload_lines(payload: str | Text) -> tuple[str, ...]:
        text = payload.plain if isinstance(payload, Text) else payload
        if not text:
            return ()
        parts = text.split("\n")
        if parts[-1] == "":
            parts.pop()
        return tuple(parts)

    def _rerender_from_buffer(self) -> None:
        if not self.is_mounted:
            return
        follow_tail = self.is_vertical_scroll_end
        previous_scroll_y = self.scroll_y
        lines = self._buffer.lines()
        # Re-decode only when the cached Text deque has drifted out of
        # sync with the line buffer (e.g. after ``replace_tail_text``
        # drops or replaces lines). The common case — a periodic snapshot
        # that already matches the buffered tail — short-circuits in
        # ``replace_tail`` before reaching here, so the slow rebuild
        # stays off the hot path entirely.
        if len(self._decoded) != len(lines):
            self._decoder = AnsiDecoder()
            self._decoded = deque(maxlen=self._buffer.max_lines)
            for raw_line in lines:
                for decoded in self._decoder.decode(raw_line):
                    self._decoded.append(decoded)
        self.clear()
        for cached in self._decoded:
            self._write_styled(cached)
        if follow_tail:
            self.scroll_end(animate=False, immediate=True, force=True, x_axis=False)
            return
        self.scroll_to(y=previous_scroll_y, animate=False, immediate=True, force=True)

    def _write_styled(self, text: Text) -> None:
        # RichLog.write is the documented append API. Guard with
        # ``is_mounted`` so unit tests that construct the widget
        # without an app don't blow up on the underlying deque.
        if self.is_mounted:
            self.write(text)


__all__ = ["DEFAULT_MAX_LINES", "LivePaneViewer"]
