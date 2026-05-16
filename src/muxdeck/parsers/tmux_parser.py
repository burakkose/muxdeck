from __future__ import annotations

from dataclasses import dataclass
from typing import Final

_KEY_ALIASES: Final[dict[str, str]] = {
    "cwd": "pane_current_path",
    "current_command": "pane_current_command",
    "current_path": "pane_current_path",
    "pane_path": "pane_current_path",
    "session": "session_name",
    "tty": "pane_tty",
    "window": "window_name",
}


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in {'"', "'"}:
        return normalized[1:-1]
    return normalized


def _parse_optional_int(value: str | None) -> int | None:
    normalized = _normalize_optional_text(value)
    if normalized is None:
        return None
    try:
        return int(normalized)
    except ValueError:
        return None


def _parse_optional_bool(value: str | None) -> bool | None:
    normalized = _normalize_optional_text(value)
    if normalized is None:
        return None
    lowered = normalized.casefold()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return None


def _split_segments(line: str) -> list[str]:
    if "\t" in line:
        return [segment.strip() for segment in line.split("\t") if segment.strip()]
    if " | " in line:
        return [segment.strip() for segment in line.split(" | ") if segment.strip()]
    if "|" in line and line.count("=") > 1:
        return [segment.strip() for segment in line.split("|") if segment.strip()]
    return [line.strip()]


def _parse_key_value_line(line: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for segment in _split_segments(line):
        if "=" not in segment:
            continue
        key, raw_value = segment.split("=", maxsplit=1)
        normalized_key = _KEY_ALIASES.get(key.strip(), key.strip())
        fields[normalized_key] = raw_value.strip()
    return fields


@dataclass(frozen=True, slots=True)
class TmuxPaneRecord:
    pane_id: str
    session_name: str | None = None
    session_id: str | None = None
    window_id: str | None = None
    window_index: int | None = None
    window_name: str | None = None
    window_active: bool | None = None
    pane_index: int | None = None
    pane_active: bool | None = None
    pane_pid: int | None = None
    pane_tty: str | None = None
    pane_current_path: str | None = None
    pane_current_command: str | None = None
    pane_activity: int | None = None
    raw_fields: dict[str, str] | None = None


@dataclass(frozen=True, slots=True)
class TmuxListPanesParseResult:
    panes: tuple[TmuxPaneRecord, ...]
    ignored_lines: tuple[str, ...] = ()


def _parse_pane_activity(value: str | None) -> int | None:
    """Parse the tmux ``#{pane_activity}`` field into an epoch second.

    Returns ``None`` when the field is missing, unparseable, or equal
    to ``0``. The cached-discovery optimization in
    :mod:`muxdeck.services.discovery_service` keys off "non-None and
    unchanged"; a literal ``0`` on legacy tmux builds (or panes that
    have never produced output) must therefore force a fresh capture
    rather than freeze the cache.
    """
    parsed = _parse_optional_int(value)
    if parsed is None or parsed <= 0:
        return None
    return parsed


def parse_tmux_list_panes_output(output: str) -> TmuxListPanesParseResult:
    panes: list[TmuxPaneRecord] = []
    ignored_lines: list[str] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        fields = _parse_key_value_line(line)
        pane_id = _normalize_optional_text(fields.get("pane_id"))
        if pane_id is None:
            ignored_lines.append(raw_line)
            continue
        panes.append(
            TmuxPaneRecord(
                pane_id=pane_id,
                session_name=_normalize_optional_text(fields.get("session_name")),
                session_id=_normalize_optional_text(fields.get("session_id")),
                window_id=_normalize_optional_text(fields.get("window_id")),
                window_index=_parse_optional_int(fields.get("window_index")),
                window_name=_normalize_optional_text(fields.get("window_name")),
                window_active=_parse_optional_bool(fields.get("window_active")),
                pane_index=_parse_optional_int(fields.get("pane_index")),
                pane_active=_parse_optional_bool(fields.get("pane_active")),
                pane_pid=_parse_optional_int(fields.get("pane_pid")),
                pane_tty=_normalize_optional_text(fields.get("pane_tty")),
                pane_current_path=_normalize_optional_text(fields.get("pane_current_path")),
                pane_current_command=_normalize_optional_text(fields.get("pane_current_command")),
                pane_activity=_parse_pane_activity(fields.get("pane_activity")),
                raw_fields=dict(fields),
            )
        )
    return TmuxListPanesParseResult(panes=tuple(panes), ignored_lines=tuple(ignored_lines))
