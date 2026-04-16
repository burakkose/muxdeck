from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4

from copilot_commander.domain.value_objects import ensure_non_empty_text


def _new_task_identifier() -> str:
    return f"task-{uuid4()}"


@dataclass(frozen=True, slots=True, order=True)
class TaskId:
    value: str = field(default_factory=_new_task_identifier)

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", ensure_non_empty_text(self.value, field_name="task_id"))

    @classmethod
    def generate(cls) -> TaskId:
        return cls()

    def __str__(self) -> str:
        return self.value


def ensure_task_id(value: TaskId | str, *, field_name: str = "task_id") -> TaskId:
    if isinstance(value, TaskId):
        return value
    return TaskId(ensure_non_empty_text(value, field_name=field_name))
