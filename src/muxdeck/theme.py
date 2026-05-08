"""Graphite palette for the Muxdeck TUI.

A calm, premium dark-mode palette in the spirit of macOS dark mode and
high-end terminal apps. The goal is a mostly-graphite UI where colour
is rare and always carries meaning (state, focus, severity).

Design rules:

* Backgrounds are layered graphite tones (BG_HARD < BG < BG1 < BG2/3).
  Selection and focus are expressed as a *raised surface*, not a loud
  coloured glyph.
* Foregrounds use a 5-step grayscale ramp from primary text to faint
  metadata so hierarchy works without colour.
* Accents (BLUE/GREEN/YELLOW/ORANGE/RED/PURPLE) are reserved for
  semantic state. Don't use them for navigation labels, repo names,
  shortcut keys, or other decoration.
* Reserved meanings:

      BLUE   navigation, focus, selected tab, primary action
      GREEN  healthy / running / active
      YELLOW stale / warning
      ORANGE review / human action required
      RED    failure / destructive only
      GRAY   metadata, completed, inactive, historical

* The ``*_DIM`` variants are slightly desaturated for use on raised
  surfaces (selected rows) where the bright accent would feel loud.

The legacy Ayu palette names (BG, FG, ORANGE, etc.) are kept so
existing widget call-sites do not need a mass rename — they now point
at graphite values. Tests assert the new hex values explicitly so an
accidental revert to brighter accents fails CI.
"""

from __future__ import annotations

from typing import Final

# ── backgrounds (graphite stack, dark to light) ─────────────────────
# BG_HARD is the base canvas; BG is the panel surface; BG1 is a raised
# surface (cards, primary panels); BG2/BG3 are the selected/focused
# surface tones used for soft full-row highlights.
BG_HARD: Final[str] = "#0B0D10"
BG: Final[str] = "#12151B"
BG1: Final[str] = "#191D25"
BG2: Final[str] = "#1D2230"
BG3: Final[str] = "#233044"
BG4: Final[str] = "#2C3A52"

# ── foregrounds (grayscale hierarchy) ───────────────────────────────
# FG  primary text (titles, important values)
# FG1 secondary text (paragraph body)
# FG2 muted (metadata values)
# FG3 muted label (field labels, footer keys)
# FG4 disabled / faint (placeholders, completed rows)
FG: Final[str] = "#F2F4F8"
FG1: Final[str] = "#D6DAE2"
FG2: Final[str] = "#A7AFBD"
FG3: Final[str] = "#6F7887"
FG4: Final[str] = "#4D5563"

# ── accents (used sparingly) ────────────────────────────────────────
RED: Final[str] = "#FF453A"
GREEN: Final[str] = "#32D74B"
YELLOW: Final[str] = "#FFD60A"
BLUE: Final[str] = "#5AC8FA"
PURPLE: Final[str] = "#BF5AF2"
# AQUA used to be a separate hue in the Ayu palette; in the graphite
# palette navigation/focus collapses to BLUE so they read as one
# semantic role. AQUA is kept as an alias to ease migration.
AQUA: Final[str] = BLUE
ORANGE: Final[str] = "#FF9F0A"

# ── desaturated accents (for raised / selected surfaces) ────────────
# Slightly less saturated so they don't dominate the soft selection
# background. Use these inside selected rows or on raised surfaces.
RED_DIM: Final[str] = "#E04A45"
GREEN_DIM: Final[str] = "#3FBE5A"
YELLOW_DIM: Final[str] = "#E6C233"
BLUE_DIM: Final[str] = "#6BB8E0"
PURPLE_DIM: Final[str] = "#A672D6"
AQUA_DIM: Final[str] = BLUE_DIM
ORANGE_DIM: Final[str] = "#E08C2A"

# ── semantic: status badges ──────────────────────────────────────────
# Each status maps to its semantic accent. Completed / dead / unknown
# all sit in the gray family because they are historical / inactive.
STATUS_RUNNING: Final[str] = GREEN
STATUS_IDLE: Final[str] = YELLOW
STATUS_WAITING_INPUT: Final[str] = ORANGE
STATUS_BLOCKED: Final[str] = ORANGE
STATUS_ERROR: Final[str] = RED
STATUS_DEAD: Final[str] = FG3
STATUS_COMPLETED: Final[str] = FG3
STATUS_DISCOVERED: Final[str] = FG3
STATUS_STARTING: Final[str] = BLUE
STATUS_UNKNOWN: Final[str] = FG3

# ── semantic: severity badges ────────────────────────────────────────
SEVERITY_INFO: Final[str] = BLUE
SEVERITY_WARNING: Final[str] = YELLOW
SEVERITY_ERROR: Final[str] = RED

# ── semantic: alert tones (very dark tints, used as panel backdrops) ─
# Foreground accents stay vivid so the badge still reads at a glance.
TONE_HEALTHY_BG: Final[str] = "#0F1B14"
TONE_HEALTHY_FG: Final[str] = GREEN
TONE_WARNING_BG: Final[str] = "#1F1A0E"
TONE_WARNING_FG: Final[str] = YELLOW
TONE_CRITICAL_BG: Final[str] = "#1F0F12"
TONE_CRITICAL_FG: Final[str] = RED

# ── semantic: UI chrome ──────────────────────────────────────────────
# Borders are very faint so panels read as separated by whitespace
# rather than visible rules. BORDER_FOCUS uses the accent BLUE so a
# focused surface has one clear cue without shouting.
BORDER: Final[str] = "#191D25"
BORDER_FOCUS: Final[str] = BLUE
PANEL_BG: Final[str] = BG
PANEL_TITLE: Final[str] = FG2
HEADER_BG: Final[str] = BG_HARD
FOOTER_BG: Final[str] = BG_HARD
# Badges sit on a raised graphite surface, not a coloured pill — the
# colour comes from the badge text or a status dot, not the chip
# background. This keeps the UI calm even when many badges are on
# screen.
BADGE_BG: Final[str] = BG2
BADGE_FG: Final[str] = FG
# Selected rows use the dedicated raised surface BG3. The text inside
# stays primary/secondary gray; only the status dot carries colour so
# the row reads as "raised", not "highlighted in coloured ink".
SELECTED_ROW_BG: Final[str] = BG3
# Attention rows reuse BG3 (no warm tint) so attention items still
# match the rest of the surface system; the colour cue lives in the
# status dot/label rather than a coloured row.
ATTENTION_ROW_BG: Final[str] = BG3
SCROLLBAR_BG: Final[str] = BG
SCROLLBAR_FG: Final[str] = BG3

__all__ = [
    "AQUA",
    "AQUA_DIM",
    "ATTENTION_ROW_BG",
    "BADGE_BG",
    "BADGE_FG",
    "BG",
    "BG1",
    "BG2",
    "BG3",
    "BG4",
    "BG_HARD",
    "BLUE",
    "BLUE_DIM",
    "BORDER",
    "BORDER_FOCUS",
    "FG",
    "FG1",
    "FG2",
    "FG3",
    "FG4",
    "FOOTER_BG",
    "GREEN",
    "GREEN_DIM",
    "HEADER_BG",
    "ORANGE",
    "ORANGE_DIM",
    "PANEL_BG",
    "PANEL_TITLE",
    "PURPLE",
    "PURPLE_DIM",
    "RED",
    "RED_DIM",
    "SCROLLBAR_BG",
    "SCROLLBAR_FG",
    "SELECTED_ROW_BG",
    "SEVERITY_ERROR",
    "SEVERITY_INFO",
    "SEVERITY_WARNING",
    "STATUS_BLOCKED",
    "STATUS_COMPLETED",
    "STATUS_DEAD",
    "STATUS_DISCOVERED",
    "STATUS_ERROR",
    "STATUS_IDLE",
    "STATUS_RUNNING",
    "STATUS_STARTING",
    "STATUS_UNKNOWN",
    "STATUS_WAITING_INPUT",
    "TONE_CRITICAL_BG",
    "TONE_CRITICAL_FG",
    "TONE_HEALTHY_BG",
    "TONE_HEALTHY_FG",
    "TONE_WARNING_BG",
    "TONE_WARNING_FG",
    "YELLOW",
    "YELLOW_DIM",
]
