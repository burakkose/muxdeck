"""Pure time-driven playback math for the replay screen.

This module is intentionally framework-free: no Textual, no infrastructure,
no I/O. Functions take any :class:`TimedEntry` sequence (e.g. the
service's :class:`ReplayEntry` or a lightweight controller adapter) and
produce immutable :class:`PlaybackState` values. The replay screen wires
the timer; the controller layer translates between :class:`PlaybackState`
and :class:`PlaybackStateView`.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Final, Literal, Protocol


class TimedEntry(Protocol):
    """Minimal entry shape needed for playback math."""

    @property
    def ordinal(self) -> int: ...

    @property
    def timestamp(self) -> datetime: ...


PlaybackMode = Literal["paused", "playing"]
StepDirection = Literal[-1, 1]


class EmptyTimelineError(ValueError):
    """Raised when initializing playback for an empty replay."""


@dataclass(frozen=True, slots=True)
class PlaybackSpeed:
    """Display label and effective wall-clock multiplier for a speed.

    A multiplier of :data:`math.inf` represents the ``"max"`` speed,
    which jumps straight to the end of the timeline on the next
    :func:`advance` tick.
    """

    label: str
    multiplier: float

    @property
    def is_max(self) -> bool:
        return math.isinf(self.multiplier)


SPEED_HALF: Final = PlaybackSpeed(label="0.5x", multiplier=0.5)
SPEED_NORMAL: Final = PlaybackSpeed(label="1x", multiplier=1.0)
SPEED_DOUBLE: Final = PlaybackSpeed(label="2x", multiplier=2.0)
SPEED_QUAD: Final = PlaybackSpeed(label="4x", multiplier=4.0)
SPEED_MAX: Final = PlaybackSpeed(label="MAX", multiplier=math.inf)

SPEED_ORDER: Final[tuple[PlaybackSpeed, ...]] = (
    SPEED_HALF,
    SPEED_NORMAL,
    SPEED_DOUBLE,
    SPEED_QUAD,
    SPEED_MAX,
)


@dataclass(frozen=True, slots=True)
class PlaybackState:
    mode: PlaybackMode
    speed: PlaybackSpeed
    clock: datetime
    start: datetime
    end: datetime

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    @property
    def progress(self) -> float:
        total = self.duration.total_seconds()
        if total <= 0:
            return 1.0
        elapsed = (self.clock - self.start).total_seconds()
        return max(0.0, min(1.0, elapsed / total))


def make_initial_state(entries: Sequence[TimedEntry]) -> PlaybackState:
    """Build the paused initial playback state for ``entries``.

    Raises :class:`EmptyTimelineError` if there are no entries — a
    timeline with no bounds cannot be played back.
    """

    if not entries:
        msg = "cannot initialize playback for an empty timeline"
        raise EmptyTimelineError(msg)
    start = entries[0].timestamp
    end = entries[-1].timestamp
    if end < start:
        # Should not happen because the service orders by timestamp,
        # but guard against malformed inputs deterministically.
        end = start
    return PlaybackState(
        mode="paused",
        speed=SPEED_NORMAL,
        clock=start,
        start=start,
        end=end,
    )


def advance(state: PlaybackState, real_elapsed: timedelta) -> PlaybackState:
    """Advance the virtual clock by ``real_elapsed * speed``.

    A no-op when paused. ``"max"`` speed jumps directly to ``end``.
    Auto-pauses when the clock reaches ``end``.
    """

    if state.mode == "paused":
        return state
    if state.speed.is_max or real_elapsed >= state.duration:
        return replace(state, clock=state.end, mode="paused")
    if real_elapsed.total_seconds() <= 0:
        return state
    delta = timedelta(seconds=real_elapsed.total_seconds() * state.speed.multiplier)
    new_clock = state.clock + delta
    if new_clock >= state.end:
        return replace(state, clock=state.end, mode="paused")
    return replace(state, clock=new_clock)


def step(
    state: PlaybackState,
    entries: Sequence[TimedEntry],
    *,
    direction: StepDirection,
) -> PlaybackState:
    """Snap the clock to the previous or next entry timestamp and pause."""

    if not entries:
        return state
    timestamps = [entry.timestamp for entry in entries]
    if direction == 1:
        target = next((ts for ts in timestamps if ts > state.clock), timestamps[-1])
    else:
        target = next(
            (ts for ts in reversed(timestamps) if ts < state.clock),
            timestamps[0],
        )
    return jump_to(state, target)


def jump_to(state: PlaybackState, target: datetime) -> PlaybackState:
    """Move the clock to ``target``, clamped to ``[start, end]``, and pause."""

    clamped = max(state.start, min(state.end, target))
    return replace(state, clock=clamped, mode="paused")


def jump_to_ordinal(
    state: PlaybackState,
    entries: Sequence[TimedEntry],
    ordinal: int,
) -> PlaybackState:
    """Jump to the entry with the given ordinal; pause."""

    for entry in entries:
        if entry.ordinal == ordinal:
            return jump_to(state, entry.timestamp)
    return state


def toggle_play(state: PlaybackState) -> PlaybackState:
    """Toggle play/pause. Resuming at ``end`` rewinds to ``start``."""

    if state.mode == "playing":
        return replace(state, mode="paused")
    if state.clock >= state.end:
        return replace(state, mode="playing", clock=state.start)
    return replace(state, mode="playing")


def cycle_speed(state: PlaybackState, *, direction: StepDirection = 1) -> PlaybackState:
    """Rotate through :data:`SPEED_ORDER`. ``direction=-1`` cycles backward."""

    try:
        index = SPEED_ORDER.index(state.speed)
    except ValueError:
        return replace(state, speed=SPEED_NORMAL)
    next_index = (index + direction) % len(SPEED_ORDER)
    return replace(state, speed=SPEED_ORDER[next_index])


def selected_ordinal(
    state: PlaybackState,
    entries: Sequence[TimedEntry],
) -> int | None:
    """Return the ordinal of the latest entry whose timestamp ``<= clock``."""

    selected: int | None = None
    for entry in entries:
        if entry.timestamp <= state.clock:
            selected = entry.ordinal
        else:
            break
    return selected


__all__ = [
    "SPEED_DOUBLE",
    "SPEED_HALF",
    "SPEED_MAX",
    "SPEED_NORMAL",
    "SPEED_ORDER",
    "SPEED_QUAD",
    "EmptyTimelineError",
    "PlaybackMode",
    "PlaybackSpeed",
    "PlaybackState",
    "StepDirection",
    "TimedEntry",
    "advance",
    "cycle_speed",
    "jump_to",
    "jump_to_ordinal",
    "make_initial_state",
    "selected_ordinal",
    "step",
    "toggle_play",
]
