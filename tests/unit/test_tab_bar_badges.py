# ruff: noqa: ANN201

from __future__ import annotations

from copilot_commander.widgets.common import TabBar


def _render_plain(bar: TabBar) -> str:
    return bar.render().plain


def test_tab_bar_renders_without_badge_when_count_is_zero() -> None:
    bar = TabBar(active="dashboard", badges={"attention": 0})
    rendered = _render_plain(bar)

    assert "attention" in rendered
    assert "⬤" not in rendered


def test_tab_bar_renders_badge_when_count_is_positive() -> None:
    bar = TabBar(active="dashboard", badges={"attention": 3})
    rendered = _render_plain(bar)

    assert "⬤3" in rendered


def test_tab_bar_set_badges_updates_render() -> None:
    bar = TabBar(active="dashboard")
    assert "⬤" not in _render_plain(bar)

    bar.set_badges({"attention": 2})
    assert "⬤2" in _render_plain(bar)

    bar.set_badges({"attention": 0})
    assert "⬤" not in _render_plain(bar)


def test_tab_bar_default_has_empty_badges() -> None:
    bar = TabBar(active="dashboard")
    assert bar.badges == {}
