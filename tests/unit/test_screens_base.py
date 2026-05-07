# ruff: noqa: I001, PT009, PT027, F401, PT018

from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

from textual.app import App, ComposeResult
from textual.widgets import Static

from muxdeck.bindings import KeyHint
from muxdeck.screens.base import ShellScreen
from muxdeck.widgets.common import KeyHintFooter


class _ScreenLike(ShellScreen):
    SCREEN_TITLE = "BASE"
    FOOTER_HINTS = (KeyHint("a", "act"),)

    def compose_body(self) -> ComposeResult:
        yield Static("body content", id="body-static")


class _Harness(App[None]):
    """Minimal Textual app harness to mount a ShellScreen subclass."""

    def __init__(self) -> None:
        super().__init__()
        self.runtime = SimpleNamespace()  # type: ignore[assignment]
        self.tab_badges = None

    def on_mount(self) -> None:
        self.push_screen(_ScreenLike(cast(object, self.runtime)))  # type: ignore[arg-type]


async def test_set_status_propagates_to_footer_when_mounted() -> None:
    app = _Harness()
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = next(s for s in app.screen_stack if isinstance(s, _ScreenLike))
        screen.set_status("running things")
        footer = screen.query_one(KeyHintFooter)
        assert footer.status == "running things"


async def test_set_hints_replaces_footer_hints_when_mounted() -> None:
    app = _Harness()
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = next(s for s in app.screen_stack if isinstance(s, _ScreenLike))
        screen.set_hints((KeyHint("z", "zen"),))
        footer = screen.query_one(KeyHintFooter)
        assert tuple((h.key, h.label) for h in footer.hints) == (("z", "zen"),)


async def test_set_status_pre_mount_only_updates_attribute() -> None:
    """Calling set_status before mount must not raise."""
    screen = _ScreenLike.__new__(_ScreenLike)
    screen._status = "ready"
    screen._active_workers = 0
    screen._is_mounted = False
    # is_mounted is False by default — no footer mutation
    screen.set_status("queued")
    assert screen._status == "queued"


async def test_set_hints_pre_mount_is_noop() -> None:
    screen = _ScreenLike.__new__(_ScreenLike)
    screen._status = "ready"
    screen._active_workers = 0
    screen._is_mounted = False
    screen.set_hints([KeyHint("x", "exit")])  # no exception


async def test_apply_ui_preferences_default_returns_false() -> None:
    screen = _ScreenLike.__new__(_ScreenLike)
    assert screen.apply_ui_preferences() is False


async def test_render_widget_text_uses_plain_when_available() -> None:
    class _R:
        plain = "hello"

    class _W:
        def render(self) -> object:
            return _R()

    text = ShellScreen._render_widget_text(cast(object, _W()))  # type: ignore[arg-type]
    assert text == "hello"


async def test_render_widget_text_falls_back_to_inner_renderable_plain() -> None:
    class _Inner:
        plain = "inner"

    class _Outer:
        plain = None
        _renderable = _Inner()

    class _W:
        def render(self) -> object:
            return _Outer()

    text = ShellScreen._render_widget_text(cast(object, _W()))  # type: ignore[arg-type]
    assert text == "inner"


async def test_render_widget_text_falls_back_to_str_inner_renderable() -> None:
    class _Inner:
        def __str__(self) -> str:
            return "str-inner"

    class _Outer:
        plain = None
        _renderable = _Inner()

    class _W:
        def render(self) -> object:
            return _Outer()

    text = ShellScreen._render_widget_text(cast(object, _W()))  # type: ignore[arg-type]
    assert text == "str-inner"


async def test_render_widget_text_falls_back_to_str_renderable() -> None:
    class _R:
        plain = None
        _renderable = None

        def __str__(self) -> str:
            return "raw"

    class _W:
        def render(self) -> object:
            return _R()

    text = ShellScreen._render_widget_text(cast(object, _W()))  # type: ignore[arg-type]
    assert text == "raw"


async def test_copy_rendered_text_no_widget_text_sets_status_no_data() -> None:
    """When no rendered text, just sets status — does not touch clipboard."""

    captured: dict[str, str] = {}

    screen = _ScreenLike.__new__(_ScreenLike)
    screen._status = "ready"
    screen._active_workers = 0

    def fake_set_status(msg: str) -> None:
        captured["status"] = msg

    screen.set_status = fake_set_status  # type: ignore[method-assign,assignment]

    class _EmptyWidget:
        def render(self) -> object:
            class _R:
                plain = ""

            return _R()

    # Use private attribute access for the test app reference -- not needed
    # because copy_to_clipboard is never reached.
    screen.copy_rendered_text("logs", cast(object, _EmptyWidget()))  # type: ignore[arg-type]
    assert captured["status"] == "no logs available"


async def test_copy_rendered_text_copies_to_clipboard_and_sets_status() -> None:
    captured: dict[str, str] = {}
    clipboard: list[str] = []

    screen = _ScreenLike.__new__(_ScreenLike)
    screen._status = "ready"
    screen._active_workers = 0

    def fake_set_status(msg: str) -> None:
        captured["status"] = msg

    screen.set_status = fake_set_status  # type: ignore[method-assign,assignment]

    fake_app = SimpleNamespace(copy_to_clipboard=clipboard.append)
    type(screen).app = property(lambda _: fake_app)  # type: ignore[assignment]
    try:

        class _Widget:
            def render(self) -> object:
                class _R:
                    plain = "captured text"

                return _R()

        screen.copy_rendered_text("snapshot", cast(object, _Widget()))  # type: ignore[arg-type]
        assert clipboard == ["captured text"]
        assert captured["status"] == "copied snapshot to clipboard"
    finally:
        del type(screen).app


async def test_describe_focus_known_widget_id_uses_explicit_label() -> None:
    screen = _ScreenLike.__new__(_ScreenLike)
    widget = SimpleNamespace(id="dashboard-agents", border_title=None, __class__=type("X", (), {}))
    label = ShellScreen._describe_focus(screen, cast(object, widget))  # type: ignore[arg-type]
    assert label == "agent list"


async def test_describe_focus_filter_input_returns_prefix_filter() -> None:
    screen = _ScreenLike.__new__(_ScreenLike)
    widget = SimpleNamespace(
        id="my-tasks-filter-input", border_title=None, __class__=type("X", (), {})
    )
    label = ShellScreen._describe_focus(screen, cast(object, widget))  # type: ignore[arg-type]
    assert "filter" in label

    help_widget = SimpleNamespace(
        id="help-filter-input", border_title=None, __class__=type("X", (), {})
    )
    label2 = ShellScreen._describe_focus(screen, cast(object, help_widget))  # type: ignore[arg-type]
    assert label2 == "help search"


async def test_describe_focus_uses_border_title_when_set() -> None:
    screen = _ScreenLike.__new__(_ScreenLike)
    widget = SimpleNamespace(id="", border_title="Sessions Panel", __class__=type("X", (), {}))
    label = ShellScreen._describe_focus(screen, cast(object, widget))  # type: ignore[arg-type]
    assert label == "sessions panel"


async def test_describe_focus_widget_id_strips_layout_words() -> None:
    screen = _ScreenLike.__new__(_ScreenLike)
    widget = SimpleNamespace(id="agent-list-row", border_title=None, __class__=type("X", (), {}))
    label = ShellScreen._describe_focus(screen, cast(object, widget))  # type: ignore[arg-type]
    assert label == "agent"


async def test_describe_focus_falls_back_to_class_name() -> None:
    screen = _ScreenLike.__new__(_ScreenLike)

    class MyShinyWidget:
        id = None
        border_title = None

    label = ShellScreen._describe_focus(screen, cast(object, MyShinyWidget()))  # type: ignore[arg-type]
    # Class-based fallback splits CamelCase into spaces
    assert "shiny" in label and "my" in label


async def test_begin_loading_and_end_loading_swallow_errors() -> None:
    screen = _ScreenLike.__new__(_ScreenLike)
    screen._status = "ready"
    screen._active_workers = 0

    class _RaisingWidget:
        @property
        def loading(self) -> bool:
            return False

        @loading.setter
        def loading(self, value: bool) -> None:
            raise RuntimeError("boom")

    # Must not raise even if assignment raises
    screen.begin_loading(_RaisingWidget(), _RaisingWidget())
    screen.end_loading(_RaisingWidget())


async def test_refresh_data_default_returns_none() -> None:
    screen = _ScreenLike.__new__(_ScreenLike)
    # ShellScreen.refresh_data returns None — call it to drive the default branch
    screen.refresh_data()


async def test_sync_busy_indicator_pre_mount_is_noop() -> None:
    screen = _ScreenLike.__new__(_ScreenLike)
    screen._status = "ready"
    screen._active_workers = 0
    screen._is_mounted = False
    # is_mounted False — early return; no exception
    screen._sync_busy_indicator()


async def test_on_descendant_focus_pre_mount_is_noop() -> None:
    screen = _ScreenLike.__new__(_ScreenLike)
    screen._status = "ready"
    screen._active_workers = 0
    screen._is_mounted = False
    event = SimpleNamespace(widget=SimpleNamespace(id="x"))
    # is_mounted False — early return
    screen.on_descendant_focus(cast(object, event))  # type: ignore[arg-type]
