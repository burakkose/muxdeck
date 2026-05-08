# ruff: noqa: ANN001,ANN201

from __future__ import annotations

from muxdeck import theme


def test_graphite_backgrounds_are_hex_strings() -> None:
    for name in ("BG_HARD", "BG", "BG1", "BG2", "BG3", "BG4"):
        value = getattr(theme, name)
        assert isinstance(value, str), f"{name} is not str"
        assert value.startswith("#"), f"{name}={value!r} missing # prefix"
        assert len(value) == 7, f"{name}={value!r} not #RRGGBB"


def test_graphite_foregrounds_are_hex_strings() -> None:
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
        # AQUA is intentionally an alias for BLUE in the graphite
        # palette — accept that pair without flagging as a duplicate.
        if name == "AQUA":
            continue
        assert bright != dim, f"{name} bright==dim"


def test_graphite_core_palette_matches_expected() -> None:
    """Pin the canonical graphite hex values so an accidental revert
    to brighter Ayu-style accents fails CI immediately."""
    assert {
        "BG_HARD": theme.BG_HARD,
        "BG": theme.BG,
        "BG1": theme.BG1,
        "BG2": theme.BG2,
        "BG3": theme.BG3,
        "FG": theme.FG,
        "FG2": theme.FG2,
        "FG3": theme.FG3,
        "FG4": theme.FG4,
        "RED": theme.RED,
        "GREEN": theme.GREEN,
        "YELLOW": theme.YELLOW,
        "BLUE": theme.BLUE,
        "PURPLE": theme.PURPLE,
        "ORANGE": theme.ORANGE,
        "BORDER_FOCUS": theme.BORDER_FOCUS,
        "SELECTED_ROW_BG": theme.SELECTED_ROW_BG,
    } == {
        "BG_HARD": "#0B0D10",
        "BG": "#12151B",
        "BG1": "#191D25",
        "BG2": "#1D2230",
        "BG3": "#233044",
        "FG": "#F2F4F8",
        "FG2": "#A7AFBD",
        "FG3": "#6F7887",
        "FG4": "#4D5563",
        "RED": "#FF453A",
        "GREEN": "#32D74B",
        "YELLOW": "#FFD60A",
        "BLUE": "#5AC8FA",
        "PURPLE": "#BF5AF2",
        "ORANGE": "#FF9F0A",
        "BORDER_FOCUS": "#5AC8FA",
        "SELECTED_ROW_BG": "#233044",
    }


def test_aqua_is_an_alias_for_blue() -> None:
    """In the graphite palette navigation/focus collapses to a single
    accent (BLUE). AQUA is kept as an alias so existing call-sites
    keep working without a mass rename, but it must point at BLUE."""
    assert theme.AQUA == theme.BLUE
    assert theme.AQUA_DIM == theme.BLUE_DIM


def test_completed_and_dead_status_use_neutral_gray() -> None:
    """Historical/inactive statuses must NOT use a coloured accent —
    they are gray so the eye reserves colour for live state."""
    assert theme.STATUS_COMPLETED == theme.FG3
    assert theme.STATUS_DEAD == theme.FG3
    assert theme.STATUS_DISCOVERED == theme.FG3
    assert theme.STATUS_UNKNOWN == theme.FG3


def test_running_uses_green_only() -> None:
    """Green is the single accent that means 'healthy / running'."""
    assert theme.STATUS_RUNNING == theme.GREEN


def test_review_and_blocked_use_review_orange() -> None:
    """Orange means 'human action required' — review and blocked
    both fall in that bucket."""
    assert theme.STATUS_WAITING_INPUT == theme.ORANGE
    assert theme.STATUS_BLOCKED == theme.ORANGE


def test_error_uses_red_only() -> None:
    """Red is reserved for failure — keep IDLE off red so 'stale'
    does not visually compete with 'crashed'."""
    assert theme.STATUS_ERROR == theme.RED
    assert theme.STATUS_IDLE != theme.RED


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
