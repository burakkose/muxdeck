# ruff: noqa: ANN001,ANN201

from __future__ import annotations

from muxdeck import theme


def test_ayu_backgrounds_are_hex_strings() -> None:
    for name in ("BG_HARD", "BG", "BG1", "BG2", "BG3", "BG4"):
        value = getattr(theme, name)
        assert isinstance(value, str), f"{name} is not str"
        assert value.startswith("#"), f"{name}={value!r} missing # prefix"
        assert len(value) == 7, f"{name}={value!r} not #RRGGBB"


def test_ayu_foregrounds_are_hex_strings() -> None:
    for name in ("FG", "FG1", "FG2", "FG3", "FG4"):
        value = getattr(theme, name)
        assert isinstance(value, str), f"{name} is not str"
        assert value.startswith("#"), f"{name}={value!r} missing # prefix"
        assert len(value) == 7, f"{name}={value!r} not #RRGGBB"


def test_accent_palette_complete() -> None:
    accents = ("RED", "GREEN", "YELLOW", "BLUE", "PURPLE", "AQUA", "ORANGE")
    for name in accents:
        bright = getattr(theme, name)
        dim = getattr(theme, f"{name}_DIM")
        assert bright.startswith("#"), f"{name} not hex"
        assert dim.startswith("#"), f"{name}_DIM not hex"
        assert bright != dim, f"{name} bright==dim"


def test_ayu_mirage_core_palette_matches_expected() -> None:
    assert {
        "BG_HARD": theme.BG_HARD,
        "BG": theme.BG,
        "BG1": theme.BG1,
        "BG2": theme.BG2,
        "FG": theme.FG,
        "RED": theme.RED,
        "GREEN": theme.GREEN,
        "YELLOW": theme.YELLOW,
        "BLUE": theme.BLUE,
        "PURPLE": theme.PURPLE,
        "AQUA": theme.AQUA,
        "ORANGE": theme.ORANGE,
        "BORDER_FOCUS": theme.BORDER_FOCUS,
        "BADGE_BG": theme.BADGE_BG,
        "BADGE_FG": theme.BADGE_FG,
    } == {
        "BG_HARD": "#181C26",
        "BG": "#1F2430",
        "BG1": "#242936",
        "BG2": "#282E3B",
        "FG": "#CCCAC2",
        "RED": "#FF6666",
        "GREEN": "#D5FF80",
        "YELLOW": "#FFCD66",
        "BLUE": "#73D0FF",
        "PURPLE": "#DFBFFF",
        "AQUA": "#95E6CB",
        "ORANGE": "#FFA659",
        "BORDER_FOCUS": "#FFCC66",
        "BADGE_BG": "#FFCC66",
        "BADGE_FG": "#735923",
    }


def test_status_constants_cover_all_statuses() -> None:
    from muxdeck.domain.enums import AgentStatus

    expected = {
        AgentStatus.RUNNING: theme.STATUS_RUNNING,
        AgentStatus.IDLE: theme.STATUS_IDLE,
        AgentStatus.WAITING_INPUT: theme.STATUS_WAITING_INPUT,
        AgentStatus.BLOCKED: theme.STATUS_BLOCKED,
        AgentStatus.ERROR: theme.STATUS_ERROR,
        AgentStatus.DEAD: theme.STATUS_DEAD,
        AgentStatus.COMPLETED: theme.STATUS_COMPLETED,
        AgentStatus.DISCOVERED: theme.STATUS_DISCOVERED,
        AgentStatus.STARTING: theme.STATUS_STARTING,
        AgentStatus.UNKNOWN: theme.STATUS_UNKNOWN,
    }
    for status, color in expected.items():
        assert color.startswith("#"), f"STATUS for {status.value} is not hex"


def test_severity_constants_exist() -> None:
    for name in ("SEVERITY_INFO", "SEVERITY_WARNING", "SEVERITY_ERROR"):
        value = getattr(theme, name)
        assert value.startswith("#"), f"{name} not hex"


def test_health_tone_constants_exist() -> None:
    for tone in ("HEALTHY", "WARNING", "CRITICAL"):
        bg = getattr(theme, f"TONE_{tone}_BG")
        fg = getattr(theme, f"TONE_{tone}_FG")
        assert bg.startswith("#"), f"TONE_{tone}_BG not hex"
        assert fg.startswith("#"), f"TONE_{tone}_FG not hex"


def test_ui_chrome_constants_exist() -> None:
    for name in (
        "BORDER",
        "BORDER_FOCUS",
        "PANEL_BG",
        "PANEL_TITLE",
        "HEADER_BG",
        "FOOTER_BG",
        "BADGE_BG",
        "BADGE_FG",
        "SELECTED_ROW_BG",
        "ATTENTION_ROW_BG",
        "SCROLLBAR_BG",
        "SCROLLBAR_FG",
    ):
        value = getattr(theme, name)
        assert isinstance(value, str), f"{name} not str"
        assert value.startswith("#"), f"{name} not hex"


def test_all_exports_match_module_contents() -> None:
    exported = set(theme.__all__)
    module_constants = {name for name in dir(theme) if name.isupper() and not name.startswith("_")}
    # __all__ should cover every public constant
    assert module_constants <= exported, f"Missing from __all__: {module_constants - exported}"
