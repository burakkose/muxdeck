"""Application-layer service for replay annotations."""

from __future__ import annotations

from copilot_commander.adapters.sqlite_replay_annotations import (
    ReplayAnnotationsRepository,
)
from copilot_commander.domain.replay_annotations import ReplayAnnotation


class AnnotationsService:
    """Wraps :class:`ReplayAnnotationsRepository` with domain operations."""

    def __init__(self, repository: ReplayAnnotationsRepository) -> None:
        self._repository = repository

    def list_for_session(self, session_id: str) -> tuple[ReplayAnnotation, ...]:
        return self._repository.list_for_session(session_id)

    def toggle_bookmark(self, session_id: str, ordinal: int) -> bool:
        return self._repository.toggle_bookmark(session_id, ordinal)

    def add_note(self, session_id: str, ordinal: int, body: str) -> ReplayAnnotation:
        annotation = ReplayAnnotation(
            session_id=session_id,
            ordinal=ordinal,
            kind="note",
            body=body,
        )
        self._repository.add(annotation)
        return annotation

    def delete(self, annotation_id: str) -> bool:
        return self._repository.delete(annotation_id)

    def update_note_body(self, annotation_id: str, body: str) -> bool:
        return self._repository.update_note_body(annotation_id, body)


__all__ = ["AnnotationsService"]
