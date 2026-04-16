"""Unit tests for :mod:`copilot_commander.adapters.pane_stream`.

These tests never spawn a real tmux process — they exercise the
adapter against a recording fake :class:`TmuxPaneStream` and the ring
reader against ``tmp_path`` files.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from copilot_commander.adapters.pane_stream import (
    KeyTranslation,
    PaneRingReader,
    PaneStreamAdapter,
    RingLineBuffer,
    ring_file_for_pane,
    translate_textual_key,
)

# ── fake tmux ────────────────────────────────────────────────────────


@dataclass
class _PipeCall:
    pane_id: str
    target_path: Path
    append: bool


@dataclass
class _SendKeysCall:
    pane_id: str
    keys: tuple[str, ...]
    literal: bool
    append_enter: bool


@dataclass
class FakeTmuxPaneStream:
    """Recording fake that also returns canned capture-pane output."""

    capture_output: str = ""
    pane_ids_present: frozenset[str] = frozenset({"%1"})
    capture_calls: list[tuple[str, bool, bool]] = field(default_factory=list)
    pipe_calls: list[_PipeCall] = field(default_factory=list)
    stop_pipe_calls: list[str] = field(default_factory=list)
    send_keys_calls: list[_SendKeysCall] = field(default_factory=list)
    pane_exists_calls: list[str] = field(default_factory=list)

    def capture_pane(
        self,
        target_pane: str,
        /,
        *,
        start_line: str | int | None = None,
        end_line: str | int | None = None,
        join_wrapped_lines: bool = False,
        include_escape_sequences: bool = False,
    ) -> str:
        del start_line, end_line
        self.capture_calls.append(
            (target_pane, join_wrapped_lines, include_escape_sequences),
        )
        return self.capture_output

    def pipe_pane_to_file(
        self,
        target_pane: str,
        /,
        *,
        target_path: Path,
        append: bool = True,
    ) -> None:
        self.pipe_calls.append(
            _PipeCall(pane_id=target_pane, target_path=target_path, append=append),
        )

    def stop_pipe_pane(self, target_pane: str, /) -> None:
        self.stop_pipe_calls.append(target_pane)

    def send_keys(
        self,
        target_pane: str,
        keys: Sequence[str],
        /,
        *,
        literal: bool = False,
        append_enter: bool = False,
    ) -> object:
        self.send_keys_calls.append(
            _SendKeysCall(
                pane_id=target_pane,
                keys=tuple(keys),
                literal=literal,
                append_enter=append_enter,
            ),
        )
        return None

    def pane_exists(self, target_pane: str, /) -> bool:
        self.pane_exists_calls.append(target_pane)
        return target_pane in self.pane_ids_present


# ── adapter ──────────────────────────────────────────────────────────


class TestSeed:
    def test_passes_colour_and_join_flags(self) -> None:
        tmux = FakeTmuxPaneStream(capture_output="hello")
        adapter = PaneStreamAdapter(tmux)
        assert adapter.seed("%1") == "hello"
        assert tmux.capture_calls == [("%1", True, True)]

    def test_returns_empty_when_pane_is_empty(self) -> None:
        tmux = FakeTmuxPaneStream(capture_output="")
        adapter = PaneStreamAdapter(tmux)
        assert adapter.seed("%1") == ""


class TestStartPipe:
    def test_truncates_existing_ring_file_and_starts_pipe(self, tmp_path: Path) -> None:
        target = tmp_path / "sub" / "ring.log"
        # File exists with stale content that must be cleared so the
        # next viewer doesn't replay another session's tail.
        target.parent.mkdir(parents=True)
        target.write_text("stale\n", encoding="utf-8")
        tmux = FakeTmuxPaneStream()
        adapter = PaneStreamAdapter(tmux)
        adapter.start_pipe("%1", target)
        assert target.exists()
        assert target.read_bytes() == b""
        assert tmux.pipe_calls == [
            _PipeCall(pane_id="%1", target_path=target, append=True),
        ]

    def test_creates_parent_directory_when_missing(self, tmp_path: Path) -> None:
        target = tmp_path / "nested" / "deeper" / "ring.log"
        tmux = FakeTmuxPaneStream()
        adapter = PaneStreamAdapter(tmux)
        adapter.start_pipe("%1", target)
        assert target.parent.is_dir()
        assert target.exists()


class TestStopPipe:
    def test_forwards_to_tmux(self) -> None:
        tmux = FakeTmuxPaneStream()
        adapter = PaneStreamAdapter(tmux)
        adapter.stop_pipe("%1")
        assert tmux.stop_pipe_calls == ["%1"]


class TestSendKeys:
    def test_forwards_literal_printable(self) -> None:
        tmux = FakeTmuxPaneStream()
        adapter = PaneStreamAdapter(tmux)
        translation = KeyTranslation(keys=("x",), literal=True)
        adapter.send_keys("%1", translation)
        assert tmux.send_keys_calls == [
            _SendKeysCall(pane_id="%1", keys=("x",), literal=True, append_enter=False),
        ]

    def test_forwards_symbolic_non_literal(self) -> None:
        tmux = FakeTmuxPaneStream()
        adapter = PaneStreamAdapter(tmux)
        translation = KeyTranslation(keys=("C-c",), literal=False)
        adapter.send_keys("%1", translation)
        assert tmux.send_keys_calls == [
            _SendKeysCall(pane_id="%1", keys=("C-c",), literal=False, append_enter=False),
        ]

    def test_ignores_empty_translation(self) -> None:
        tmux = FakeTmuxPaneStream()
        adapter = PaneStreamAdapter(tmux)
        adapter.send_keys("%1", KeyTranslation(keys=(), literal=False))
        assert tmux.send_keys_calls == []


class TestPaneExists:
    def test_delegates(self) -> None:
        tmux = FakeTmuxPaneStream(pane_ids_present=frozenset({"%2"}))
        adapter = PaneStreamAdapter(tmux)
        assert adapter.pane_exists("%2") is True
        assert adapter.pane_exists("%missing") is False
        assert tmux.pane_exists_calls == ["%2", "%missing"]


# ── ring file helper ─────────────────────────────────────────────────


class TestRingFileForPane:
    def test_strips_leading_percent(self, tmp_path: Path) -> None:
        assert ring_file_for_pane(tmp_path, "%42").name == "muxdeck-pane-42.log"

    def test_sanitises_unsafe_chars(self, tmp_path: Path) -> None:
        assert ring_file_for_pane(tmp_path, "/weird id").name == "muxdeck-pane-_weird_id.log"

    def test_falls_back_when_id_is_empty(self, tmp_path: Path) -> None:
        assert ring_file_for_pane(tmp_path, "%").name == "muxdeck-pane-pane.log"


# ── ring reader ──────────────────────────────────────────────────────


class TestPaneRingReader:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        reader = PaneRingReader(tmp_path / "absent.log")
        assert reader.read_new() == ""

    def test_reads_appended_bytes_once(self, tmp_path: Path) -> None:
        path = tmp_path / "ring.log"
        path.write_text("first line\n", encoding="utf-8")
        reader = PaneRingReader(path)
        assert reader.read_new() == "first line\n"
        # No new data → empty.
        assert reader.read_new() == ""
        with path.open("a", encoding="utf-8") as fh:
            fh.write("second\n")
        assert reader.read_new() == "second\n"

    def test_buffers_partial_trailing_line(self, tmp_path: Path) -> None:
        path = tmp_path / "ring.log"
        path.write_text("partial", encoding="utf-8")
        reader = PaneRingReader(path)
        # No newline yet — reader buffers and returns nothing.
        assert reader.read_new() == ""
        with path.open("a", encoding="utf-8") as fh:
            fh.write(" tail\n")
        # Partial is re-joined and flushed as one line.
        assert reader.read_new() == "partial tail\n"

    def test_rotation_on_shrink_resets_offset(self, tmp_path: Path) -> None:
        path = tmp_path / "ring.log"
        path.write_text("old content long\n", encoding="utf-8")
        reader = PaneRingReader(path)
        assert reader.read_new() == "old content long\n"
        # Rotation: file shrinks (or inode changes). We re-anchor
        # from the new EOF — any newly appended bytes are visible.
        path.write_text("new\n", encoding="utf-8")
        assert reader.read_new() == "new\n"

    def test_decodes_utf8_and_tolerates_bad_bytes(self, tmp_path: Path) -> None:
        path = tmp_path / "ring.log"
        path.write_bytes("café\n".encode() + b"\xff\n")
        reader = PaneRingReader(path)
        out = reader.read_new()
        assert "café" in out
        assert "\n" in out

    def test_flushes_large_partial_without_newline(self, tmp_path: Path) -> None:
        path = tmp_path / "ring.log"
        # 5 KiB of no-newline data — exceeds the 4 KiB buffer so the
        # reader must flush rather than defer forever.
        payload = "a" * 5000
        path.write_text(payload, encoding="utf-8")
        reader = PaneRingReader(path)
        assert reader.read_new() == payload


# ── ring line buffer ─────────────────────────────────────────────────


class TestRingLineBuffer:
    def test_splits_on_newlines(self) -> None:
        buf = RingLineBuffer(max_lines=10)
        buf.append_text("one\ntwo\nthree\n")
        assert buf.lines() == ("one", "two", "three")

    def test_drops_trailing_empty(self) -> None:
        buf = RingLineBuffer(max_lines=10)
        buf.append_text("solo\n")
        assert buf.lines() == ("solo",)

    def test_respects_max_lines(self) -> None:
        buf = RingLineBuffer(max_lines=3)
        buf.append_text("a\nb\nc\nd\ne\n")
        assert buf.lines() == ("c", "d", "e")
        assert len(buf) == 3

    def test_empty_input_is_noop(self) -> None:
        buf = RingLineBuffer(max_lines=3)
        buf.append_text("")
        assert buf.lines() == ()

    def test_rejects_invalid_cap(self) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            RingLineBuffer(max_lines=0)


# ── key translation ──────────────────────────────────────────────────


class TestTranslateTextualKey:
    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            ("enter", KeyTranslation(keys=("Enter",), literal=False)),
            ("escape", KeyTranslation(keys=("Escape",), literal=False)),
            ("tab", KeyTranslation(keys=("Tab",), literal=False)),
            ("shift+tab", KeyTranslation(keys=("BTab",), literal=False)),
            ("backspace", KeyTranslation(keys=("BSpace",), literal=False)),
            ("delete", KeyTranslation(keys=("DC",), literal=False)),
            ("up", KeyTranslation(keys=("Up",), literal=False)),
            ("down", KeyTranslation(keys=("Down",), literal=False)),
            ("left", KeyTranslation(keys=("Left",), literal=False)),
            ("right", KeyTranslation(keys=("Right",), literal=False)),
            ("pageup", KeyTranslation(keys=("PageUp",), literal=False)),
            ("page_down", KeyTranslation(keys=("PageDown",), literal=False)),
            ("space", KeyTranslation(keys=("Space",), literal=False)),
            ("home", KeyTranslation(keys=("Home",), literal=False)),
            ("end", KeyTranslation(keys=("End",), literal=False)),
        ],
    )
    def test_symbolic_names(self, key: str, expected: KeyTranslation) -> None:
        assert translate_textual_key(key) == expected

    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            ("ctrl+c", KeyTranslation(keys=("C-c",), literal=False)),
            ("ctrl+d", KeyTranslation(keys=("C-d",), literal=False)),
            ("alt+x", KeyTranslation(keys=("M-x",), literal=False)),
            ("ctrl+alt+a", KeyTranslation(keys=("C-M-a",), literal=False)),
        ],
    )
    def test_modifier_combinations(self, key: str, expected: KeyTranslation) -> None:
        assert translate_textual_key(key) == expected

    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            ("a", KeyTranslation(keys=("a",), literal=True)),
            ("Z", KeyTranslation(keys=("Z",), literal=True)),
            ("5", KeyTranslation(keys=("5",), literal=True)),
            ("!", KeyTranslation(keys=("!",), literal=True)),
            (" ", KeyTranslation(keys=(" ",), literal=True)),
        ],
    )
    def test_printable_char_literal(self, key: str, expected: KeyTranslation) -> None:
        assert translate_textual_key(key) == expected

    @pytest.mark.parametrize(
        "key",
        ["", "f1", "ctrl+shift+home", "unknown_key", "ctrl+f1"],
    )
    def test_unmapped_returns_none(self, key: str) -> None:
        assert translate_textual_key(key) is None
