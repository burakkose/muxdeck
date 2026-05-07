"""Tests for the common widget helpers and the TabBar / KeyHintFooter.

Focuses on coverage gaps: ``TabBar`` mode-badges + per-tab variants,
``KeyHintFooter`` busy + focus_label + mode_badges, and the small
formatting helpers (``format_timestamp``, ``format_short_timestamp``,
``format_bool``, ``join_lines``, ``status_glyph_*``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

import pytest
from textual.app import App, ComposeResult

from muxdeck.bindings import KeyHint
from muxdeck.domain.enums import AgentStatus
from muxdeck.ui_preferences import (
    UiContrast,
    UiDecorations,
    UiDensity,
    UiGlyphs,
    UiPreferences,
)
from muxdeck.widgets.common import (
    KeyHintFooter,
    TabBar,
    format_bool,
    format_short_timestamp,
    format_timestamp,
    item_separator,
    join_lines,
    pipe_separator,
    status_glyph,
    status_glyph_char,
    status_glyph_parts,
    ui_symbol,
)


class _Renderable(Protocol):
    def render(self) -> object: ...


def _render(widget: _Renderable) -> str:
    renderable = widget.render()
    plain = getattr(renderable, "plain", None)
    return plain if isinstance(plain, str) else str(renderable)


# ── format_* ────────────────────────────────────────────────────────


def test_format_timestamp_returns_dash_when_none() -> None:
    assert format_timestamp(None) == "-"


def test_format_timestamp_uses_utc_zulu_format() -> None:
    ts = datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert format_timestamp(ts) == "2025-01-02 03:04:05Z"


def test_format_short_timestamp_dash_when_none_else_hms() -> None:
    assert format_short_timestamp(None) == "-"
    ts = datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert format_short_timestamp(ts) == "03:04:05"


def test_format_bool_returns_yes_or_no() -> None:
    assert format_bool(True) == "yes"
    assert format_bool(False) == "no"


def test_join_lines_handles_empty_and_populated() -> None:
    assert join_lines(()) == "-"
    assert join_lines(("a", "b", "c")) == "a\nb\nc"


# ── ui_symbol / pipe_separator / item_separator ─────────────────────


def test_ui_symbol_default_uses_rich_glyphs() -> None:
    assert ui_symbol("brand") == "◆"


def test_ui_symbol_ascii_glyphs() -> None:
    prefs = UiPreferences(glyphs=UiGlyphs.ASCII)
    assert ui_symbol("brand", preferences=prefs) == "*"
    assert ui_symbol("separator", preferences=prefs) == "|"


def test_ui_symbol_reduced_decoration_overrides_rich_brand() -> None:
    prefs = UiPreferences(decorations=UiDecorations.REDUCED)
    assert ui_symbol("brand", preferences=prefs) == ""
    assert ui_symbol("section-fill", preferences=prefs) == ""
    # Non-overridden symbols keep their decoration set
    assert ui_symbol("annotation", preferences=prefs) == "✎"


def test_pipe_and_item_separator_use_active_glyphs() -> None:
    prefs = UiPreferences(glyphs=UiGlyphs.ASCII)
    assert pipe_separator(prefs) == " | "
    assert item_separator(prefs) == " / "


# ── status_glyph helpers ────────────────────────────────────────────


def test_status_glyph_char_defaults_for_each_status() -> None:
    for status in AgentStatus:
        char = status_glyph_char(status)
        assert isinstance(char, str)
        assert char


def test_status_glyph_returns_text_with_style() -> None:
    text = status_glyph(AgentStatus.RUNNING)
    assert text.plain
    text_selected = status_glyph(AgentStatus.RUNNING, selected=True)
    # Selected state forces a different style string.
    assert text_selected.plain == text.plain


def test_status_glyph_parts_returns_tuple_of_char_and_color() -> None:
    char, color = status_glyph_parts(AgentStatus.RUNNING)
    assert isinstance(char, str)
    assert char
    assert isinstance(color, str)
    assert color


def test_status_glyph_handles_ascii_glyph_set() -> None:
    prefs = UiPreferences(glyphs=UiGlyphs.ASCII)
    char, _ = status_glyph_parts(AgentStatus.RUNNING, preferences=prefs)
    assert char == "o"


# ── TabBar ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tab_bar_renders_brand_active_tab_and_badges() -> None:
    bar = TabBar(active="dashboard", badges={"attention": 3}, widget_id="tabs")

    class _Harness(App[None]):
        def compose(self) -> ComposeResult:
            yield bar

    async with _Harness().run_test() as pilot:
        await pilot.pause()
        rendered = _render(bar)
        assert "muxdeck" in rendered
        assert "dashboard" in rendered
        assert "attention" in rendered
        # Badge glyph + count
        assert "3" in rendered
        bar.set_badges({"attention": 5, "replay": 1})
        await pilot.pause()
        assert "5" in _render(bar)


@pytest.mark.asyncio
async def test_tab_bar_renders_mode_badges_when_preferences_non_default() -> None:
    bar = TabBar(active="replay", widget_id="tabs")

    class _Harness(App[None]):
        def compose(self) -> ComposeResult:
            yield bar

        def on_mount(self) -> None:
            self.ui_preferences = UiPreferences(  # type: ignore[attr-defined]
                density=UiDensity.COMFORTABLE,
                glyphs=UiGlyphs.ASCII,
                contrast=UiContrast.HIGH,
                decorations=UiDecorations.REDUCED,
                wrap_logs=True,
            )

    async with _Harness().run_test() as pilot:
        await pilot.pause()
        rendered = _render(bar)
        assert "modes" in rendered
        # Badges enumerate density/glyph/contrast/decorations/wrap_logs
        assert "comfy" in rendered
        assert "ascii" in rendered
        assert "high" in rendered
        assert "plain" in rendered
        assert "wrap" in rendered


# ── KeyHintFooter ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_key_hint_footer_busy_state_and_focus_label() -> None:
    footer = KeyHintFooter(
        hints=(KeyHint("r", "refresh"),),
        status="ready",
        widget_id="footer",
    )

    class _Harness(App[None]):
        def compose(self) -> ComposeResult:
            yield footer

    async with _Harness().run_test() as pilot:
        await pilot.pause()
        footer.busy = True
        footer.focus_label = "agent list"
        await pilot.pause()
        rendered = _render(footer)
        assert "working" in rendered
        assert "focus" in rendered
        assert "agent list" in rendered
        assert "refresh" in rendered


@pytest.mark.asyncio
async def test_key_hint_footer_renders_action_and_non_action_hints() -> None:
    footer = KeyHintFooter(
        hints=(
            KeyHint("ctrl+p", "commands"),
            KeyHint("R", "resume"),
            KeyHint("?", "help"),
        ),
        status="ready",
        widget_id="footer",
    )

    class _Harness(App[None]):
        def compose(self) -> ComposeResult:
            yield footer

    async with _Harness().run_test() as pilot:
        await pilot.pause()
        rendered = _render(footer)
        # action keys (ctrl+p, R) and non-action keys (?) are both
        # rendered. Their styling differs but the text is present.
        assert "commands" in rendered
        assert "resume" in rendered
        assert "help" in rendered


@pytest.mark.asyncio
async def test_key_hint_footer_renders_mode_badges() -> None:
    footer = KeyHintFooter(
        hints=(KeyHint("r", "refresh"),),
        widget_id="footer",
    )

    class _Harness(App[None]):
        def compose(self) -> ComposeResult:
            yield footer

        def on_mount(self) -> None:
            self.ui_preferences = UiPreferences(  # type: ignore[attr-defined]
                glyphs=UiGlyphs.ASCII, wrap_logs=True
            )

    async with _Harness().run_test() as pilot:
        await pilot.pause()
        rendered = _render(footer)
        assert "modes" in rendered
        assert "ascii" in rendered
        assert "wrap" in rendered
