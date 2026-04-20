"""Repository adapter for replay annotations.

Defines the ``ReplayAnnotationsRepository`` Protocol consumed by the
application layer and a SQLite-backed implementation that delegates SQL
to the existing :class:`SQLiteStore` (where all schema-aware queries
live so migrations stay co-located with their tables).
"""

from __future__ import annotations

from typing import Protocol

from muxdeck.adapters.sqlite_store import SQLiteStore
from muxdeck.domain.replay_annotations import ReplayAnnotation


class ReplayAnnotationsRepository(Protocol):
    """Persistence boundary for operator-authored replay annotations."""

    def add(self, annotation: ReplayAnnotation, /) -> None: ...

    def delete(self, annotation_id: str, /) -> bool: ...

    def update_note_body(self, annotation_id: str, body: str, /) -> bool: ...

    def list_for_session(self, session_id: str, /) -> tuple[ReplayAnnotation, ...]: ...

    def toggle_bookmark(self, session_id: str, ordinal: int, /) -> bool: ...


class SqliteReplayAnnotationsRepository:
    """SQLite-backed :class:`ReplayAnnotationsRepository`."""

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def add(self, annotation: ReplayAnnotation, /) -> None:
        self._store.insert_replay_annotation(annotation)

    def delete(self, annotation_id: str, /) -> bool:
        return self._store.delete_replay_annotation(annotation_id)

    def update_note_body(self, annotation_id: str, body: str, /) -> bool:
        return self._store.update_replay_annotation_body(annotation_id, body)

    def list_for_session(self, session_id: str, /) -> tuple[ReplayAnnotation, ...]:
        return self._store.list_replay_annotations(session_id)

    def toggle_bookmark(self, session_id: str, ordinal: int, /) -> bool:
        existing = self._store.find_replay_bookmark(session_id, ordinal)
        if existing is not None:
            self._store.delete_replay_annotation(existing.id)
            return False
        self._store.insert_replay_annotation(
            ReplayAnnotation(
                session_id=session_id,
                ordinal=ordinal,
                kind="bookmark",
            )
        )
        return True


__all__ = [
    "ReplayAnnotationsRepository",
    "SqliteReplayAnnotationsRepository",
]
