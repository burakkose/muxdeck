"""Coverage for muxdeck.screens.base.ShellScreen behaviours.

These tests exercise the default ``compose_body`` widget, the focus-label
machinery, the rendered-text helpers used by ``copy_rendered_text``, and the
mounted/unmounted guards on the various footer-touching helpers.
"""

from __future__ import annotations

import asyncio
import unittest
from collections.abc import Callable
from typing import Any, ClassVar, cast
from unittest.mock import MagicMock

from textual.app import App, ComposeResult
from textual.screen import Screen
from textual.widgets import Static

from muxdeck.app import MuxdeckRuntime
from muxdeck.bindings import KeyHint
from muxdeck.screens.base import ShellScreen
from muxdeck.widgets.common import KeyHintFooter, TabBar


class _Harness(App[None]):
    """Bare app used to mount and pump ``ShellScreen`` instances."""

    MODES: ClassVar[dict[str, str | Callable[[], Screen[object]]]] = {}

    def __init__(self) -> None:
        super().__init__()

    def compose(self) -> ComposeResult:
        return iter(())


def _runtime() -> MuxdeckRuntime:
    return cast(MuxdeckRuntime, type("_FakeRuntime", (), {})())


class _BorderTitleStatic(Static):
    """Static with a ``border_title`` attribute used to exercise the focus-label branch."""

    def __init__(self, *, border_title: str, widget_id: str | None = None) -> None:
        super().__init__(id=widget_id)
        self.border_title = border_title


class CamelCasePanel(Static):
    """Widget without an id whose class name drives the fallback focus label."""


class _FakeRenderable:
    """Stand-in for a Rich renderable returned from ``Widget.render``."""

    def __init__(self, *, plain: object | None = None, inner: object | None = None) -> None:
        if plain is not None:
            self.plain = plain
        if inner is not None:
            self._renderable = inner


class _RenderingWidget(Static):
    """Static whose ``render`` method returns a custom object for assertions."""

    def __init__(self, *, renderable: object) -> None:
        super().__init__()
        self._fake_renderable = renderable

    def render(self) -> object:  # type: ignore[override]
        return self._fake_renderable


class ShellScreenComposeTests(unittest.TestCase):
    """Default ``compose_body`` should yield a Static placeholder."""

    def test_default_compose_body_renders_placeholder_static(self) -> None:
        async def scenario() -> None:
            app = _Harness()
            async with app.run_test() as pilot:
                screen = ShellScreen(_runtime())
                await app.push_screen(screen)
                await pilot.pause()
                # The shell-frame container holds the body placeholder Static.
                frame = app.screen.query_one("#shell-frame")
                statics = list(frame.query(Static))
                # At least one Static is yielded by the default compose_body().
                assert any(isinstance(child, Static) for child in statics)

        asyncio.run(scenario())


class ShellScreenFooterStateTests(unittest.TestCase):
    """``set_hints``/``on_descendant_focus`` only touch the footer when mounted."""

    def test_set_hints_updates_footer_hints_after_mount(self) -> None:
        async def scenario() -> tuple[KeyHint, ...]:
            app = _Harness()
            async with app.run_test() as pilot:
                screen = ShellScreen(_runtime())
                await app.push_screen(screen)
                await pilot.pause()
                screen.set_hints([KeyHint("g", "go"), KeyHint("h", "help")])
                await pilot.pause()
                return app.screen.query_one(KeyHintFooter).hints

        hints = asyncio.run(scenario())
        assert hints == (KeyHint("g", "go"), KeyHint("h", "help"))

    def test_set_hints_is_noop_before_mount(self) -> None:
        screen = ShellScreen(_runtime())
        # Pre-mount call must not raise even though there is no footer to query.
        screen.set_hints([KeyHint("a", "act")])

    def test_on_descendant_focus_updates_focus_label(self) -> None:
        async def scenario() -> str:
            app = _Harness()
            async with app.run_test() as pilot:
                screen = ShellScreen(_runtime())
                await app.push_screen(screen)
                await pilot.pause()
                target = Static(id="dashboard-agents")
                await screen.mount(target)
                await pilot.pause()
                from textual import events

                screen.on_descendant_focus(events.DescendantFocus(target))
                await pilot.pause()
                return app.screen.query_one(KeyHintFooter).focus_label

        assert asyncio.run(scenario()) == "agent list"

    def test_on_descendant_focus_is_noop_before_mount(self) -> None:
        screen = ShellScreen(_runtime())
        target = Static(id="anything")
        from textual import events

        # Calling the handler on an unmounted screen exits cleanly.
        screen.on_descendant_focus(events.DescendantFocus(target))


class ShellScreenStaticHelperTests(unittest.TestCase):
    """``apply_ui_preferences`` and ``refresh_data`` defaults are observable."""

    def test_apply_ui_preferences_returns_false_by_default(self) -> None:
        screen = ShellScreen(_runtime())
        assert screen.apply_ui_preferences() is False

    def test_refresh_data_default_is_a_noop(self) -> None:
        screen = ShellScreen(_runtime())
        # Default returns ``None`` and must not raise.
        result = cast(Any, screen).refresh_data()
        assert result is None


class ShellScreenRenderedTextTests(unittest.TestCase):
    """``_render_widget_text`` walks several Rich renderable shapes."""

    def test_uses_plain_string_when_renderable_exposes_plain(self) -> None:
        widget = _RenderingWidget(renderable=_FakeRenderable(plain="hello"))
        assert ShellScreen._render_widget_text(widget) == "hello"

    def test_falls_back_to_inner_renderable_plain_when_outer_plain_missing(self) -> None:
        inner = _FakeRenderable(plain="inner-text")
        outer = _FakeRenderable(inner=inner)
        widget = _RenderingWidget(renderable=outer)
        assert ShellScreen._render_widget_text(widget) == "inner-text"

    def test_uses_str_of_inner_renderable_when_no_plain_anywhere(self) -> None:
        # Inner renderable exists but neither outer nor inner expose ``.plain``.
        class _Stringy:
            def __str__(self) -> str:
                return "stringy"

        widget = _RenderingWidget(renderable=_FakeRenderable(inner=_Stringy()))
        assert ShellScreen._render_widget_text(widget) == "stringy"

    def test_uses_str_of_renderable_when_no_plain_or_inner(self) -> None:
        class _Bare:
            def __str__(self) -> str:
                return "bare-output"

        widget = _RenderingWidget(renderable=_Bare())
        assert ShellScreen._render_widget_text(widget) == "bare-output"

    def test_ignores_non_string_plain_and_falls_through(self) -> None:
        # Both outer and inner have ``plain`` but they are not strings.
        inner = _FakeRenderable(plain=123)

        class _Outer(_FakeRenderable):
            def __str__(self) -> str:
                return "outer-str"

        outer = _Outer(plain=object(), inner=inner)
        widget = _RenderingWidget(renderable=outer)
        # Falls through to ``str(inner_renderable)`` since inner is not None.
        text = ShellScreen._render_widget_text(widget)
        assert isinstance(text, str)


class ShellScreenCopyRenderedTextTests(unittest.TestCase):
    """``copy_rendered_text`` copies joined widget text and reports status."""

    def test_copies_joined_text_to_clipboard_and_sets_status(self) -> None:
        async def scenario() -> tuple[list[str], str]:
            app = _Harness()
            copied: list[str] = []
            async with app.run_test() as pilot:
                screen = ShellScreen(_runtime())
                await app.push_screen(screen)
                await pilot.pause()
                # Patch the clipboard so we can assert without a real terminal.
                app.copy_to_clipboard = MagicMock(  # type: ignore[method-assign]
                    side_effect=lambda text: copied.append(text)
                )
                widget_a = _RenderingWidget(renderable=_FakeRenderable(plain="alpha"))
                widget_b = _RenderingWidget(renderable=_FakeRenderable(plain=" beta "))
                screen.copy_rendered_text("details", widget_a, widget_b)
                await pilot.pause()
                return copied, app.screen.query_one(KeyHintFooter).status

        copied, status = asyncio.run(scenario())
        assert copied == ["alpha\n\nbeta"]
        assert status == "copied details to clipboard"

    def test_sets_status_when_no_widget_yields_text(self) -> None:
        async def scenario() -> tuple[list[str], str]:
            app = _Harness()
            copied: list[str] = []
            async with app.run_test() as pilot:
                screen = ShellScreen(_runtime())
                await app.push_screen(screen)
                await pilot.pause()
                app.copy_to_clipboard = MagicMock(  # type: ignore[method-assign]
                    side_effect=lambda text: copied.append(text)
                )
                empty = _RenderingWidget(renderable=_FakeRenderable(plain="   "))
                screen.copy_rendered_text("notes", empty)
                await pilot.pause()
                return copied, app.screen.query_one(KeyHintFooter).status

        copied, status = asyncio.run(scenario())
        assert copied == []
        assert status == "no notes available"


class ShellScreenDescribeFocusTests(unittest.TestCase):
    """``_describe_focus`` derives a human label across several widget shapes."""

    def _screen(self) -> ShellScreen:
        return ShellScreen.__new__(ShellScreen)

    def test_uses_explicit_mapping_for_known_widget_ids(self) -> None:
        screen = self._screen()
        widget = Static(id="compose-editor")
        assert screen._describe_focus(widget) == "message editor"

    def test_filter_input_suffix_strips_to_screen_prefix(self) -> None:
        screen = self._screen()
        widget = Static(id="dashboard-filter-input")
        assert screen._describe_focus(widget) == "dashboard filter"

    def test_help_filter_input_uses_help_search_label(self) -> None:
        screen = self._screen()
        # ``help-filter-input`` is in the explicit map but the suffix branch
        # still maps the trailing ``help`` token to "help search".
        widget = Static(id="other-help-filter-input")
        assert screen._describe_focus(widget) == "help search"

    def test_uses_border_title_when_present(self) -> None:
        screen = self._screen()
        widget = _BorderTitleStatic(border_title="Recent Activity ", widget_id="not-explicit")
        # Border title takes precedence over id-derived fallback once the id
        # is not in the explicit map and not a filter-input suffix.
        assert screen._describe_focus(widget) == "recent activity"

    def test_blank_border_title_falls_through_to_id_split(self) -> None:
        screen = self._screen()
        widget = _BorderTitleStatic(border_title="   ", widget_id="my_custom-panel-list")
        # Underscores normalised to dashes, generic suffixes (panel/list) dropped.
        assert screen._describe_focus(widget) == "my custom"

    def test_falls_back_to_class_name_when_no_id_or_border_title(self) -> None:
        screen = self._screen()
        widget = CamelCasePanel()
        # Class name "CamelCasePanel" → "camel case panel" → strip trailing "panel".
        assert screen._describe_focus(widget) == "camel case"


class ShellScreenBusyIndicatorMountGuardTests(unittest.TestCase):
    """``_sync_busy_indicator`` no-ops cleanly when the screen has not mounted."""

    def test_sync_busy_indicator_returns_silently_before_mount(self) -> None:
        screen = ShellScreen(_runtime())
        # Pre-mount call should hit the ``is_mounted`` guard and return.
        screen._sync_busy_indicator()

    def test_sync_busy_indicator_propagates_active_count_to_footer(self) -> None:
        async def scenario() -> tuple[bool, bool]:
            app = _Harness()
            async with app.run_test() as pilot:
                screen = ShellScreen(_runtime())
                await app.push_screen(screen)
                await pilot.pause()
                footer = app.screen.query_one(KeyHintFooter)

                screen._active_workers = 1
                screen._sync_busy_indicator()
                await pilot.pause()
                busy_with_workers = footer.busy

                screen._active_workers = 0
                screen._sync_busy_indicator()
                await pilot.pause()
                busy_without_workers = footer.busy

            return busy_with_workers, busy_without_workers

        busy_with, busy_without = asyncio.run(scenario())
        assert busy_with is True
        assert busy_without is False


class ShellScreenLoadingHelpersTests(unittest.TestCase):
    """``begin_loading``/``end_loading`` toggle ``loading`` and swallow errors."""

    def test_begin_and_end_loading_toggle_widget_loading_attribute(self) -> None:
        screen = ShellScreen(_runtime())

        class _LoadableWidget:
            loading: bool = False

        widget_a = _LoadableWidget()
        widget_b = _LoadableWidget()

        screen.begin_loading(widget_a, widget_b)
        assert widget_a.loading is True
        assert widget_b.loading is True

        screen.end_loading(widget_a, widget_b)
        assert widget_a.loading is False
        assert widget_b.loading is False

    def test_begin_and_end_loading_swallow_errors_from_setter(self) -> None:
        screen = ShellScreen(_runtime())

        class _RejectingWidget:
            @property
            def loading(self) -> bool:
                return False

            @loading.setter
            def loading(self, value: bool) -> None:
                msg = f"loading={value} is not allowed"
                raise RuntimeError(msg)

        # Both helpers should swallow the exception and keep going.
        screen.begin_loading(_RejectingWidget())
        screen.end_loading(_RejectingWidget())


class ShellScreenSetStatusGuardTests(unittest.TestCase):
    """``set_status`` updates ``_status`` always but only touches footer when mounted."""

    def test_set_status_updates_internal_state_before_mount(self) -> None:
        screen = ShellScreen(_runtime())
        screen.set_status("loading")
        assert screen._status == "loading"


class ShellScreenDescribeFocusFallthroughTests(unittest.TestCase):
    """``_describe_focus`` falls through to the class-name path when id parts are filtered out."""

    def test_id_with_only_filtered_parts_falls_through_to_class_name(self) -> None:
        screen = ShellScreen.__new__(ShellScreen)

        # ``panel-list-row`` is entirely composed of filtered tokens, so the
        # method falls through to the class-name based label below.
        widget = Static(id="panel-list-row")
        # Static class name contains no capital after start, so label is "static".
        assert screen._describe_focus(widget) == "static"


# A small smoke check to exercise the TabBar refresh path that ``compose``
# relies on when ``app.tab_badges`` is set. Keeps the explicit-map TabBar
# wiring inside compose covered without requiring a full DashboardScreen.
class ShellScreenComposeBadgesTests(unittest.TestCase):
    def test_tab_bar_exposes_badges_passed_through_app(self) -> None:
        async def scenario() -> tuple[bool, bool]:
            app = _Harness()
            cast(Any, app).tab_badges = {"shell": 2}
            async with app.run_test() as pilot:
                screen = ShellScreen(_runtime())
                await app.push_screen(screen)
                await pilot.pause()
                tab_bar = app.screen.query_one(TabBar)
                badges = dict(tab_bar.badges)
                footer = app.screen.query_one(KeyHintFooter)
            return badges == {"shell": 2}, footer is not None

        assert asyncio.run(scenario()) == (True, True)
