"""Tmux pane streaming for the live pane viewer screen.

The pane viewer works in two halves:

* ``PaneStreamAdapter`` is a narrow tmux-facing adapter. It seeds the
  viewer with the current scrollback (``capture-pane -p -e -J``), wires
  ``pipe-pane`` so every new byte written to the real pane is appended
  to a session-scoped ring file, and on close tears the pipe down. It
  also forwards translated key events via ``send-keys``.

* ``PaneRingReader`` is a pure tailer over that ring file. It remembers
  the last byte offset + inode so repeated ``read_new()`` calls only
  return bytes written since the previous call. It is deliberately
  independent from Textual so we can test it with plain ``tmp_path``
  files — no real tmux processes are ever spawned in the test suite.

The adapter is keyed on a ``TmuxPaneStream`` Protocol so tests inject a
tiny fake that records calls; the real implementation is
``TmuxAdapter`` (see ``pipe_pane_to_file`` / ``stop_pipe_pane`` /
``capture_pane(include_escape_sequences=...)``).

Keys typed inside the viewer are translated to the tmux ``send-keys``
vocabulary via :func:`translate_textual_key`. Printable characters are
forwarded with ``-l`` (literal) so shell metacharacters don't trigger
tmux key-name lookups; control keys (Enter, Ctrl-C, arrows, …) use
their symbolic tmux names.
"""

from __future__ import annotations

import logging
import re
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

_log = logging.getLogger(__name__)


# ── tmux protocol ────────────────────────────────────────────────────


class TmuxPaneStream(Protocol):
    """Minimal tmux surface the pane viewer needs.

    Kept narrow on purpose so tests can provide a recording fake
    without standing up the full :class:`TmuxAdapter`.
    """

    def capture_pane(
        self,
        target_pane: str,
        /,
        *,
        start_line: str | int | None = ...,
        end_line: str | int | None = ...,
        join_wrapped_lines: bool = ...,
        include_escape_sequences: bool = ...,
    ) -> str: ...

    def pipe_pane_to_file(
        self,
        target_pane: str,
        /,
        *,
        target_path: Path,
        append: bool = ...,
    ) -> None: ...

    def stop_pipe_pane(self, target_pane: str, /) -> None: ...

    def send_keys(
        self,
        target_pane: str,
        keys: Sequence[str],
        /,
        *,
        literal: bool = ...,
        append_enter: bool = ...,
    ) -> object: ...

    def pane_exists(self, target_pane: str, /) -> bool: ...


# ── key translation ──────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class KeyTranslation:
    """Result of translating a Textual key name to tmux ``send-keys`` args.

    ``keys`` is always non-empty for supported inputs. ``literal`` is
    ``True`` when the payload must be forwarded verbatim (printable
    text) and ``False`` when tmux should interpret its symbolic names
    (``Enter``, ``C-c``, ``Up``, …).
    """

    keys: tuple[str, ...]
    literal: bool


# Textual key names → tmux send-keys symbolic names. Covers the
# navigation/control vocabulary; printable keys go through the
# single-character fallback below.
_SYMBOLIC_KEYS: dict[str, str] = {
    "enter": "Enter",
    "return": "Enter",
    "tab": "Tab",
    "shift+tab": "BTab",
    "escape": "Escape",
    "esc": "Escape",
    "backspace": "BSpace",
    "delete": "DC",
    "space": "Space",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "home": "Home",
    "end": "End",
    "pageup": "PageUp",
    "page_up": "PageUp",
    "pagedown": "PageDown",
    "page_down": "PageDown",
    "insert": "IC",
}

# Printable ASCII we'll pass through with ``-l``.  Anything outside
# this set goes through the symbolic path.
_PRINTABLE_RE = re.compile(r"^[\x20-\x7e]$")


def translate_textual_key(key: str) -> KeyTranslation | None:
    """Translate a Textual ``Key.key`` name into a tmux send-keys payload.

    Returns ``None`` for key names we don't know how to forward
    (function keys we haven't mapped, multi-char sequences without a
    tmux equivalent). The viewer should silently ignore those rather
    than guess — guessing is how you accidentally send ``C-c`` into
    someone's agent.

    Rules:

    * A single printable ASCII character → ``(keys=(char,), literal=True)``
      so shell metacharacters survive unmodified.
    * ``ctrl+<letter>``, ``ctrl+<digit>`` → ``(keys=("C-x",), literal=False)``.
    * ``alt+<x>`` → ``(keys=("M-x",), literal=False)``.
    * Named keys (``enter``, ``up``, …) → their tmux name, non-literal.
    """
    if not key:
        return None
    lowered = key.lower()

    # Modifier-prefixed single keys: ctrl+x, alt+x, ctrl+alt+x.
    # Only handle single-char targets — multi-char modifier combos like
    # ``ctrl+shift+home`` don't have a clean tmux mapping, skip them.
    modifier_match = _parse_modified_key(lowered)
    if modifier_match is not None:
        return modifier_match

    # Whole-key lookup (enter, up, shift+tab, …).
    if lowered in _SYMBOLIC_KEYS:
        return KeyTranslation(keys=(_SYMBOLIC_KEYS[lowered],), literal=False)

    # Single printable character (letter, digit, punctuation).  Use the
    # *original* ``key`` so casing is preserved (``A`` vs ``a``).
    if len(key) == 1 and _PRINTABLE_RE.match(key):
        return KeyTranslation(keys=(key,), literal=True)

    return None


def _parse_modified_key(lowered: str) -> KeyTranslation | None:
    parts = lowered.split("+")
    if len(parts) < 2:
        return None
    *modifiers, base = parts
    # Only ctrl and alt map cleanly onto tmux's ``C-`` / ``M-`` prefixes.
    if not modifiers or any(m not in ("ctrl", "alt") for m in modifiers):
        return None
    if not base or len(base) != 1 or not _PRINTABLE_RE.match(base):
        return None
    prefix = ""
    if "ctrl" in modifiers:
        prefix += "C-"
    if "alt" in modifiers:
        prefix += "M-"
    return KeyTranslation(keys=(f"{prefix}{base}",), literal=False)


# ── ring-file tailer ─────────────────────────────────────────────────


@dataclass(slots=True)
class _RingState:
    inode: int = 0
    offset: int = 0
    last_size: int = 0
    partial: str = ""


class PaneRingReader:
    """Incremental tailer over the pane-viewer ring file.

    The adapter wires tmux ``pipe-pane -o 'cat >> ring.log'``; this
    reader polls the file and returns the bytes that were appended
    since the last call. It handles:

    * Inode change or size shrink → treat as rotation, re-read from
      the current EOF anchor and drop buffered state.
    * A partial trailing line (pipe-pane flushes without newline
      boundaries) is buffered and re-prepended on the next read so
      callers see complete chunks.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._state = _RingState()

    @property
    def path(self) -> Path:
        return self._path

    def read_new(self) -> str:
        """Return bytes appended since the previous call.

        Returns an empty string when the file is missing, empty, or
        unchanged. Never raises on filesystem errors — the viewer
        should degrade to "no new output" rather than crash.
        """
        try:
            stat = self._path.stat()
        except FileNotFoundError:
            # File hasn't been created yet (pipe-pane is still
            # spinning up) — nothing to do, keep state intact so a
            # later read picks up from offset 0 once it exists.
            return ""
        except OSError as exc:
            _log.debug("pane ring stat failed for %s: %s", self._path, exc)
            return ""

        state = self._state
        rotated = stat.st_ino != state.inode or stat.st_size < state.last_size
        if rotated:
            state.inode = stat.st_ino
            state.offset = 0
            state.last_size = 0
            state.partial = ""

        if stat.st_size == state.last_size:
            return ""

        try:
            with self._path.open("rb") as fh:
                fh.seek(state.offset)
                chunk = fh.read(stat.st_size - state.offset)
        except OSError as exc:
            _log.debug("pane ring read failed for %s: %s", self._path, exc)
            return ""

        state.offset = stat.st_size
        state.last_size = stat.st_size
        text = state.partial + chunk.decode("utf-8", errors="replace")
        # Buffer anything after the final newline so callers never see
        # half-written lines. If there is *no* newline in the entire
        # chunk we still flush — partial control sequences are the
        # common case for raw tmux output and deferring them forever
        # means the viewer freezes.
        last_newline = text.rfind("\n")
        if last_newline == -1:
            # No line boundary yet — avoid deferring forever by
            # flushing once the buffered partial grows past 4 KiB
            # (roughly a full terminal line of ANSI).
            if len(text) > 4096:
                state.partial = ""
                return text
            state.partial = text
            return ""
        state.partial = text[last_newline + 1 :]
        return text[: last_newline + 1]

    def reset(self) -> None:
        """Drop cached state so the next read re-anchors to EOF."""
        self._state = _RingState()


# ── ring-buffer helper (for widget + tests) ──────────────────────────


class RingLineBuffer:
    """Fixed-capacity FIFO of text lines.

    Used by :class:`LivePaneViewer` to enforce its line cap
    deterministically. Kept in its own module so tests can exercise
    the eviction logic without instantiating a Textual widget.
    """

    def __init__(self, max_lines: int) -> None:
        if max_lines <= 0:
            msg = "max_lines must be positive"
            raise ValueError(msg)
        self._buf: deque[str] = deque(maxlen=max_lines)
        self._max = max_lines

    @property
    def max_lines(self) -> int:
        return self._max

    def __len__(self) -> int:
        return len(self._buf)

    def append_text(self, text: str) -> tuple[str, ...]:
        """Split ``text`` on newlines and append. Returns the new lines.

        An empty trailing segment (from ``"foo\\n".split("\\n")``) is
        dropped so a caller passing whole lines doesn't accidentally
        store a blank tail. Empty input returns an empty tuple.
        """
        if not text:
            return ()
        parts = text.split("\n")
        # Drop the empty final segment produced by a trailing \n so
        # ``"line\n"`` stores exactly one line, not one + "".
        if parts[-1] == "":
            parts.pop()
        if not parts:
            return ()
        added = tuple(parts)
        self._buf.extend(added)
        return added

    def lines(self) -> tuple[str, ...]:
        return tuple(self._buf)


# ── adapter ──────────────────────────────────────────────────────────


class PaneStreamAdapter:
    """Orchestrate tmux seed + pipe-pane lifecycle for a viewer screen.

    One adapter instance can drive many viewers over time: each
    :meth:`seed` / :meth:`start_pipe` / :meth:`stop_pipe` call is
    independent and keyed on ``pane_id``. The adapter holds no state;
    it's a thin typed wrapper around :class:`TmuxPaneStream` so the
    screen doesn't have to know about tmux argument shapes.
    """

    def __init__(self, tmux: TmuxPaneStream) -> None:
        self._tmux = tmux

    def seed(self, pane_id: str) -> str:
        """Return the current scrollback with ANSI sequences preserved.

        Raises whatever :class:`TmuxPaneStream` raises — callers wrap
        in try/except to surface "pane vanished" as a status message
        rather than a crash.
        """
        return self._tmux.capture_pane(
            pane_id,
            join_wrapped_lines=True,
            include_escape_sequences=True,
        )

    def start_pipe(self, pane_id: str, target_path: Path) -> None:
        """Begin streaming pane output into ``target_path``.

        The file is created (and its parent directory) if needed; if
        it already exists it's truncated so a previous session's tail
        doesn't bleed into this viewer.
        """
        target_path.parent.mkdir(parents=True, exist_ok=True)
        # Truncate with an explicit write — relying on shell ``>``
        # redirection via pipe-pane would race the first tmux flush.
        target_path.write_bytes(b"")
        self._tmux.pipe_pane_to_file(pane_id, target_path=target_path, append=True)

    def stop_pipe(self, pane_id: str) -> None:
        """Stop streaming. Safe to call if the pane has already died."""
        self._tmux.stop_pipe_pane(pane_id)

    def send_keys(self, pane_id: str, translation: KeyTranslation) -> None:
        """Forward a translated keystroke to the pane."""
        if not translation.keys:
            return
        self._tmux.send_keys(
            pane_id,
            translation.keys,
            literal=translation.literal,
        )

    def pane_exists(self, pane_id: str) -> bool:
        return self._tmux.pane_exists(pane_id)


# ── helpers ──────────────────────────────────────────────────────────


def ring_file_for_pane(root: Path, pane_id: str) -> Path:
    """Return the per-pane ring-file path.

    ``pane_id`` comes straight from tmux (``%42``); we normalise the
    leading ``%`` and any unsafe characters to keep the filename
    portable. One file per pane-id keeps parallel viewers on
    different panes from stepping on each other.
    """
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", pane_id.lstrip("%"))
    if not safe:
        safe = "pane"
    return root / f"muxdeck-pane-{safe}.log"


__all__ = [
    "KeyTranslation",
    "PaneRingReader",
    "PaneStreamAdapter",
    "RingLineBuffer",
    "TmuxPaneStream",
    "ring_file_for_pane",
    "translate_textual_key",
]
