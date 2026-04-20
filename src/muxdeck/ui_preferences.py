from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from textual._context import NoActiveAppError
from textual.dom import DOMNode


class UiDensity(StrEnum):
    COMPACT = "compact"
    COMFORTABLE = "comfortable"


class UiGlyphs(StrEnum):
    RICH = "rich"
    ASCII = "ascii"


class UiContrast(StrEnum):
    STANDARD = "standard"
    HIGH = "high"


class UiDecorations(StrEnum):
    FULL = "full"
    REDUCED = "reduced"


@dataclass(frozen=True, slots=True)
class UiPreferences:
    density: UiDensity = UiDensity.COMPACT
    glyphs: UiGlyphs = UiGlyphs.RICH
    contrast: UiContrast = UiContrast.STANDARD
    decorations: UiDecorations = UiDecorations.FULL
    wrap_logs: bool = False

    def css_classes(self) -> tuple[str, ...]:
        wrap_class = "ux-wrap-logs" if self.wrap_logs else "ux-nowrap-logs"
        return (
            f"ux-density-{self.density.value}",
            f"ux-glyphs-{self.glyphs.value}",
            f"ux-contrast-{self.contrast.value}",
            f"ux-decor-{self.decorations.value}",
            wrap_class,
        )

    def mode_badges(self) -> tuple[str, ...]:
        badges: list[str] = []
        if self.density is UiDensity.COMFORTABLE:
            badges.append("comfy")
        if self.glyphs is UiGlyphs.ASCII:
            badges.append("ascii")
        if self.contrast is UiContrast.HIGH:
            badges.append("high")
        if self.decorations is UiDecorations.REDUCED:
            badges.append("plain")
        if self.wrap_logs:
            badges.append("wrap")
        return tuple(badges)

    @property
    def is_default(self) -> bool:
        return self == UiPreferences()


def resolve_ui_preferences(node: DOMNode | None) -> UiPreferences:
    if node is None:
        return UiPreferences()
    try:
        app = node.app
    except NoActiveAppError:
        return UiPreferences()
    preferences = getattr(app, "ui_preferences", None)
    if isinstance(preferences, UiPreferences):
        return preferences
    return UiPreferences()


__all__ = [
    "UiContrast",
    "UiDecorations",
    "UiDensity",
    "UiGlyphs",
    "UiPreferences",
    "resolve_ui_preferences",
]
