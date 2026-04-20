from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from os import PathLike as OsPathLike
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pathlib import Path

    from muxdeck.domain.events import Event, LogChunk
    from muxdeck.domain.models import Agent, Session, Worktree
    from muxdeck.domain.value_objects import CommandResult

type JsonPrimitive = str | int | float | bool | None
type JsonValue = JsonPrimitive | dict[str, JsonValue] | list[JsonValue]
type JsonObject = dict[str, JsonValue]
type PathLike = str | OsPathLike[str]
type Environment = Mapping[str, str]
type Clock = Callable[[], datetime]
type RepoName = str
type BranchName = str
type TaskSlug = str
type ConfidenceValue = Decimal | str | int | float
type CommandArgs = tuple[str, ...]


@runtime_checkable
class HasTimestamp(Protocol):
    @property
    def occurred_at(self) -> datetime:
        """Return the primary timestamp for an event-like object."""


@runtime_checkable
class AgentStore(Protocol):
    def upsert_agent(self, agent: Agent, /) -> None:
        """Persist or replace a single agent snapshot."""

    def list_agents(self) -> Sequence[Agent]:
        """Return all known agents."""


@runtime_checkable
class WorktreeStore(Protocol):
    def upsert_worktree(self, worktree: Worktree, /) -> None:
        """Persist or replace a single worktree snapshot."""

    def get_worktree(self, worktree_id: str, /) -> Worktree | None:
        """Return a worktree by identifier."""


@runtime_checkable
class SessionStore(Protocol):
    def upsert_session(self, session: Session, /) -> None:
        """Persist or replace a single session snapshot."""

    def list_sessions(self, agent_id: str | None = None, /) -> Sequence[Session]:
        """Return stored sessions, optionally filtered by agent."""

    def get_session(self, session_id: str, /) -> Session | None:
        """Return a session by identifier."""


@runtime_checkable
class EventStore(Protocol):
    def append_events(self, events: Sequence[Event], /) -> None:
        """Persist an ordered batch of events."""


@runtime_checkable
class LogChunkStore(Protocol):
    def append_log_chunks(self, chunks: Sequence[LogChunk], /) -> None:
        """Persist an ordered batch of log chunks."""

    def list_log_chunks(self, session_id: str, /) -> Sequence[LogChunk]:
        """Return captured log chunks for a session."""

    def get_log_chunk(self, log_chunk_id: str, /) -> LogChunk | None:
        """Return a log chunk by identifier."""


@runtime_checkable
class CommandRunner(Protocol):
    def run(
        self,
        command: Sequence[str],
        /,
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_sec: float | None = None,
    ) -> CommandResult:
        """Run a command and capture its result."""
