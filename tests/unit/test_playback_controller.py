# ruff: noqa: E402,I001,PT009,PT027

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from copilot_commander.services.playback_controller import (
    SPEED_DOUBLE,
    SPEED_HALF,
    SPEED_MAX,
    SPEED_NORMAL,
    SPEED_QUAD,
    EmptyTimelineError,
    PlaybackState,
    advance,
    cycle_speed,
    jump_to,
    jump_to_ordinal,
    make_initial_state,
    selected_ordinal,
    step,
    toggle_play,
)


@dataclass(frozen=True, slots=True)
class _Entry:
    ordinal: int
    timestamp: datetime


def _entries(*offsets_seconds: int) -> tuple[_Entry, ...]:
    base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
    return tuple(
        _Entry(ordinal=i, timestamp=base + timedelta(seconds=offset))
        for i, offset in enumerate(offsets_seconds)
    )


class PlaybackControllerTests(unittest.TestCase):
    def test_make_initial_state_uses_first_and_last_timestamps(self) -> None:
        entries = _entries(0, 10, 30)
        state = make_initial_state(entries)
        self.assertEqual(state.mode, "paused")
        self.assertIs(state.speed, SPEED_NORMAL)
        self.assertEqual(state.clock, entries[0].timestamp)
        self.assertEqual(state.start, entries[0].timestamp)
        self.assertEqual(state.end, entries[-1].timestamp)

    def test_make_initial_state_raises_on_empty(self) -> None:
        with self.assertRaises(EmptyTimelineError):
            make_initial_state(())

    def test_advance_paused_is_noop(self) -> None:
        entries = _entries(0, 10, 30)
        state = make_initial_state(entries)
        self.assertEqual(advance(state, timedelta(seconds=5)), state)

    def test_advance_at_each_speed_scales_clock(self) -> None:
        entries = _entries(0, 100)
        base = make_initial_state(entries)
        for speed, expected_seconds in (
            (SPEED_HALF, 1.0),
            (SPEED_NORMAL, 2.0),
            (SPEED_DOUBLE, 4.0),
            (SPEED_QUAD, 8.0),
        ):
            with self.subTest(speed=speed.label):
                state = PlaybackState(
                    mode="playing",
                    speed=speed,
                    clock=base.start,
                    start=base.start,
                    end=base.end,
                )
                advanced = advance(state, timedelta(seconds=2))
                self.assertEqual(
                    (advanced.clock - state.start).total_seconds(),
                    expected_seconds,
                )
                self.assertEqual(advanced.mode, "playing")

    def test_advance_max_speed_jumps_to_end(self) -> None:
        entries = _entries(0, 100)
        base = make_initial_state(entries)
        state = PlaybackState(
            mode="playing",
            speed=SPEED_MAX,
            clock=base.start,
            start=base.start,
            end=base.end,
        )
        advanced = advance(state, timedelta(seconds=0.001))
        self.assertEqual(advanced.clock, base.end)
        self.assertEqual(advanced.mode, "paused")

    def test_advance_clamps_at_end_and_pauses(self) -> None:
        entries = _entries(0, 10)
        base = make_initial_state(entries)
        state = PlaybackState(
            mode="playing",
            speed=SPEED_DOUBLE,
            clock=base.end - timedelta(seconds=1),
            start=base.start,
            end=base.end,
        )
        advanced = advance(state, timedelta(seconds=10))
        self.assertEqual(advanced.clock, base.end)
        self.assertEqual(advanced.mode, "paused")

    def test_step_next_snaps_to_following_entry(self) -> None:
        entries = _entries(0, 10, 30, 60)
        base = make_initial_state(entries)
        state = PlaybackState(
            mode="paused",
            speed=SPEED_NORMAL,
            clock=base.start + timedelta(seconds=15),
            start=base.start,
            end=base.end,
        )
        stepped = step(state, entries, direction=1)
        self.assertEqual(stepped.clock, entries[2].timestamp)
        self.assertEqual(stepped.mode, "paused")

    def test_step_prev_snaps_to_previous_entry(self) -> None:
        entries = _entries(0, 10, 30, 60)
        base = make_initial_state(entries)
        state = PlaybackState(
            mode="playing",
            speed=SPEED_NORMAL,
            clock=base.start + timedelta(seconds=20),
            start=base.start,
            end=base.end,
        )
        stepped = step(state, entries, direction=-1)
        self.assertEqual(stepped.clock, entries[1].timestamp)
        self.assertEqual(stepped.mode, "paused")

    def test_jump_to_clamps_and_pauses(self) -> None:
        entries = _entries(0, 10)
        base = make_initial_state(entries)
        state = PlaybackState(
            mode="playing",
            speed=SPEED_NORMAL,
            clock=base.start,
            start=base.start,
            end=base.end,
        )
        before = jump_to(state, base.start - timedelta(seconds=5))
        after = jump_to(state, base.end + timedelta(seconds=5))
        self.assertEqual(before.clock, base.start)
        self.assertEqual(before.mode, "paused")
        self.assertEqual(after.clock, base.end)
        self.assertEqual(after.mode, "paused")

    def test_jump_to_ordinal(self) -> None:
        entries = _entries(0, 10, 30)
        base = make_initial_state(entries)
        target = jump_to_ordinal(base, entries, ordinal=2)
        self.assertEqual(target.clock, entries[2].timestamp)

    def test_toggle_play_resets_when_at_end(self) -> None:
        entries = _entries(0, 10)
        base = make_initial_state(entries)
        ended = PlaybackState(
            mode="paused",
            speed=SPEED_NORMAL,
            clock=base.end,
            start=base.start,
            end=base.end,
        )
        resumed = toggle_play(ended)
        self.assertEqual(resumed.mode, "playing")
        self.assertEqual(resumed.clock, base.start)

    def test_toggle_play_pauses_then_plays(self) -> None:
        entries = _entries(0, 10)
        base = make_initial_state(entries)
        playing = toggle_play(base)
        self.assertEqual(playing.mode, "playing")
        paused = toggle_play(playing)
        self.assertEqual(paused.mode, "paused")

    def test_cycle_speed_wraps_forward(self) -> None:
        entries = _entries(0, 10)
        base = make_initial_state(entries)
        # Initial speed is 1x (index 1); cycle through every speed and
        # then wrap once to confirm the rotation order.
        expected = ["2x", "4x", "MAX", "0.5x", "1x", "2x"]
        sequence: list[str] = []
        state = base
        for _ in range(len(expected)):
            state = cycle_speed(state)
            sequence.append(state.speed.label)
        self.assertEqual(sequence, expected)

    def test_cycle_speed_backward(self) -> None:
        entries = _entries(0, 10)
        base = make_initial_state(entries)
        backward = cycle_speed(base, direction=-1)
        self.assertIs(backward.speed, SPEED_HALF)

    def test_selected_ordinal_returns_none_before_first_entry(self) -> None:
        entries = _entries(10, 20, 30)
        base = make_initial_state(entries)
        state = PlaybackState(
            mode="paused",
            speed=SPEED_NORMAL,
            clock=base.start - timedelta(seconds=5),
            start=base.start - timedelta(seconds=5),
            end=base.end,
        )
        self.assertIsNone(selected_ordinal(state, entries))

    def test_selected_ordinal_at_exact_timestamp(self) -> None:
        entries = _entries(0, 10, 30)
        base = make_initial_state(entries)
        state = PlaybackState(
            mode="paused",
            speed=SPEED_NORMAL,
            clock=entries[1].timestamp,
            start=base.start,
            end=base.end,
        )
        self.assertEqual(selected_ordinal(state, entries), entries[1].ordinal)

    def test_selected_ordinal_between_entries(self) -> None:
        entries = _entries(0, 10, 30)
        base = make_initial_state(entries)
        state = PlaybackState(
            mode="paused",
            speed=SPEED_NORMAL,
            clock=entries[1].timestamp + timedelta(seconds=5),
            start=base.start,
            end=base.end,
        )
        self.assertEqual(selected_ordinal(state, entries), entries[1].ordinal)


if __name__ == "__main__":
    unittest.main()
