from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from importlib.resources import files
from pathlib import Path
from typing import Literal, cast

from muxdeck.config import AppConfig
from muxdeck.constants import DEFAULT_DATABASE_FILE_NAME as _DEFAULT_DATABASE_FILE_NAME
from muxdeck.domain.enums import AgentStatus, TaskPriority, TaskStatus
from muxdeck.domain.events import Event, LogChunk
from muxdeck.domain.models import Agent, Session, Worktree
from muxdeck.domain.replay_annotations import (
    ReplayAnnotation,
    ReplayAnnotationKind,
)
from muxdeck.domain.task_models import Task
from muxdeck.domain.value_objects import (
    ensure_aware_datetime,
    ensure_non_empty_text,
    utc_now,
)
from muxdeck.exceptions import DomainValidationError, PersistenceError
from muxdeck.types import JsonValue, PathLike

AgentBackend = Literal["copilot_cli"]
EventSeverity = Literal["debug", "info", "warning", "error"]
LogSource = Literal["tmux_capture", "stdout", "stderr", "system"]

DEFAULT_DATABASE_FILE_NAME = _DEFAULT_DATABASE_FILE_NAME

_MIGRATIONS_PACKAGE = "muxdeck.adapters.migrations"
_PRAGMAS = ("PRAGMA foreign_keys = ON", "PRAGMA journal_mode = WAL")
_CREATE_MIGRATIONS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
)
"""

_AGENT_COLUMNS = (
    "id",
    "name",
    "backend",
    "tmux_session_name",
    "tmux_window_id",
    "tmux_window_name",
    "tmux_pane_id",
    "pane_tty",
    "cwd",
    "repo_root",
    "worktree_path",
    "branch",
    "task_title",
    "task_summary",
    "copilot_session_id",
    "pid",
    "status",
    "started_at",
    "last_activity_at",
    "last_seen_at",
    "idle_seconds",
    "needs_attention",
    "attention_reason",
    "token_input",
    "token_output",
    "token_total",
    "estimated_cost_usd",
)
_WORKTREE_COLUMNS = (
    "id",
    "repo_root",
    "path",
    "branch",
    "base_branch",
    "is_main_worktree",
    "is_dirty",
    "ahead_count",
    "behind_count",
    "locked",
    "assigned_agent_id",
    "created_at",
    "last_seen_at",
)
_TASK_COLUMNS = (
    "id",
    "title",
    "summary",
    "description",
    "repo_root",
    "priority",
    "status",
    "assigned_agent_id",
    "assigned_worktree_id",
    "created_at",
    "started_at",
    "completed_at",
    "notes",
)
_SESSION_COLUMNS = (
    "id",
    "agent_id",
    "copilot_session_id",
    "task_title",
    "created_at",
    "ended_at",
    "exit_reason",
)
_EVENT_COLUMNS = (
    "id",
    "occurred_at",
    "agent_id",
    "session_id",
    "kind",
    "severity",
    "payload_json",
)
_LOG_CHUNK_COLUMNS = (
    "id",
    "agent_id",
    "session_id",
    "source",
    "sequence_no",
    "captured_at",
    "content",
)
_SESSION_CONTEXT_COLUMNS = (
    "session_id",
    "agent_id",
    "worktree_id",
    "tmux_pane_id",
    "pane_tty",
    "worktree_path",
    "copilot_session_id",
    "repo_root",
    "branch",
    "updated_at",
)


def _placeholders(columns: Sequence[str]) -> str:
    return ", ".join(f":{column}" for column in columns)


_UPSERT_AGENT_SQL = f"""
INSERT INTO agents ({", ".join(_AGENT_COLUMNS)})
VALUES ({_placeholders(_AGENT_COLUMNS)})
ON CONFLICT(id) DO UPDATE SET
    name = excluded.name,
    backend = excluded.backend,
    tmux_session_name = excluded.tmux_session_name,
    tmux_window_id = excluded.tmux_window_id,
    tmux_window_name = excluded.tmux_window_name,
    tmux_pane_id = excluded.tmux_pane_id,
    pane_tty = excluded.pane_tty,
    cwd = excluded.cwd,
    repo_root = excluded.repo_root,
    worktree_path = excluded.worktree_path,
    branch = excluded.branch,
    task_title = excluded.task_title,
    task_summary = excluded.task_summary,
    copilot_session_id = excluded.copilot_session_id,
    pid = excluded.pid,
    status = excluded.status,
    started_at = excluded.started_at,
    last_activity_at = excluded.last_activity_at,
    last_seen_at = excluded.last_seen_at,
    idle_seconds = excluded.idle_seconds,
    needs_attention = excluded.needs_attention,
    attention_reason = excluded.attention_reason,
    token_input = excluded.token_input,
    token_output = excluded.token_output,
    token_total = excluded.token_total,
    estimated_cost_usd = excluded.estimated_cost_usd
"""
_UPDATE_AGENT_BY_PANE_SQL = """
UPDATE agents SET
    id = :id,
    name = :name,
    backend = :backend,
    tmux_session_name = :tmux_session_name,
    tmux_window_id = :tmux_window_id,
    tmux_window_name = :tmux_window_name,
    pane_tty = :pane_tty,
    cwd = :cwd,
    repo_root = :repo_root,
    worktree_path = :worktree_path,
    branch = :branch,
    task_title = :task_title,
    task_summary = :task_summary,
    copilot_session_id = :copilot_session_id,
    pid = :pid,
    status = :status,
    started_at = :started_at,
    last_activity_at = :last_activity_at,
    last_seen_at = :last_seen_at,
    idle_seconds = :idle_seconds,
    needs_attention = :needs_attention,
    attention_reason = :attention_reason,
    token_input = :token_input,
    token_output = :token_output,
    token_total = :token_total,
    estimated_cost_usd = :estimated_cost_usd
WHERE tmux_pane_id = :tmux_pane_id
"""

_UPSERT_WORKTREE_SQL = f"""
INSERT INTO worktrees ({", ".join(_WORKTREE_COLUMNS)})
VALUES ({_placeholders(_WORKTREE_COLUMNS)})
ON CONFLICT(id) DO UPDATE SET
    repo_root = excluded.repo_root,
    path = excluded.path,
    branch = excluded.branch,
    base_branch = excluded.base_branch,
    is_main_worktree = excluded.is_main_worktree,
    is_dirty = excluded.is_dirty,
    ahead_count = excluded.ahead_count,
    behind_count = excluded.behind_count,
    locked = excluded.locked,
    assigned_agent_id = excluded.assigned_agent_id,
    created_at = excluded.created_at,
    last_seen_at = excluded.last_seen_at
"""
_UPDATE_WORKTREE_BY_PATH_SQL = """
UPDATE worktrees SET
    id = :id,
    repo_root = :repo_root,
    branch = :branch,
    base_branch = :base_branch,
    is_main_worktree = :is_main_worktree,
    is_dirty = :is_dirty,
    ahead_count = :ahead_count,
    behind_count = :behind_count,
    locked = :locked,
    assigned_agent_id = :assigned_agent_id,
    created_at = :created_at,
    last_seen_at = :last_seen_at
WHERE path = :path
"""

_UPSERT_TASK_SQL = f"""
INSERT INTO tasks ({", ".join(_TASK_COLUMNS)})
VALUES ({_placeholders(_TASK_COLUMNS)})
ON CONFLICT(id) DO UPDATE SET
    title = excluded.title,
    summary = excluded.summary,
    description = excluded.description,
    repo_root = excluded.repo_root,
    priority = excluded.priority,
    status = excluded.status,
    assigned_agent_id = excluded.assigned_agent_id,
    assigned_worktree_id = excluded.assigned_worktree_id,
    created_at = excluded.created_at,
    started_at = excluded.started_at,
    completed_at = excluded.completed_at,
    notes = excluded.notes
"""

_UPSERT_SESSION_SQL = f"""
INSERT INTO sessions ({", ".join(_SESSION_COLUMNS)})
VALUES ({_placeholders(_SESSION_COLUMNS)})
ON CONFLICT(id) DO UPDATE SET
    agent_id = excluded.agent_id,
    copilot_session_id = excluded.copilot_session_id,
    task_title = excluded.task_title,
    created_at = excluded.created_at,
    ended_at = excluded.ended_at,
    exit_reason = excluded.exit_reason
"""

_INSERT_EVENT_SQL = f"""
INSERT INTO events ({", ".join(_EVENT_COLUMNS)})
VALUES ({_placeholders(_EVENT_COLUMNS)})
ON CONFLICT(id) DO UPDATE SET
    occurred_at = excluded.occurred_at,
    agent_id = excluded.agent_id,
    session_id = excluded.session_id,
    kind = excluded.kind,
    severity = excluded.severity,
    payload_json = excluded.payload_json
"""

_INSERT_LOG_CHUNK_SQL = f"""
INSERT INTO log_chunks ({", ".join(_LOG_CHUNK_COLUMNS)})
VALUES ({_placeholders(_LOG_CHUNK_COLUMNS)})
ON CONFLICT(id) DO UPDATE SET
    agent_id = excluded.agent_id,
    session_id = excluded.session_id,
    source = excluded.source,
    sequence_no = excluded.sequence_no,
    captured_at = excluded.captured_at,
    content = excluded.content
"""

_UPSERT_SESSION_CONTEXT_SQL = f"""
INSERT INTO session_context_cache ({", ".join(_SESSION_CONTEXT_COLUMNS)})
VALUES ({_placeholders(_SESSION_CONTEXT_COLUMNS)})
ON CONFLICT(session_id) DO UPDATE SET
    agent_id = excluded.agent_id,
    worktree_id = excluded.worktree_id,
    tmux_pane_id = excluded.tmux_pane_id,
    pane_tty = excluded.pane_tty,
    worktree_path = excluded.worktree_path,
    copilot_session_id = excluded.copilot_session_id,
    repo_root = excluded.repo_root,
    branch = excluded.branch,
    updated_at = excluded.updated_at
"""

_SELECT_AGENT_SQL = f"SELECT {', '.join(_AGENT_COLUMNS)} FROM agents"
_SELECT_WORKTREE_SQL = f"SELECT {', '.join(_WORKTREE_COLUMNS)} FROM worktrees"
_SELECT_TASK_SQL = f"SELECT {', '.join(_TASK_COLUMNS)} FROM tasks"
_SELECT_SESSION_SQL = (
    "SELECT "
    + ", ".join(f"sessions.{column} AS {column}" for column in _SESSION_COLUMNS)
    + " FROM sessions"
)
_SELECT_EVENT_SQL = f"SELECT {', '.join(_EVENT_COLUMNS)} FROM events"
_SELECT_LOG_CHUNK_SQL = f"SELECT {', '.join(_LOG_CHUNK_COLUMNS)} FROM log_chunks"
_SELECT_SESSION_CONTEXT_SQL = (
    f"SELECT {', '.join(_SESSION_CONTEXT_COLUMNS)} FROM session_context_cache"
)

_REPLAY_ANNOTATION_COLUMNS = (
    "id",
    "session_id",
    "ordinal",
    "created_at",
    "kind",
    "body",
)
_INSERT_REPLAY_ANNOTATION_SQL = f"""
INSERT INTO replay_annotations ({", ".join(_REPLAY_ANNOTATION_COLUMNS)})
VALUES ({_placeholders(_REPLAY_ANNOTATION_COLUMNS)})
"""
_SELECT_REPLAY_ANNOTATION_SQL = (
    f"SELECT {', '.join(_REPLAY_ANNOTATION_COLUMNS)} FROM replay_annotations"
)


@dataclass(frozen=True, slots=True)
class SessionContextRecord:
    session_id: str
    agent_id: str | None = None
    worktree_id: str | None = None
    tmux_pane_id: str | None = None
    pane_tty: str | None = None
    worktree_path: str | None = None
    copilot_session_id: str | None = None
    repo_root: str | None = None
    branch: str | None = None
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "session_id",
            ensure_non_empty_text(self.session_id, field_name="session_id"),
        )
        for field_name in (
            "agent_id",
            "worktree_id",
            "tmux_pane_id",
            "pane_tty",
            "worktree_path",
            "copilot_session_id",
            "repo_root",
            "branch",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    ensure_non_empty_text(value, field_name=field_name),
                )
        object.__setattr__(
            self,
            "updated_at",
            ensure_aware_datetime(self.updated_at, field_name="updated_at"),
        )


class SQLiteStore:
    def __init__(
        self,
        config: AppConfig | None = None,
        /,
        *,
        database_path: PathLike | None = None,
        check_same_thread: bool = True,
    ) -> None:
        self._config = AppConfig.default() if config is None else config
        self._database_path, self._connection = self._connect(
            database_path,
            check_same_thread=check_same_thread,
        )
        self._bootstrap_migrations_table()
        self._run_migrations()

    @classmethod
    def from_config(
        cls,
        config: AppConfig,
        /,
        *,
        check_same_thread: bool = True,
    ) -> SQLiteStore:
        return cls(config, check_same_thread=check_same_thread)

    @property
    def database_path(self) -> Path:
        return self._database_path

    @property
    def journal_mode(self) -> str:
        row = self._fetch_pragma("journal_mode")
        value = row[0]
        if not isinstance(value, str):
            raise PersistenceError(f"invalid journal_mode value: {value!r}")
        return value.lower()

    @property
    def foreign_keys_enabled(self) -> bool:
        row = self._fetch_pragma("foreign_keys")
        value = row[0]
        if value not in (0, 1):
            raise PersistenceError(f"invalid foreign_keys value: {value!r}")
        return bool(value)

    def applied_migrations(self) -> tuple[str, ...]:
        rows = self._fetch_all(
            "SELECT version FROM migrations ORDER BY version ASC",
            operation="list migrations",
        )
        return tuple(_require_text(row, "version") for row in rows)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> SQLiteStore:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()

    def upsert_agent(self, agent: Agent, /) -> None:
        params = self._agent_params(agent)
        try:
            with self._transaction(operation="upsert agent") as connection:
                try:
                    connection.execute(_UPSERT_AGENT_SQL, params)
                except sqlite3.IntegrityError as exc:
                    if "agents.tmux_pane_id" not in str(exc):
                        raise
                    connection.execute(_UPDATE_AGENT_BY_PANE_SQL, params)
        except sqlite3.Error as exc:
            msg = f"failed to upsert agent {agent.id}"
            raise PersistenceError(msg) from exc

    def list_agents(self) -> tuple[Agent, ...]:
        rows = self._fetch_all(
            f"{_SELECT_AGENT_SQL} ORDER BY last_seen_at DESC, started_at DESC, id DESC",
            operation="list agents",
        )
        return tuple(_row_to_agent(row) for row in rows)

    def get_agent(self, agent_id: str, /) -> Agent | None:
        row = self._fetch_one(
            f"{_SELECT_AGENT_SQL} WHERE id = ?",
            (agent_id,),
            operation="get agent",
        )
        return None if row is None else _row_to_agent(row)

    def get_agent_by_tmux_pane_id(self, tmux_pane_id: str, /) -> Agent | None:
        row = self._fetch_one(
            (
                f"{_SELECT_AGENT_SQL} WHERE tmux_pane_id = ? "
                "ORDER BY last_seen_at DESC, id DESC LIMIT 1"
            ),
            (tmux_pane_id,),
            operation="get agent by tmux pane",
        )
        return None if row is None else _row_to_agent(row)

    def get_agent_by_pane_id(self, pane_id: str, /) -> Agent | None:
        return self.get_agent_by_tmux_pane_id(pane_id)

    def get_agent_by_copilot_session_id(self, copilot_session_id: str, /) -> Agent | None:
        row = self._fetch_one(
            (
                f"{_SELECT_AGENT_SQL} WHERE copilot_session_id = ? "
                "ORDER BY last_seen_at DESC, id DESC LIMIT 1"
            ),
            (copilot_session_id,),
            operation="get agent by copilot session",
        )
        return None if row is None else _row_to_agent(row)

    def upsert_worktree(self, worktree: Worktree, /) -> None:
        params = self._worktree_params(worktree)
        try:
            with self._transaction(operation="upsert worktree") as connection:
                try:
                    connection.execute(_UPSERT_WORKTREE_SQL, params)
                except sqlite3.IntegrityError as exc:
                    if "worktrees.path" not in str(exc):
                        raise
                    connection.execute(_UPDATE_WORKTREE_BY_PATH_SQL, params)
        except sqlite3.Error as exc:
            msg = f"failed to upsert worktree {worktree.id}"
            raise PersistenceError(msg) from exc

    def get_worktree(self, worktree_id: str, /) -> Worktree | None:
        row = self._fetch_one(
            f"{_SELECT_WORKTREE_SQL} WHERE id = ?",
            (worktree_id,),
            operation="get worktree",
        )
        return None if row is None else _row_to_worktree(row)

    def get_worktree_by_path(self, path: str, /) -> Worktree | None:
        row = self._fetch_one(
            (f"{_SELECT_WORKTREE_SQL} WHERE path = ? ORDER BY last_seen_at DESC, id DESC LIMIT 1"),
            (path,),
            operation="get worktree by path",
        )
        return None if row is None else _row_to_worktree(row)

    def list_worktrees(
        self,
        /,
        *,
        repo_root: str | None = None,
        assigned_agent_id: str | None = None,
    ) -> tuple[Worktree, ...]:
        clauses: list[str] = []
        params: list[object] = []
        if repo_root is not None:
            clauses.append("repo_root = ?")
            params.append(repo_root)
        if assigned_agent_id is not None:
            clauses.append("assigned_agent_id = ?")
            params.append(assigned_agent_id)
        where_clause = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._fetch_all(
            f"{_SELECT_WORKTREE_SQL}{where_clause} ORDER BY last_seen_at DESC, path ASC, id ASC",
            tuple(params),
            operation="list worktrees",
        )
        return tuple(_row_to_worktree(row) for row in rows)

    def list_worktrees_by_repo(self, repo_root: str, /) -> tuple[Worktree, ...]:
        return self.list_worktrees(repo_root=repo_root)

    def delete_worktree(self, worktree_id: str, /) -> bool:
        """Delete a worktree record by ID. Returns True if a row was deleted."""
        return self._execute_delete(
            "DELETE FROM worktrees WHERE id = ?",
            (worktree_id,),
            operation="delete worktree",
        )

    def upsert_task(self, task: Task, /) -> None:
        self._execute_write(
            _UPSERT_TASK_SQL,
            self._task_params(task),
            operation="upsert task",
        )

    def list_tasks(
        self,
        /,
        *,
        status: TaskStatus | None = None,
        assigned_agent_id: str | None = None,
        assigned_worktree_id: str | None = None,
        repo_root: str | None = None,
    ) -> tuple[Task, ...]:
        clauses: list[str] = []
        params: list[object] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if assigned_agent_id is not None:
            clauses.append("assigned_agent_id = ?")
            params.append(assigned_agent_id)
        if assigned_worktree_id is not None:
            clauses.append("assigned_worktree_id = ?")
            params.append(assigned_worktree_id)
        if repo_root is not None:
            clauses.append("repo_root = ?")
            params.append(repo_root)
        where_clause = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._fetch_all(
            f"{_SELECT_TASK_SQL}{where_clause} ORDER BY created_at DESC, id DESC",
            tuple(params),
            operation="list tasks",
        )
        return tuple(_row_to_task(row) for row in rows)

    def get_task(self, task_id: str, /) -> Task | None:
        row = self._fetch_one(
            f"{_SELECT_TASK_SQL} WHERE id = ?",
            (task_id,),
            operation="get task",
        )
        return None if row is None else _row_to_task(row)

    def delete_task(self, task_id: str, /) -> bool:
        return self._execute_delete(
            "DELETE FROM tasks WHERE id = ?",
            (task_id,),
            operation="delete task",
        )

    # ----- replay annotations -------------------------------------------------

    def insert_replay_annotation(self, annotation: ReplayAnnotation, /) -> None:
        self._execute_write(
            _INSERT_REPLAY_ANNOTATION_SQL,
            self._replay_annotation_params(annotation),
            operation="insert replay annotation",
        )

    def delete_replay_annotation(self, annotation_id: str, /) -> bool:
        return self._execute_delete(
            "DELETE FROM replay_annotations WHERE id = ?",
            (annotation_id,),
            operation="delete replay annotation",
        )

    def update_replay_annotation_body(self, annotation_id: str, body: str, /) -> bool:
        return self._execute_delete(
            "UPDATE replay_annotations SET body = ? WHERE id = ?",
            (body, annotation_id),
            operation="update replay annotation body",
        )

    def list_replay_annotations(self, session_id: str, /) -> tuple[ReplayAnnotation, ...]:
        rows = self._fetch_all(
            f"{_SELECT_REPLAY_ANNOTATION_SQL} WHERE session_id = ? "
            "ORDER BY ordinal ASC, created_at ASC, id ASC",
            (session_id,),
            operation="list replay annotations",
        )
        return tuple(_row_to_replay_annotation(row) for row in rows)

    def find_replay_bookmark(
        self,
        session_id: str,
        ordinal: int,
        /,
    ) -> ReplayAnnotation | None:
        row = self._fetch_one(
            f"{_SELECT_REPLAY_ANNOTATION_SQL} "
            "WHERE session_id = ? AND ordinal = ? AND kind = 'bookmark'",
            (session_id, ordinal),
            operation="find replay bookmark",
        )
        return None if row is None else _row_to_replay_annotation(row)

    def _replay_annotation_params(self, annotation: ReplayAnnotation) -> dict[str, object]:
        return {
            "id": annotation.id,
            "session_id": annotation.session_id,
            "ordinal": annotation.ordinal,
            "created_at": _serialize_datetime(annotation.created_at),
            "kind": annotation.kind,
            "body": annotation.body,
        }

    def upsert_session(self, session: Session, /) -> None:
        self._execute_write(
            _UPSERT_SESSION_SQL,
            self._session_params(session),
            operation="upsert session",
        )

    def list_sessions(self, agent_id: str | None = None, /) -> tuple[Session, ...]:
        where_clause = ""
        params: tuple[object, ...] = ()
        if agent_id is not None:
            where_clause = " WHERE agent_id = ?"
            params = (agent_id,)
        rows = self._fetch_all(
            f"{_SELECT_SESSION_SQL}{where_clause} ORDER BY created_at DESC, id DESC",
            params,
            operation="list sessions",
        )
        return tuple(_row_to_session(row) for row in rows)

    def list_sessions_for_worktree(self, worktree_id: str, /) -> tuple[Session, ...]:
        rows = self._fetch_all(
            f"""
            {_SELECT_SESSION_SQL}
            INNER JOIN session_context_cache ON session_context_cache.session_id = sessions.id
            WHERE session_context_cache.worktree_id = ?
            ORDER BY sessions.created_at DESC, sessions.id DESC
            """,
            (worktree_id,),
            operation="list sessions for worktree",
        )
        return tuple(_row_to_session(row) for row in rows)

    def get_session(self, session_id: str, /) -> Session | None:
        row = self._fetch_one(
            f"{_SELECT_SESSION_SQL} WHERE id = ?",
            (session_id,),
            operation="get session",
        )
        return None if row is None else _row_to_session(row)

    def get_session_by_copilot_session_id(self, copilot_session_id: str, /) -> Session | None:
        row = self._fetch_one(
            (
                f"{_SELECT_SESSION_SQL} WHERE copilot_session_id = ? "
                "ORDER BY created_at DESC, id DESC LIMIT 1"
            ),
            (copilot_session_id,),
            operation="get session by copilot session",
        )
        return None if row is None else _row_to_session(row)

    def get_session_by_tmux_pane_id(self, tmux_pane_id: str, /) -> Session | None:
        row = self._fetch_one(
            f"""
            {_SELECT_SESSION_SQL}
            INNER JOIN session_context_cache ON session_context_cache.session_id = sessions.id
            WHERE session_context_cache.tmux_pane_id = ?
            ORDER BY
                session_context_cache.updated_at DESC,
                sessions.created_at DESC,
                sessions.id DESC
            LIMIT 1
            """,
            (tmux_pane_id,),
            operation="get session by tmux pane",
        )
        return None if row is None else _row_to_session(row)

    def append_events(self, events: Sequence[Event], /) -> None:
        if not events:
            return
        self._execute_many(
            _INSERT_EVENT_SQL,
            tuple(self._event_params(event) for event in events),
            operation="append events",
        )

    def list_events(
        self,
        /,
        *,
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> tuple[Event, ...]:
        if agent_id is not None and session_id is not None:
            raise PersistenceError("agent_id and session_id cannot both be provided")
        if session_id is not None:
            return self.list_events_for_session(session_id)
        if agent_id is not None:
            return self.list_events_for_agent(agent_id)
        rows = self._fetch_all(
            f"{_SELECT_EVENT_SQL} ORDER BY occurred_at ASC, storage_order ASC",
            operation="list events",
        )
        return tuple(_row_to_event(row) for row in rows)

    def list_events_for_session(self, session_id: str, /) -> tuple[Event, ...]:
        rows = self._fetch_all(
            f"{_SELECT_EVENT_SQL} WHERE session_id = ? ORDER BY occurred_at ASC, storage_order ASC",
            (session_id,),
            operation="list session events",
        )
        return tuple(_row_to_event(row) for row in rows)

    def get_latest_event_for_session(self, session_id: str, /) -> Event | None:
        """Return the most recent event for a session, or None."""
        row = self._fetch_one(
            (
                f"{_SELECT_EVENT_SQL} WHERE session_id = ? "
                "ORDER BY occurred_at DESC, storage_order DESC LIMIT 1"
            ),
            (session_id,),
            operation="get latest session event",
        )
        return None if row is None else _row_to_event(row)

    def list_events_for_agent(self, agent_id: str, /) -> tuple[Event, ...]:
        rows = self._fetch_all(
            f"{_SELECT_EVENT_SQL} WHERE agent_id = ? ORDER BY occurred_at ASC, storage_order ASC",
            (agent_id,),
            operation="list agent events",
        )
        return tuple(_row_to_event(row) for row in rows)

    def append_log_chunks(self, chunks: Sequence[LogChunk], /) -> None:
        if not chunks:
            return
        self._execute_many(
            _INSERT_LOG_CHUNK_SQL,
            tuple(self._log_chunk_params(chunk) for chunk in chunks),
            operation="append log chunks",
        )

    def list_log_chunks(self, session_id: str, /) -> tuple[LogChunk, ...]:
        rows = self._fetch_all(
            (
                f"{_SELECT_LOG_CHUNK_SQL} WHERE session_id = ? "
                "ORDER BY sequence_no ASC, captured_at ASC, storage_order ASC"
            ),
            (session_id,),
            operation="list log chunks",
        )
        return tuple(_row_to_log_chunk(row) for row in rows)

    def get_latest_log_chunk(self, session_id: str, /) -> LogChunk | None:
        """Return the most recent log chunk for a session, or None."""
        row = self._fetch_one(
            (
                f"{_SELECT_LOG_CHUNK_SQL} WHERE session_id = ? "
                "ORDER BY sequence_no DESC, captured_at DESC, storage_order DESC LIMIT 1"
            ),
            (session_id,),
            operation="get latest log chunk",
        )
        return None if row is None else _row_to_log_chunk(row)

    def get_latest_session_for_agent(self, agent_id: str, /) -> Session | None:
        """Return the most recent session for an agent, or None."""
        row = self._fetch_one(
            (f"{_SELECT_SESSION_SQL} WHERE agent_id = ? ORDER BY created_at DESC, id DESC LIMIT 1"),
            (agent_id,),
            operation="get latest session for agent",
        )
        return None if row is None else _row_to_session(row)

    def count_sessions_for_agent(self, agent_id: str, /) -> int:
        """Return the number of sessions for an agent."""
        row = self._fetch_one(
            "SELECT COUNT(*) FROM sessions WHERE agent_id = ?",
            (agent_id,),
            operation="count sessions for agent",
        )
        return int(row[0]) if row else 0

    def get_open_session_for_agent(self, agent_id: str, /) -> Session | None:
        """Return the currently open (no ended_at) session for an agent, or None."""
        row = self._fetch_one(
            (
                f"{_SELECT_SESSION_SQL} WHERE agent_id = ? AND ended_at IS NULL "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            (agent_id,),
            operation="get open session for agent",
        )
        return None if row is None else _row_to_session(row)

    def list_recent_log_chunks(
        self, session_id: str, /, *, limit: int = 20
    ) -> tuple[LogChunk, ...]:
        """Return the most recent *limit* log chunks for a session, in chronological order."""
        rows = self._fetch_all(
            (
                "SELECT * FROM ("
                f"SELECT storage_order, {', '.join(_LOG_CHUNK_COLUMNS)} FROM log_chunks "
                "WHERE session_id = ? "
                "ORDER BY sequence_no DESC, captured_at DESC, storage_order DESC "
                f"LIMIT ?) ORDER BY sequence_no ASC, captured_at ASC, storage_order ASC"
            ),
            (session_id, limit),
            operation="list recent log chunks",
        )
        return tuple(_row_to_log_chunk(row) for row in rows)

    def list_log_chunks_for_agent(self, agent_id: str, /) -> tuple[LogChunk, ...]:
        rows = self._fetch_all(
            (
                f"{_SELECT_LOG_CHUNK_SQL} WHERE agent_id = ? "
                "ORDER BY sequence_no ASC, captured_at ASC, storage_order ASC"
            ),
            (agent_id,),
            operation="list agent log chunks",
        )
        return tuple(_row_to_log_chunk(row) for row in rows)

    def get_log_chunk(self, log_chunk_id: str, /) -> LogChunk | None:
        row = self._fetch_one(
            f"{_SELECT_LOG_CHUNK_SQL} WHERE id = ?",
            (log_chunk_id,),
            operation="get log chunk",
        )
        return None if row is None else _row_to_log_chunk(row)

    def upsert_session_context(self, context: SessionContextRecord, /) -> None:
        self._execute_write(
            _UPSERT_SESSION_CONTEXT_SQL,
            self._session_context_params(context),
            operation="upsert session context",
        )

    def get_session_context(self, session_id: str, /) -> SessionContextRecord | None:
        row = self._fetch_one(
            f"{_SELECT_SESSION_CONTEXT_SQL} WHERE session_id = ?",
            (session_id,),
            operation="get session context",
        )
        return None if row is None else _row_to_session_context(row)

    def get_session_context_by_tmux_pane_id(
        self,
        tmux_pane_id: str,
        /,
    ) -> SessionContextRecord | None:
        row = self._fetch_one(
            f"""
            {_SELECT_SESSION_CONTEXT_SQL}
            WHERE tmux_pane_id = ?
            ORDER BY updated_at DESC, session_id DESC
            LIMIT 1
            """,
            (tmux_pane_id,),
            operation="get session context by tmux pane",
        )
        return None if row is None else _row_to_session_context(row)

    def list_session_contexts_for_worktree(
        self,
        worktree_id: str,
        /,
    ) -> tuple[SessionContextRecord, ...]:
        rows = self._fetch_all(
            (
                f"{_SELECT_SESSION_CONTEXT_SQL} WHERE worktree_id = ? "
                "ORDER BY updated_at DESC, session_id DESC"
            ),
            (worktree_id,),
            operation="list session contexts for worktree",
        )
        return tuple(_row_to_session_context(row) for row in rows)

    def set_setting(self, key: str, value: JsonValue, /) -> None:
        normalized_key = ensure_non_empty_text(key, field_name="key")
        value_json = _serialize_json(value, key_name=normalized_key)
        self._execute_write(
            """
            INSERT INTO settings (key, value_json, updated_at)
            VALUES (:key, :value_json, :updated_at)
            ON CONFLICT(key) DO UPDATE SET
                value_json = excluded.value_json,
                updated_at = excluded.updated_at
            """,
            {
                "key": normalized_key,
                "value_json": value_json,
                "updated_at": _serialize_datetime(utc_now()),
            },
            operation="set setting",
        )

    def get_setting(self, key: str, /) -> JsonValue | None:
        normalized_key = ensure_non_empty_text(key, field_name="key")
        row = self._fetch_one(
            "SELECT value_json FROM settings WHERE key = ?",
            (normalized_key,),
            operation="get setting",
        )
        if row is None:
            return None
        return _deserialize_json(
            _require_text(row, "value_json"),
            context=f"setting {normalized_key!r}",
        )

    def delete_setting(self, key: str, /) -> bool:
        normalized_key = ensure_non_empty_text(key, field_name="key")
        return self._execute_delete(
            "DELETE FROM settings WHERE key = ?",
            (normalized_key,),
            operation="delete setting",
        )

    def set_cache_entry(
        self,
        namespace: str,
        key: str,
        value: JsonValue,
        /,
        *,
        expires_at: datetime | None = None,
    ) -> None:
        normalized_namespace = ensure_non_empty_text(namespace, field_name="namespace")
        normalized_key = ensure_non_empty_text(key, field_name="key")
        self._execute_write(
            """
            INSERT INTO cache_entries (namespace, key, value_json, updated_at, expires_at)
            VALUES (:namespace, :key, :value_json, :updated_at, :expires_at)
            ON CONFLICT(namespace, key) DO UPDATE SET
                value_json = excluded.value_json,
                updated_at = excluded.updated_at,
                expires_at = excluded.expires_at
            """,
            {
                "namespace": normalized_namespace,
                "key": normalized_key,
                "value_json": _serialize_json(
                    value,
                    key_name=f"{normalized_namespace}:{normalized_key}",
                ),
                "updated_at": _serialize_datetime(utc_now()),
                "expires_at": _serialize_optional_datetime(expires_at),
            },
            operation="set cache entry",
        )

    def get_cache_entry(self, namespace: str, key: str, /) -> JsonValue | None:
        normalized_namespace = ensure_non_empty_text(namespace, field_name="namespace")
        normalized_key = ensure_non_empty_text(key, field_name="key")
        row = self._fetch_one(
            """
            SELECT value_json, expires_at
            FROM cache_entries
            WHERE namespace = ? AND key = ?
            """,
            (normalized_namespace, normalized_key),
            operation="get cache entry",
        )
        if row is None:
            return None
        expires_at = _optional_datetime(row, "expires_at")
        if expires_at is not None and expires_at <= utc_now():
            return None
        return _deserialize_json(
            _require_text(row, "value_json"),
            context=f"cache entry {normalized_namespace!r}:{normalized_key!r}",
        )

    def delete_cache_entry(self, namespace: str, key: str, /) -> bool:
        normalized_namespace = ensure_non_empty_text(namespace, field_name="namespace")
        normalized_key = ensure_non_empty_text(key, field_name="key")
        return self._execute_delete(
            "DELETE FROM cache_entries WHERE namespace = ? AND key = ?",
            (normalized_namespace, normalized_key),
            operation="delete cache entry",
        )

    def purge_expired_cache(self, /, *, now: datetime | None = None) -> int:
        cutoff = utc_now() if now is None else ensure_aware_datetime(now, field_name="now")
        try:
            with self._transaction(operation="purge expired cache") as connection:
                cursor = connection.execute(
                    "DELETE FROM cache_entries WHERE expires_at IS NOT NULL AND expires_at <= ?",
                    (_serialize_datetime(cutoff),),
                )
        except sqlite3.Error as exc:
            raise PersistenceError("failed to purge expired cache") from exc
        return cursor.rowcount

    def _connect(
        self,
        database_path: PathLike | None,
        *,
        check_same_thread: bool = True,
    ) -> tuple[Path, sqlite3.Connection]:
        errors: list[str] = []
        for candidate in self._database_candidates(database_path):
            try:
                candidate.parent.mkdir(parents=True, exist_ok=True)
                connection = sqlite3.connect(
                    candidate,
                    check_same_thread=check_same_thread,
                )
                connection.row_factory = sqlite3.Row
                for pragma in _PRAGMAS:
                    connection.execute(pragma)
                return candidate, connection
            except (OSError, sqlite3.Error) as exc:
                errors.append(f"{candidate}: {exc}")
        msg = "unable to open sqlite database; tried " + "; ".join(errors)
        raise PersistenceError(msg)

    def _database_candidates(self, database_path: PathLike | None) -> tuple[Path, ...]:
        if database_path is not None:
            return (Path(database_path).expanduser().resolve(strict=False),)
        candidates = [
            self._config.paths.database_path,
            self._config.paths.fallback_database_path,
        ]
        deduplicated: list[Path] = []
        for candidate in candidates:
            if candidate not in deduplicated:
                deduplicated.append(candidate)
        return tuple(deduplicated)

    def _bootstrap_migrations_table(self) -> None:
        try:
            with self._connection:
                self._connection.execute(_CREATE_MIGRATIONS_TABLE_SQL)
        except sqlite3.Error as exc:
            raise PersistenceError("failed to bootstrap migrations table") from exc

    def _run_migrations(self) -> None:
        applied = set(self.applied_migrations())
        for version, migration_name, migration_sql in _migration_files():
            if migration_name in applied:
                continue
            self._apply_migration(version, migration_name, migration_sql)

    def _apply_migration(self, version: int, migration_name: str, migration_sql: str) -> None:
        applied_at = _serialize_datetime(utc_now())
        script = "\n".join(
            (
                "BEGIN IMMEDIATE;",
                migration_sql,
                "INSERT INTO migrations (version, applied_at) VALUES "
                f"({_sql_quote(migration_name)}, {_sql_quote(applied_at)});",
                f"PRAGMA user_version = {version};",
                "COMMIT;",
            )
        )
        try:
            self._connection.executescript(script)
        except sqlite3.Error as exc:
            msg = f"failed to apply migration {migration_name}"
            raise PersistenceError(msg) from exc

    def _fetch_pragma(self, pragma_name: str) -> sqlite3.Row:
        try:
            row = self._connection.execute(f"PRAGMA {pragma_name}").fetchone()
        except sqlite3.Error as exc:
            msg = f"failed to read sqlite pragma {pragma_name}"
            raise PersistenceError(msg) from exc
        if row is None:
            msg = f"sqlite pragma {pragma_name} returned no rows"
            raise PersistenceError(msg)
        return cast(sqlite3.Row, row)

    @contextmanager
    def _transaction(self, *, operation: str) -> Iterator[sqlite3.Connection]:
        try:
            with self._connection:
                yield self._connection
        except sqlite3.Error as exc:
            msg = f"failed to {operation}"
            raise PersistenceError(msg) from exc

    def _execute_write(
        self,
        sql: str,
        params: Mapping[str, object],
        *,
        operation: str,
    ) -> None:
        with self._transaction(operation=operation) as connection:
            connection.execute(sql, params)

    def _execute_many(
        self,
        sql: str,
        params: Sequence[Mapping[str, object]],
        *,
        operation: str,
    ) -> None:
        with self._transaction(operation=operation) as connection:
            connection.executemany(sql, params)

    def _execute_delete(
        self,
        sql: str,
        params: Sequence[object],
        *,
        operation: str,
    ) -> bool:
        with self._transaction(operation=operation) as connection:
            cursor = connection.execute(sql, params)
        return cursor.rowcount > 0

    def _fetch_one(
        self,
        sql: str,
        params: Sequence[object] = (),
        *,
        operation: str,
    ) -> sqlite3.Row | None:
        try:
            return cast(sqlite3.Row | None, self._connection.execute(sql, params).fetchone())
        except sqlite3.Error as exc:
            msg = f"failed to {operation}"
            raise PersistenceError(msg) from exc

    def _fetch_all(
        self,
        sql: str,
        params: Sequence[object] = (),
        *,
        operation: str,
    ) -> tuple[sqlite3.Row, ...]:
        try:
            rows = self._connection.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            msg = f"failed to {operation}"
            raise PersistenceError(msg) from exc
        return tuple(cast(Sequence[sqlite3.Row], rows))

    def _agent_params(self, agent: Agent) -> dict[str, object]:
        return {
            "id": agent.id,
            "name": agent.name,
            "backend": agent.backend,
            "tmux_session_name": agent.tmux_session_name,
            "tmux_window_id": agent.tmux_window_id,
            "tmux_window_name": agent.tmux_window_name,
            "tmux_pane_id": agent.tmux_pane_id,
            "pane_tty": agent.pane_tty,
            "cwd": agent.cwd,
            "repo_root": agent.repo_root,
            "worktree_path": agent.worktree_path,
            "branch": agent.branch,
            "task_title": agent.task_title,
            "task_summary": agent.task_summary,
            "copilot_session_id": agent.copilot_session_id,
            "pid": agent.pid,
            "status": agent.status.value,
            "started_at": _serialize_datetime(agent.started_at),
            "last_activity_at": _serialize_optional_datetime(agent.last_activity_at),
            "last_seen_at": _serialize_datetime(agent.last_seen_at),
            "idle_seconds": agent.idle_seconds,
            "needs_attention": int(agent.needs_attention),
            "attention_reason": agent.attention_reason,
            "token_input": agent.token_input,
            "token_output": agent.token_output,
            "token_total": agent.token_total,
            "estimated_cost_usd": _serialize_optional_decimal(agent.estimated_cost_usd),
        }

    def _worktree_params(self, worktree: Worktree) -> dict[str, object]:
        return {
            "id": worktree.id,
            "repo_root": worktree.repo_root,
            "path": worktree.path,
            "branch": worktree.branch,
            "base_branch": worktree.base_branch,
            "is_main_worktree": int(worktree.is_main_worktree),
            "is_dirty": int(worktree.is_dirty),
            "ahead_count": worktree.ahead_count,
            "behind_count": worktree.behind_count,
            "locked": int(worktree.locked),
            "assigned_agent_id": worktree.assigned_agent_id,
            "created_at": _serialize_optional_datetime(worktree.created_at),
            "last_seen_at": _serialize_datetime(worktree.last_seen_at),
        }

    def _task_params(self, task: Task) -> dict[str, object]:
        return {
            "id": task.id,
            "title": task.title,
            "summary": task.summary,
            "description": task.description,
            "repo_root": task.repo_root,
            "priority": task.priority.value,
            "status": task.status.value,
            "assigned_agent_id": task.assigned_agent_id,
            "assigned_worktree_id": task.assigned_worktree_id,
            "created_at": _serialize_datetime(task.created_at),
            "started_at": _serialize_optional_datetime(task.started_at),
            "completed_at": _serialize_optional_datetime(task.completed_at),
            "notes": task.notes,
        }

    def _session_params(self, session: Session) -> dict[str, object]:
        return {
            "id": session.id,
            "agent_id": session.agent_id,
            "copilot_session_id": session.copilot_session_id,
            "task_title": session.task_title,
            "created_at": _serialize_datetime(session.created_at),
            "ended_at": _serialize_optional_datetime(session.ended_at),
            "exit_reason": session.exit_reason,
        }

    def _event_params(self, event: Event) -> dict[str, object]:
        return {
            "id": event.id,
            "occurred_at": _serialize_datetime(event.occurred_at),
            "agent_id": event.agent_id,
            "session_id": event.session_id,
            "kind": event.kind,
            "severity": event.severity,
            "payload_json": event.payload_json,
        }

    def _log_chunk_params(self, chunk: LogChunk) -> dict[str, object]:
        return {
            "id": chunk.id,
            "agent_id": chunk.agent_id,
            "session_id": chunk.session_id,
            "source": chunk.source,
            "sequence_no": chunk.sequence_no,
            "captured_at": _serialize_datetime(chunk.captured_at),
            "content": chunk.content,
        }

    def _session_context_params(self, context: SessionContextRecord) -> dict[str, object]:
        return {
            "session_id": context.session_id,
            "agent_id": context.agent_id,
            "worktree_id": context.worktree_id,
            "tmux_pane_id": context.tmux_pane_id,
            "pane_tty": context.pane_tty,
            "worktree_path": context.worktree_path,
            "copilot_session_id": context.copilot_session_id,
            "repo_root": context.repo_root,
            "branch": context.branch,
            "updated_at": _serialize_datetime(context.updated_at),
        }


def _migration_files() -> tuple[tuple[int, str, str], ...]:
    migrations: list[tuple[int, str, str]] = []
    for resource in files(_MIGRATIONS_PACKAGE).iterdir():
        if resource.name.startswith("__") or not resource.name.endswith(".sql"):
            continue
        prefix, _, _ = resource.name.partition("_")
        if not prefix.isdigit():
            msg = f"invalid migration file name: {resource.name}"
            raise PersistenceError(msg)
        migrations.append((int(prefix), resource.name, resource.read_text(encoding="utf-8")))
    return tuple(sorted(migrations, key=lambda item: item[0]))


def _serialize_datetime(value: datetime) -> str:
    return ensure_aware_datetime(value, field_name="value").isoformat()


def _serialize_optional_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _serialize_datetime(value)


def _serialize_optional_decimal(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")


def _serialize_json(value: JsonValue, *, key_name: str) -> str:
    try:
        return json.dumps(value, separators=(",", ":"), sort_keys=True)
    except TypeError as exc:
        msg = f"{key_name} is not JSON serializable"
        raise PersistenceError(msg) from exc


def _deserialize_json(value_json: str, *, context: str) -> JsonValue:
    try:
        return cast(JsonValue, json.loads(value_json))
    except json.JSONDecodeError as exc:
        msg = f"invalid JSON stored for {context}"
        raise PersistenceError(msg) from exc


def _sql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _row_to_agent(row: sqlite3.Row) -> Agent:
    agent_id = _require_text(row, "id")
    try:
        return Agent(
            id=agent_id,
            name=_require_text(row, "name"),
            backend=cast(AgentBackend, _require_text(row, "backend")),
            tmux_session_name=_require_text(row, "tmux_session_name"),
            tmux_window_id=_require_text(row, "tmux_window_id"),
            tmux_window_name=_optional_text(row, "tmux_window_name"),
            tmux_pane_id=_require_text(row, "tmux_pane_id"),
            pane_tty=_optional_text(row, "pane_tty"),
            cwd=_require_text(row, "cwd"),
            repo_root=_optional_text(row, "repo_root"),
            worktree_path=_optional_text(row, "worktree_path"),
            branch=_optional_text(row, "branch"),
            task_title=_optional_text(row, "task_title"),
            task_summary=_optional_text(row, "task_summary"),
            copilot_session_id=_optional_text(row, "copilot_session_id"),
            pid=_optional_int(row, "pid"),
            status=AgentStatus(_require_text(row, "status")),
            started_at=_require_datetime(row, "started_at"),
            last_activity_at=_optional_datetime(row, "last_activity_at"),
            last_seen_at=_require_datetime(row, "last_seen_at"),
            idle_seconds=_require_int(row, "idle_seconds"),
            needs_attention=_require_bool(row, "needs_attention"),
            attention_reason=_optional_text(row, "attention_reason"),
            token_input=_optional_int(row, "token_input"),
            token_output=_optional_int(row, "token_output"),
            token_total=_optional_int(row, "token_total"),
            estimated_cost_usd=_optional_decimal(row, "estimated_cost_usd"),
        )
    except ValueError as exc:
        msg = f"invalid agent status in row {agent_id!r}"
        raise PersistenceError(msg) from exc
    except DomainValidationError as exc:
        msg = f"invalid agent row for {agent_id!r}: {exc}"
        raise PersistenceError(msg) from exc


def _row_to_worktree(row: sqlite3.Row) -> Worktree:
    worktree_id = _require_text(row, "id")
    try:
        return Worktree(
            id=worktree_id,
            repo_root=_require_text(row, "repo_root"),
            path=_require_text(row, "path"),
            branch=_require_text(row, "branch"),
            base_branch=_optional_text(row, "base_branch"),
            is_main_worktree=_require_bool(row, "is_main_worktree"),
            is_dirty=_require_bool(row, "is_dirty"),
            ahead_count=_optional_int(row, "ahead_count"),
            behind_count=_optional_int(row, "behind_count"),
            locked=_require_bool(row, "locked"),
            assigned_agent_id=_optional_text(row, "assigned_agent_id"),
            created_at=_optional_datetime(row, "created_at"),
            last_seen_at=_require_datetime(row, "last_seen_at"),
        )
    except DomainValidationError as exc:
        msg = f"invalid worktree row for {worktree_id!r}: {exc}"
        raise PersistenceError(msg) from exc


def _row_to_replay_annotation(row: sqlite3.Row) -> ReplayAnnotation:
    annotation_id = _require_text(row, "id")
    kind_value = _require_text(row, "kind")
    if kind_value not in ("bookmark", "note"):
        msg = f"invalid replay annotation kind in row {annotation_id!r}: {kind_value!r}"
        raise PersistenceError(msg)
    try:
        return ReplayAnnotation(
            id=annotation_id,
            session_id=_require_text(row, "session_id"),
            ordinal=_require_int(row, "ordinal"),
            created_at=_require_datetime(row, "created_at"),
            kind=cast(ReplayAnnotationKind, kind_value),
            body=_require_text_allow_empty(row, "body"),
        )
    except (DomainValidationError, ValueError) as exc:
        msg = f"invalid replay annotation row for {annotation_id!r}: {exc}"
        raise PersistenceError(msg) from exc


def _row_to_task(row: sqlite3.Row) -> Task:
    task_id = _require_text(row, "id")
    try:
        return Task(
            id=task_id,
            title=_require_text(row, "title"),
            summary=_optional_text(row, "summary"),
            description=_optional_text(row, "description"),
            repo_root=_optional_text(row, "repo_root"),
            priority=TaskPriority(_require_text(row, "priority")),
            status=TaskStatus(_require_text(row, "status")),
            assigned_agent_id=_optional_text(row, "assigned_agent_id"),
            assigned_worktree_id=_optional_text(row, "assigned_worktree_id"),
            created_at=_require_datetime(row, "created_at"),
            started_at=_optional_datetime(row, "started_at"),
            completed_at=_optional_datetime(row, "completed_at"),
            notes=_optional_text(row, "notes"),
        )
    except ValueError as exc:
        msg = f"invalid task enum in row {task_id!r}"
        raise PersistenceError(msg) from exc
    except DomainValidationError as exc:
        msg = f"invalid task row for {task_id!r}: {exc}"
        raise PersistenceError(msg) from exc


def _row_to_session(row: sqlite3.Row) -> Session:
    session_id = _require_text(row, "id")
    try:
        return Session(
            id=session_id,
            agent_id=_require_text(row, "agent_id"),
            copilot_session_id=_optional_text(row, "copilot_session_id"),
            task_title=_optional_text(row, "task_title"),
            created_at=_require_datetime(row, "created_at"),
            ended_at=_optional_datetime(row, "ended_at"),
            exit_reason=_optional_text(row, "exit_reason"),
        )
    except DomainValidationError as exc:
        msg = f"invalid session row for {session_id!r}: {exc}"
        raise PersistenceError(msg) from exc


def _row_to_event(row: sqlite3.Row) -> Event:
    event_id = _require_text(row, "id")
    try:
        return Event(
            id=event_id,
            occurred_at=_require_datetime(row, "occurred_at"),
            agent_id=_optional_text(row, "agent_id"),
            session_id=_optional_text(row, "session_id"),
            kind=_require_text(row, "kind"),
            severity=cast(EventSeverity, _require_text(row, "severity")),
            payload_json=_require_text(row, "payload_json"),
        )
    except DomainValidationError as exc:
        msg = f"invalid event row for {event_id!r}: {exc}"
        raise PersistenceError(msg) from exc


def _row_to_log_chunk(row: sqlite3.Row) -> LogChunk:
    log_chunk_id = _require_text(row, "id")
    try:
        return LogChunk(
            id=log_chunk_id,
            agent_id=_require_text(row, "agent_id"),
            session_id=_optional_text(row, "session_id"),
            source=cast(LogSource, _require_text(row, "source")),
            sequence_no=_require_int(row, "sequence_no"),
            captured_at=_require_datetime(row, "captured_at"),
            content=_require_text(row, "content"),
        )
    except DomainValidationError as exc:
        msg = f"invalid log chunk row for {log_chunk_id!r}: {exc}"
        raise PersistenceError(msg) from exc


def _row_to_session_context(row: sqlite3.Row) -> SessionContextRecord:
    session_id = _require_text(row, "session_id")
    try:
        return SessionContextRecord(
            session_id=session_id,
            agent_id=_optional_text(row, "agent_id"),
            worktree_id=_optional_text(row, "worktree_id"),
            tmux_pane_id=_optional_text(row, "tmux_pane_id"),
            pane_tty=_optional_text(row, "pane_tty"),
            worktree_path=_optional_text(row, "worktree_path"),
            copilot_session_id=_optional_text(row, "copilot_session_id"),
            repo_root=_optional_text(row, "repo_root"),
            branch=_optional_text(row, "branch"),
            updated_at=_require_datetime(row, "updated_at"),
        )
    except DomainValidationError as exc:
        msg = f"invalid session context row for {session_id!r}: {exc}"
        raise PersistenceError(msg) from exc


def _row_value(row: sqlite3.Row, column: str) -> object:
    if column not in set(row.keys()):
        msg = f"missing sqlite column: {column}"
        raise PersistenceError(msg)
    return row[column]


def _require_text(row: sqlite3.Row, column: str) -> str:
    value = _row_value(row, column)
    if not isinstance(value, str):
        msg = f"expected text in column {column}, got {type(value).__name__}"
        raise PersistenceError(msg)
    if not value.strip():
        msg = f"column {column} must not be empty"
        raise PersistenceError(msg)
    return value


def _require_text_allow_empty(row: sqlite3.Row, column: str) -> str:
    value = _row_value(row, column)
    if not isinstance(value, str):
        msg = f"expected text in column {column}, got {type(value).__name__}"
        raise PersistenceError(msg)
    return value


def _optional_text(row: sqlite3.Row, column: str) -> str | None:
    value = _row_value(row, column)
    if value is None:
        return None
    if not isinstance(value, str):
        msg = f"expected nullable text in column {column}, got {type(value).__name__}"
        raise PersistenceError(msg)
    if not value.strip():
        msg = f"column {column} must not be blank when present"
        raise PersistenceError(msg)
    return value


def _require_int(row: sqlite3.Row, column: str) -> int:
    value = _row_value(row, column)
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"expected integer in column {column}, got {type(value).__name__}"
        raise PersistenceError(msg)
    return value


def _optional_int(row: sqlite3.Row, column: str) -> int | None:
    value = _row_value(row, column)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"expected nullable integer in column {column}, got {type(value).__name__}"
        raise PersistenceError(msg)
    return value


def _require_bool(row: sqlite3.Row, column: str) -> bool:
    value = _row_value(row, column)
    if value not in (0, 1):
        msg = f"expected boolean sentinel in column {column}, got {value!r}"
        raise PersistenceError(msg)
    return bool(value)


def _require_datetime(row: sqlite3.Row, column: str) -> datetime:
    value = _row_value(row, column)
    if not isinstance(value, str):
        msg = f"expected ISO datetime text in column {column}, got {type(value).__name__}"
        raise PersistenceError(msg)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        msg = f"invalid ISO datetime in column {column}: {value!r}"
        raise PersistenceError(msg) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        msg = f"datetime column {column} must be timezone-aware"
        raise PersistenceError(msg)
    return parsed.astimezone(UTC)


def _optional_datetime(row: sqlite3.Row, column: str) -> datetime | None:
    value = _row_value(row, column)
    if value is None:
        return None
    if not isinstance(value, str):
        msg = f"expected nullable ISO datetime text in column {column}, got {type(value).__name__}"
        raise PersistenceError(msg)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        msg = f"invalid ISO datetime in column {column}: {value!r}"
        raise PersistenceError(msg) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        msg = f"datetime column {column} must be timezone-aware"
        raise PersistenceError(msg)
    return parsed.astimezone(UTC)


def _optional_decimal(row: sqlite3.Row, column: str) -> Decimal | None:
    value = _row_value(row, column)
    if value is None:
        return None
    if not isinstance(value, str | int | float):
        msg = (
            f"expected nullable decimal-compatible value in column {column}, "
            f"got {type(value).__name__}"
        )
        raise PersistenceError(msg)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        msg = f"invalid decimal in column {column}: {value!r}"
        raise PersistenceError(msg) from exc
