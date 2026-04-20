from __future__ import annotations

from textual.widgets import Static

from muxdeck.domain.enums import AgentStatus
from muxdeck.ui_preferences import (
    UiContrast,
    UiDecorations,
    UiDensity,
    UiGlyphs,
    UiPreferences,
    resolve_ui_preferences,
)
from muxdeck.widgets.common import (
    item_separator,
    pipe_separator,
    status_glyph_char,
    ui_symbol,
)


def test_ui_preferences_emit_css_classes_and_mode_badges() -> None:
    preferences = UiPreferences(
        density=UiDensity.COMFORTABLE,
        glyphs=UiGlyphs.ASCII,
        contrast=UiContrast.HIGH,
        decorations=UiDecorations.REDUCED,
        wrap_logs=True,
    )

    assert preferences.css_classes() == (
        "ux-density-comfortable",
        "ux-glyphs-ascii",
        "ux-contrast-high",
        "ux-decor-reduced",
        "ux-wrap-logs",
    )
    assert preferences.mode_badges() == ("comfy", "ascii", "high", "plain", "wrap")
    assert preferences.is_default is False


def test_resolve_ui_preferences_falls_back_to_defaults_without_app() -> None:
    assert resolve_ui_preferences(None) == UiPreferences()
    assert resolve_ui_preferences(Static()) == UiPreferences()


def test_symbol_helpers_respect_ascii_and_reduced_modes() -> None:
    preferences = UiPreferences(
        glyphs=UiGlyphs.ASCII,
        decorations=UiDecorations.REDUCED,
    )

    assert ui_symbol("brand", preferences=preferences) == ""
    assert ui_symbol("separator", preferences=preferences) == "|"
    assert pipe_separator(preferences) == " | "
    assert item_separator(preferences) == " / "
    assert status_glyph_char(AgentStatus.RUNNING, preferences=preferences) == "o"
