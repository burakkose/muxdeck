"""Tests for the dashboard log-preview ANSI / control-byte stripper.

The stripper has to handle the full menagerie of escape sequences
that real shells emit — particularly PowerShell on WSL with
PSReadLine, which was the original motivation for hardening this
code (the previous regex only handled bare CSI sequences and OSC
terminated with BEL, so DEC private modes, ST-terminated OSC,
charset designators, and Fe two-char escapes leaked through).
"""

from __future__ import annotations

import pytest

from copilot_commander.widgets.dashboard import _strip_ansi


class TestCsiSequences:
    def test_simple_sgr_is_removed(self) -> None:
        assert _strip_ansi("\x1b[1;31mred\x1b[0m") == "red"

    def test_dec_private_mode_show_cursor(self) -> None:
        # Pre-fix this leaked through as visible "[?25h" garbage.
        assert _strip_ansi("\x1b[?25hbefore\x1b[?25l") == "before"

    def test_bracketed_paste_mode(self) -> None:
        # PSReadLine emits these around every prompt.
        assert _strip_ansi("\x1b[?2004hcommand\x1b[?2004l") == "command"

    def test_csi_with_intermediate_bytes(self) -> None:
        # ECMA-48 permits intermediate bytes (0x20-0x2F) between
        # parameters and final byte. ``\x1b[1 q`` sets cursor shape.
        assert _strip_ansi("\x1b[1 qafter") == "after"

    def test_cursor_position_query(self) -> None:
        assert _strip_ansi("\x1b[6nreply") == "reply"

    def test_multiple_csi_in_one_string(self) -> None:
        raw = "\x1b[2J\x1b[H\x1b[?25l$ ls\x1b[K\nfile.txt\n"
        assert _strip_ansi(raw) == "$ ls\nfile.txt\n"


class TestOscSequences:
    def test_osc_with_bel_terminator(self) -> None:
        # Window title set via xterm OSC 0, BEL-terminated.
        assert _strip_ansi("\x1b]0;PowerShell\x07prompt$") == "prompt$"

    def test_osc_with_st_terminator(self) -> None:
        # PowerShell more often terminates with ST (ESC \) rather
        # than BEL — the previous regex missed this entirely.
        assert _strip_ansi("\x1b]0;PowerShell\x1b\\prompt$") == "prompt$"

    def test_osc_8_hyperlink_keeps_visible_text(self) -> None:
        # Modern terminals embed clickable links via OSC 8. The
        # opening and closing OSCs must be stripped while the
        # inner text content survives.
        raw = "\x1b]8;;https://example.com\x1b\\click here\x1b]8;;\x1b\\"
        assert _strip_ansi(raw) == "click here"

    def test_osc_single_byte_st(self) -> None:
        assert _strip_ansi("\x1b]2;title\x9cdone") == "done"


class TestTwoCharAndCharsetEscapes:
    def test_keypad_application_mode(self) -> None:
        # ``ESC =`` (DECKPAM) — appeared inline in WSL output.
        assert _strip_ansi("\x1b=after") == "after"

    def test_reverse_index(self) -> None:
        assert _strip_ansi("\x1bMtext") == "text"

    def test_save_restore_cursor(self) -> None:
        assert _strip_ansi("\x1b7middle\x1b8") == "middle"

    def test_charset_designator_g0_us_ascii(self) -> None:
        # ``ESC ( B`` selects US-ASCII as G0; commonly emitted at
        # startup by xterm-compatible shells.
        assert _strip_ansi("\x1b(Btext") == "text"

    def test_charset_designator_g1_dec_special(self) -> None:
        assert _strip_ansi("\x1b)0graphics") == "graphics"


class TestControlByteNoise:
    def test_bom_is_stripped(self) -> None:
        # Windows-originated streams sometimes carry a BOM.
        assert _strip_ansi("\ufefftext") == "text"

    def test_stray_carriage_return_is_dropped(self) -> None:
        # PSReadLine redraws by emitting CR without LF; that left
        # the cursor "weirdly" in the middle of rendered lines.
        assert _strip_ansi("text\rmore") == "textmore"

    def test_crlf_is_preserved(self) -> None:
        # CR followed by LF is a legitimate line break and must
        # survive — splitlines() relies on it downstream.
        assert _strip_ansi("line1\r\nline2") == "line1\r\nline2"

    def test_backspace_is_dropped(self) -> None:
        assert _strip_ansi("ab\x08c") == "abc"

    def test_tab_and_newline_are_preserved(self) -> None:
        assert _strip_ansi("col1\tcol2\nrow2") == "col1\tcol2\nrow2"

    def test_bell_outside_osc_is_dropped(self) -> None:
        assert _strip_ansi("ding\x07dong") == "dingdong"


class TestRealWorldSamples:
    def test_psreadline_prompt_redraw(self) -> None:
        # Approximation of a PSReadLine prompt redraw on WSL: bracketed
        # paste off, set window title (ST-terminated), reset SGR, draw
        # prompt with colors, show cursor.
        raw = (
            "\x1b[?2004l\x1b]0;Administrator: PowerShell\x1b\\"
            "\x1b[0m\x1b[1;36mPS\x1b[0m \x1b[1;33mC:\\Users\\me\x1b[0m> "
            "\x1b[?25h\x1b[?2004h"
        )
        assert _strip_ansi(raw) == "PS C:\\Users\\me> "

    def test_copilot_cli_status_line(self) -> None:
        # Spinner-style updates rewrite the line in place via CR.
        raw = "\rThinking...\r\x1b[KDone."
        assert _strip_ansi(raw) == "Thinking...Done."

    def test_plain_text_unchanged(self) -> None:
        # Sanity check: pure text passes through untouched.
        msg = "All good — nothing to strip here."
        assert _strip_ansi(msg) == msg


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ""),
        # Truncated CSI (no final byte) — the leading ESC is stripped
        # as control noise and the bracket survives. Cleanup over
        # preservation: a half-finished sequence in a log preview is
        # garbage that the user shouldn't see.
        ("\x1b[", "["),
        ("\x1b]0;unterminated", "]0;unterminated"),
    ],
)
def test_malformed_sequences_are_cleaned_up(raw: str, expected: str) -> None:
    # The stripper must never invent characters or hang on a
    # malformed sequence. Stray ESC bytes that survive every ANSI
    # pattern are dropped by the C0 control-noise pass — keeping
    # them would visibly corrupt the log preview.
    assert _strip_ansi(raw) == expected
