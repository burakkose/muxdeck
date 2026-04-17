"""Domain model for operator-authored replay annotations."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from uuid import uuid4

from copilot_commander.domain.value_objects import (
    ensure_aware_datetime,
    ensure_non_empty_text,
    ensure_non_negative_int,
    utc_now,
)

ReplayAnnotationKind = Literal["bookmark", "note"]


def _new_annotation_id() -> str:
    return f"replay-annotation-{uuid4()}"


@dataclass(frozen=True, slots=True)
class ReplayAnnotation:
    """A bookmark or note attached to a single replay entry ordinal."""

    session_id: str
    ordinal: int
    kind: ReplayAnnotationKind
    id: str = field(default_factory=_new_annotation_id)
    created_at: datetime = field(default_factory=utc_now)
    body: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "id",
            ensure_non_empty_text(self.id, field_name="annotation id"),
        )
        object.__setattr__(
            self,
            "session_id",
            ensure_non_empty_text(self.session_id, field_name="annotation session_id"),
        )
        object.__setattr__(
            self,
            "ordinal",
            ensure_non_negative_int(self.ordinal, field_name="annotation ordinal"),
        )
        object.__setattr__(
            self,
            "created_at",
            ensure_aware_datetime(self.created_at, field_name="annotation created_at"),
        )
        if self.kind not in ("bookmark", "note"):
            msg = f"annotation kind must be 'bookmark' or 'note', got {self.kind!r}"
            raise ValueError(msg)
        # ``body`` may legitimately be empty for bookmarks; only normalise.
        object.__setattr__(self, "body", self.body or "")


__all__ = ["ReplayAnnotation", "ReplayAnnotationKind"]
