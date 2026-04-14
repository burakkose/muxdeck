from __future__ import annotations

from dataclasses import dataclass
import re


_AHEAD_BEHIND_TAB_PATTERN = re.compile(r"^\s*(?P<ahead>\d+)\s+(?P<behind>\d+)\s*$")
_AHEAD_PATTERN = re.compile(r"\bahead\s+(?P<count>\d+)\b", re.IGNORECASE)
_BEHIND_PATTERN = re.compile(r"\bbehind\s+(?P<count>\d+)\b", re.IGNORECASE)


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _decode_git_path(path: str) -> str:
    stripped = path.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] == '"':
        decoded = bytearray()
        inner = stripped[1:-1]
        index = 0
        escapes = {
            '"': ord('"'),
            "\\": ord("\\"),
            "a": 7,
            "b": 8,
            "f": 12,
            "n": 10,
            "r": 13,
            "t": 9,
            "v": 11,
        }
        while index < len(inner):
            character = inner[index]
            if character != "\\":
                decoded.extend(character.encode("utf-8"))
                index += 1
                continue
            index += 1
            if index >= len(inner):
                decoded.append(ord("\\"))
                break
            escaped = inner[index]
            if escaped in escapes:
                decoded.append(escapes[escaped])
                index += 1
                continue
            if escaped in "01234567":
                octal_digits = [escaped]
                index += 1
                while index < len(inner) and len(octal_digits) < 3 and inner[index] in "01234567":
                    octal_digits.append(inner[index])
                    index += 1
                decoded.append(int("".join(octal_digits), 8))
                continue
            decoded.extend(escaped.encode("utf-8"))
            index += 1
        return decoded.decode("utf-8", errors="replace")
    return stripped


def _split_git_rename_payload(payload: str) -> tuple[str, str] | None:
    in_quotes = False
    escaped = False
    for index, character in enumerate(payload):
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if character == '"':
            in_quotes = not in_quotes
            continue
        if not in_quotes and payload.startswith(" -> ", index):
            return payload[:index], payload[index + 4 :]
    return None


def _current_text(current: dict[str, str | bool | None], key: str) -> str | None:
    value = current.get(key)
    return value if isinstance(value, str) else None


@dataclass(frozen=True, slots=True)
class GitWorktreeRecord:
    path: str
    head_commit: str | None = None
    branch: str | None = None
    is_detached: bool = False
    is_bare: bool = False
    is_locked: bool = False
    lock_reason: str | None = None
    is_prunable: bool = False
    prunable_reason: str | None = None


@dataclass(frozen=True, slots=True)
class AheadBehindCounts:
    ahead: int = 0
    behind: int = 0
    recognized: bool = False


@dataclass(frozen=True, slots=True)
class GitStatusEntry:
    index_status: str
    worktree_status: str
    path: str
    original_path: str | None = None
    is_untracked: bool = False
    is_unmerged: bool = False


@dataclass(frozen=True, slots=True)
class GitStatusSummary:
    entries: tuple[GitStatusEntry, ...]
    ignored_lines: tuple[str, ...] = ()
    branch_line: str | None = None

    @property
    def is_dirty(self) -> bool:
        return bool(self.entries)


def parse_git_worktree_list_porcelain(output: str) -> tuple[GitWorktreeRecord, ...]:
    records: list[GitWorktreeRecord] = []
    current: dict[str, str | bool | None] = {}

    def flush() -> None:
        path = _normalize_optional_text(_current_text(current, "path"))
        if path is None:
            current.clear()
            return
        records.append(
            GitWorktreeRecord(
                path=path,
                head_commit=_normalize_optional_text(_current_text(current, "head_commit")),
                branch=_normalize_optional_text(_current_text(current, "branch")),
                is_detached=bool(current.get("is_detached", False)),
                is_bare=bool(current.get("is_bare", False)),
                is_locked=bool(current.get("is_locked", False)),
                lock_reason=_normalize_optional_text(_current_text(current, "lock_reason")),
                is_prunable=bool(current.get("is_prunable", False)),
                prunable_reason=_normalize_optional_text(_current_text(current, "prunable_reason")),
            )
        )
        current.clear()

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            flush()
            continue
        if raw_line.startswith("worktree "):
            flush()
            current["path"] = raw_line.removeprefix("worktree ").strip()
            continue
        if raw_line.startswith("HEAD "):
            current["head_commit"] = raw_line.removeprefix("HEAD ").strip()
            continue
        if raw_line.startswith("branch "):
            branch = raw_line.removeprefix("branch ").strip()
            current["branch"] = branch.removeprefix("refs/heads/")
            continue
        if raw_line == "detached":
            current["is_detached"] = True
            continue
        if raw_line == "bare":
            current["is_bare"] = True
            continue
        if raw_line.startswith("locked"):
            current["is_locked"] = True
            raw_reason = raw_line.removeprefix("locked").strip()
            current["lock_reason"] = _decode_git_path(raw_reason) if raw_reason else None
            continue
        if raw_line.startswith("prunable"):
            current["is_prunable"] = True
            raw_reason = raw_line.removeprefix("prunable").strip()
            current["prunable_reason"] = _decode_git_path(raw_reason) if raw_reason else None
    flush()
    return tuple(records)


def parse_ahead_behind(output: str) -> AheadBehindCounts:
    stripped = output.strip()
    if not stripped:
        return AheadBehindCounts()

    tab_match = _AHEAD_BEHIND_TAB_PATTERN.match(stripped)
    if tab_match is not None:
        return AheadBehindCounts(
            ahead=int(tab_match.group("ahead")),
            behind=int(tab_match.group("behind")),
            recognized=True,
        )

    ahead_match = _AHEAD_PATTERN.search(stripped)
    behind_match = _BEHIND_PATTERN.search(stripped)
    if ahead_match is None and behind_match is None:
        return AheadBehindCounts()
    return AheadBehindCounts(
        ahead=int(ahead_match.group("count")) if ahead_match is not None else 0,
        behind=int(behind_match.group("count")) if behind_match is not None else 0,
        recognized=True,
    )


def parse_git_status_porcelain(output: str) -> GitStatusSummary:
    entries: list[GitStatusEntry] = []
    ignored_lines: list[str] = []
    branch_line: str | None = None

    for raw_line in output.splitlines():
        if not raw_line.strip():
            continue
        if raw_line.startswith("## "):
            branch_line = raw_line
            continue
        if len(raw_line) < 3:
            ignored_lines.append(raw_line)
            continue
        status = raw_line[:2]
        if raw_line[2] != " ":
            ignored_lines.append(raw_line)
            continue
        payload = raw_line[3:]
        if status == "!!":
            ignored_lines.append(raw_line)
            continue
        original_path: str | None = None
        path = payload
        if status[0] in {"R", "C"}:
            renamed_paths = _split_git_rename_payload(payload)
        else:
            renamed_paths = None
        if renamed_paths is not None:
            original_raw, renamed_raw = renamed_paths
            original_path = _decode_git_path(original_raw)
            path = renamed_raw
        entries.append(
            GitStatusEntry(
                index_status=status[0],
                worktree_status=status[1],
                path=_decode_git_path(path),
                original_path=original_path,
                is_untracked=status == "??",
                is_unmerged="U" in status or status in {"AA", "DD"},
            )
        )

    return GitStatusSummary(
        entries=tuple(entries),
        ignored_lines=tuple(ignored_lines),
        branch_line=branch_line,
    )
