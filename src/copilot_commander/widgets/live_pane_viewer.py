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

from rich.ansi import AnsiDecoder
from rich.text import Text
from textual.widgets import RichLog

from copilot_commander.adapters.pane_stream import RingLineBuffer

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

    DEFAULT_CSS = """
    LivePaneViewer {
        height: 1fr;
        width: 1fr;
        background: #1d2021;
        border: solid #504945;
        border-title-color: #bdae93;
        border-title-style: bold;
        padding: 0 1;
        scrollbar-color: #504945 #1d2021;
        scrollbar-color-hover: #665c54 #1d2021;
        scrollbar-color-active: #83a598 #1d2021;
    }

    LivePaneViewer:focus {
        border: solid #83a598;
    }

    LivePaneViewer.-input-on {
        border: double #fabd2f;
        border-title-color: #fabd2f;
    }

    LivePaneViewer.-input-on:focus {
        border: double #fabd2f;
    }
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
        """Replace only the newest lines with a corrected tmux snapshot."""
        text = payload.plain if isinstance(payload, Text) else payload
        if not text:
            return
        self._buffer.replace_tail_text(text)
        self._has_content = self.buffer_line_count > 0
        self._rerender_from_buffer()

    def clear_buffer(self) -> None:
        """Drop all rendered content and reset the ring buffer."""
        self._buffer = RingLineBuffer(self._buffer.max_lines)
        self._has_content = False
        self.clear()

    # ── internals ────────────────────────────────────────────────────

    def _render_string(self, payload: str) -> None:
        # AnsiDecoder emits one Text per newline-separated line.  Feed
        # them in order; RichLog adds its own newline separation.
        for line in self._decoder.decode(payload.rstrip("\n")):
            self._write_styled(line)

    def _rerender_from_buffer(self) -> None:
        if not self.is_mounted:
            return
        follow_tail = self.is_vertical_scroll_end
        previous_scroll_y = self.scroll_y
        lines = self._buffer.lines()
        self.clear()
        for raw_line in lines:
            self._render_string(raw_line)
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
