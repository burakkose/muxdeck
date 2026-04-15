from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from copilot_commander.domain.value_objects import ensure_confidence

BoundaryKind = Literal["prompt_start", "prompt_end", "response_start", "response_end"]
BlockingKind = Literal[
    "waiting_for_confirmation",
    "merge_conflict",
    "authentication_issue",
    "rate_limit",
    "tool_failure",
]

_SESSION_PATTERNS = (
    (
        re.compile(
            r"\bcopilot[_ -]?session(?:\s+id)?\s*[:=#]\s*"
            r"(?P<value>[A-Za-z0-9][A-Za-z0-9._:-]{2,})",
            re.IGNORECASE,
        ),
        Decimal("0.9800"),
    ),
    (
        re.compile(
            r"\bsession(?:\s+id)?\s*[:=#]\s*"
            r"(?P<value>[A-Za-z0-9][A-Za-z0-9._:-]{2,})",
            re.IGNORECASE,
        ),
        Decimal("0.9000"),
    ),
    (re.compile(r'"session_id"\s*:\s*"(?P<value>[^"]+)"', re.IGNORECASE), Decimal("0.9800")),
)
_BOUNDARY_PATTERNS: tuple[tuple[BoundaryKind, re.Pattern[str], Decimal], ...] = (
    (
        "prompt_start",
        re.compile(r"^(?:>>>+\s*)?(?:prompt|user(?:\s+prompt)?|input)\s*:\s*", re.IGNORECASE),
        Decimal("0.9500"),
    ),
    ("prompt_end", re.compile(r"^(?:<<<+\s*)?end\s+prompt\b", re.IGNORECASE), Decimal("0.8500")),
    (
        "response_start",
        re.compile(
            r"^(?:>>>+\s*)?(?:response|assistant|copilot|output)\s*:\s*",
            re.IGNORECASE,
        ),
        Decimal("0.9500"),
    ),
    (
        "response_end",
        re.compile(r"^(?:<<<+\s*)?end\s+response\b", re.IGNORECASE),
        Decimal("0.8500"),
    ),
)
_BLOCKING_PATTERNS: dict[BlockingKind, tuple[re.Pattern[str], ...]] = {
    "waiting_for_confirmation": (
        re.compile(r"\bwaiting for confirmation\b", re.IGNORECASE),
        re.compile(r"\brequires confirmation\b", re.IGNORECASE),
        re.compile(r"\bconfirm (?:to )?continue\b", re.IGNORECASE),
        re.compile(r"\bpress [yn](?:/| or )[yn] to continue\b", re.IGNORECASE),
    ),
    "merge_conflict": (
        re.compile(r"\bmerge conflict\b(?!.*\bresolved\b)", re.IGNORECASE),
        re.compile(r"\bconflict markers\b", re.IGNORECASE),
        re.compile(r"\bcould not apply .*conflict", re.IGNORECASE),
    ),
    "authentication_issue": (
        re.compile(r"\bauthentication (?:failed|required|error|expired)\b", re.IGNORECASE),
        re.compile(r"\blogin required\b", re.IGNORECASE),
        re.compile(r"\bnot authenticated\b", re.IGNORECASE),
        re.compile(r"\binvalid token\b", re.IGNORECASE),
        re.compile(r"\bsign in\b", re.IGNORECASE),
    ),
    "rate_limit": (
        re.compile(r"\brate limit(?: exceeded| hit| reached)\b", re.IGNORECASE),
        re.compile(r"\btoo many requests\b", re.IGNORECASE),
        re.compile(r"\bquota exceeded\b", re.IGNORECASE),
        re.compile(r"\b429\b.*\b(?:rate limit|too many requests)\b", re.IGNORECASE),
    ),
    "tool_failure": (
        re.compile(r"\btool failed\b", re.IGNORECASE),
        re.compile(r"\bfailed to run tool\b", re.IGNORECASE),
        re.compile(r"\bcommand failed\b", re.IGNORECASE),
        re.compile(r"\bexit code\s+[1-9]\d*\b", re.IGNORECASE),
        re.compile(r"\bstderr:\b", re.IGNORECASE),
    ),
}
_ERROR_PATTERNS = (
    re.compile(r"(?:^|\b)error:", re.IGNORECASE),
    re.compile(r"(?:^|\b)fatal:", re.IGNORECASE),
    re.compile(r"\bexception\b", re.IGNORECASE),
    re.compile(r"^traceback \(most recent call last\):?$", re.IGNORECASE),
)
_COPILOT_UI_MARKER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("slash_commands", re.compile(r"/\s*commands\b")),
    ("enqueue_binding", re.compile(r"ctrl\+q\s+enqueue", re.IGNORECASE)),
    ("autopilot_prompt", re.compile(r"\bautopilot\s*·\s*/\s*commands\b")),
    ("esc_to_cancel", re.compile(r"\bEsc to cancel\b")),
    ("copilot_model", re.compile(r"\bClaude (?:Opus|Sonnet|Haiku)\b")),
    ("copilot_model", re.compile(r"\bGPT-\d", re.IGNORECASE)),
    ("copilot_model", re.compile(r"\bGemini\b", re.IGNORECASE)),
)
_ACTIVITY_PATTERNS: tuple[tuple[str, str, str], ...] = (
    # (regex, activity_template, category)
    # {0} is replaced with capture group 1
    (
        r"(?i)(?:Read file|Read(?:ing)?)[: ]+"
        r"[`'\"]?([^\s`'\"]{3,120})",
        "reading {0}",
        "file_read",
    ),
    (
        r"(?i)Read\(file_path=['\"]([^'\"]+)",
        "reading {0}",
        "file_read",
    ),
    (
        r"(?i)(?:Writ(?:e|ing|ten)|Edit(?:ing)?|Creat(?:e|ing))"
        r" (?:file[: ]+|to )?[`'\"]?([^\s`'\"]{3,120})",
        "writing {0}",
        "file_write",
    ),
    (
        r"(?i)(?:Run(?:ning)?|Exec(?:uting)?)"
        r"(?:\s+command)?[: ]+[`'\"]?(.{3,80}?)[`'\"]?\s*$",
        "running {0}",
        "command",
    ),
    (
        r"(?i)Bash\(.*?command=['\"](.{3,80}?)['\"]",
        "running {0}",
        "command",
    ),
    (
        r"(?i)^\s*(?:Think(?:ing)?|Plan(?:ning)?|Analyz(?:e|ing))"
        r"\.{0,3}\s*$",
        "thinking",
        "thinking",
    ),
    (
        r"(?i)(?:Search(?:ing)?|Grep(?:ping)?)"
        r"\(?.*?['\"]?(.{3,60}?)['\"]?\)?$",
        "searching {0}",
        "search",
    ),
    (
        r"(?i)(?:Tool|Using(?: tool)?):\s*(\w[\w_]{2,30})",
        "using tool {0}",
        "tool_use",
    ),
)
_INPUT_TOKENS_PATTERNS = (
    re.compile(r"\binput(?:_tokens?| tokens?)\s*[:=]\s*(?P<value>\d[\d,]*)", re.IGNORECASE),
    re.compile(r"\b(?P<value>\d[\d,]*)\s+input tokens?\b", re.IGNORECASE),
)
_OUTPUT_TOKENS_PATTERNS = (
    re.compile(r"\boutput(?:_tokens?| tokens?)\s*[:=]\s*(?P<value>\d[\d,]*)", re.IGNORECASE),
    re.compile(r"\b(?P<value>\d[\d,]*)\s+output tokens?\b", re.IGNORECASE),
)
_TOTAL_TOKENS_PATTERNS = (
    re.compile(r"\btotal(?:_tokens?| tokens?)\s*[:=]\s*(?P<value>\d[\d,]*)", re.IGNORECASE),
    re.compile(r"\b(?P<value>\d[\d,]*)\s+total tokens?\b", re.IGNORECASE),
)
_COST_PATTERNS = (
    re.compile(
        r"\bcost(?:_usd)?\s*[:=]\s*(?:(?P<currency>[A-Z]{3})\s*)?"
        r"\$?(?P<value>\d+(?:\.\d+)?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bestimated cost\b.*?(?:(?P<currency>[A-Z]{3})\s*)?"
        r"\$?(?P<value>\d+(?:\.\d+)?)",
        re.IGNORECASE,
    ),
)


def _parse_int_from_patterns(line: str, patterns: tuple[re.Pattern[str], ...]) -> int | None:
    for pattern in patterns:
        match = pattern.search(line)
        if match is not None:
            return int(match.group("value").replace(",", ""))
    return None


def _parse_cost(line: str) -> tuple[Decimal | None, str | None]:
    for pattern in _COST_PATTERNS:
        match = pattern.search(line)
        if match is not None:
            currency = match.group("currency")
            return Decimal(match.group("value")), currency.upper() if currency else "USD"
    return None, None


def _build_span(
    *,
    category: str,
    start_line: int,
    end_line: int,
    lines: list[str],
    confidence: Decimal,
) -> CopilotEvidenceSpan:
    return CopilotEvidenceSpan(
        category=category,
        start_line=start_line,
        end_line=end_line,
        text="\n".join(lines),
        confidence=confidence,
    )


def _usage_int(usage_data: dict[str, int | Decimal | str | None], key: str) -> int | None:
    value = usage_data.get(key)
    return value if isinstance(value, int) else None


def _usage_decimal(
    usage_data: dict[str, int | Decimal | str | None],
    key: str,
) -> Decimal | None:
    value = usage_data.get(key)
    return value if isinstance(value, Decimal) else None


def _usage_text(usage_data: dict[str, int | Decimal | str | None], key: str) -> str | None:
    value = usage_data.get(key)
    return value if isinstance(value, str) else None


@dataclass(frozen=True, slots=True)
class CopilotEvidenceSpan:
    category: str
    start_line: int
    end_line: int
    text: str
    confidence: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "category", self.category.strip())
        object.__setattr__(self, "text", self.text.strip())
        object.__setattr__(self, "confidence", ensure_confidence(self.confidence))
        if self.start_line < 1 or self.end_line < self.start_line:
            msg = "evidence span line numbers must be positive and ordered"
            raise ValueError(msg)
        if not self.category:
            msg = "evidence span category must not be empty"
            raise ValueError(msg)
        if not self.text:
            msg = "evidence span text must not be empty"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class CopilotSessionIdCandidate:
    value: str
    span: CopilotEvidenceSpan


@dataclass(frozen=True, slots=True)
class CopilotTranscriptBoundary:
    kind: BoundaryKind
    span: CopilotEvidenceSpan


@dataclass(frozen=True, slots=True)
class CopilotUsageSnapshot:
    span: CopilotEvidenceSpan
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cost: Decimal | None = None
    currency: str | None = None


@dataclass(frozen=True, slots=True)
class CopilotBlockingIssue:
    kind: BlockingKind
    span: CopilotEvidenceSpan


@dataclass(frozen=True, slots=True)
class CopilotErrorEvidence:
    message: str
    span: CopilotEvidenceSpan


@dataclass(frozen=True, slots=True)
class CopilotUIMarker:
    kind: str
    span: CopilotEvidenceSpan


@dataclass(frozen=True, slots=True)
class CopilotActivityMarker:
    """Detected agent activity from copilot output."""

    activity: str
    category: str
    span: CopilotEvidenceSpan


@dataclass(frozen=True, slots=True)
class CopilotOutputParseResult:
    session_ids: tuple[CopilotSessionIdCandidate, ...]
    boundaries: tuple[CopilotTranscriptBoundary, ...]
    usage_snapshots: tuple[CopilotUsageSnapshot, ...]
    blocking_issues: tuple[CopilotBlockingIssue, ...]
    errors: tuple[CopilotErrorEvidence, ...]
    ui_markers: tuple[CopilotUIMarker, ...]
    activity_markers: tuple[CopilotActivityMarker, ...] = ()
    evidence_spans: tuple[CopilotEvidenceSpan, ...] = ()


def parse_copilot_output(output: str) -> CopilotOutputParseResult:
    session_ids: list[CopilotSessionIdCandidate] = []
    boundaries: list[CopilotTranscriptBoundary] = []
    usage_snapshots: list[CopilotUsageSnapshot] = []
    blocking_issues: list[CopilotBlockingIssue] = []
    errors: list[CopilotErrorEvidence] = []
    ui_markers: list[CopilotUIMarker] = []
    evidence_spans: list[CopilotEvidenceSpan] = []

    usage_lines: list[str] = []
    usage_start_line: int | None = None
    usage_end_line = 0
    usage_data: dict[str, int | Decimal | str | None] = {}

    def flush_usage() -> None:
        nonlocal usage_lines, usage_start_line, usage_end_line, usage_data
        if usage_start_line is None or not usage_lines:
            usage_lines = []
            usage_start_line = None
            usage_end_line = 0
            usage_data = {}
            return
        filled_fields = sum(
            value is not None
            for value in (
                usage_data.get("input_tokens"),
                usage_data.get("output_tokens"),
                usage_data.get("total_tokens"),
                usage_data.get("cost"),
            )
        )
        confidence = Decimal("0.9000") if filled_fields >= 2 else Decimal("0.7800")
        span = _build_span(
            category="usage",
            start_line=usage_start_line,
            end_line=usage_end_line,
            lines=usage_lines,
            confidence=confidence,
        )
        usage_snapshots.append(
            CopilotUsageSnapshot(
                span=span,
                input_tokens=_usage_int(usage_data, "input_tokens"),
                output_tokens=_usage_int(usage_data, "output_tokens"),
                total_tokens=_usage_int(usage_data, "total_tokens"),
                cost=_usage_decimal(usage_data, "cost"),
                currency=_usage_text(usage_data, "currency"),
            )
        )
        evidence_spans.append(span)
        usage_lines = []
        usage_start_line = None
        usage_end_line = 0
        usage_data = {}

    for line_number, raw_line in enumerate(output.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            flush_usage()
            continue

        for pattern, confidence in _SESSION_PATTERNS:
            match = pattern.search(line)
            if match is None:
                continue
            span = _build_span(
                category="session_id",
                start_line=line_number,
                end_line=line_number,
                lines=[raw_line],
                confidence=confidence,
            )
            session_ids.append(CopilotSessionIdCandidate(value=match.group("value"), span=span))
            evidence_spans.append(span)
            break

        for kind, pattern, confidence in _BOUNDARY_PATTERNS:
            if pattern.search(line) is None:
                continue
            span = _build_span(
                category=kind,
                start_line=line_number,
                end_line=line_number,
                lines=[raw_line],
                confidence=confidence,
            )
            boundaries.append(CopilotTranscriptBoundary(kind=kind, span=span))
            evidence_spans.append(span)

        matched_usage = False
        input_tokens = _parse_int_from_patterns(line, _INPUT_TOKENS_PATTERNS)
        output_tokens = _parse_int_from_patterns(line, _OUTPUT_TOKENS_PATTERNS)
        total_tokens = _parse_int_from_patterns(line, _TOTAL_TOKENS_PATTERNS)
        cost, currency = _parse_cost(line)
        if any(value is not None for value in (input_tokens, output_tokens, total_tokens, cost)):
            matched_usage = True
            if usage_start_line is None:
                usage_start_line = line_number
            usage_end_line = line_number
            usage_lines.append(raw_line)
            if input_tokens is not None:
                usage_data["input_tokens"] = input_tokens
            if output_tokens is not None:
                usage_data["output_tokens"] = output_tokens
            if total_tokens is not None:
                usage_data["total_tokens"] = total_tokens
            if cost is not None:
                usage_data["cost"] = cost
            if currency is not None:
                usage_data["currency"] = currency
        elif usage_start_line is not None:
            flush_usage()

        for issue_kind, patterns in _BLOCKING_PATTERNS.items():
            if not any(pattern.search(line) for pattern in patterns):
                continue
            span = _build_span(
                category=f"blocking:{issue_kind}",
                start_line=line_number,
                end_line=line_number,
                lines=[raw_line],
                confidence=Decimal("0.9300"),
            )
            blocking_issues.append(CopilotBlockingIssue(kind=issue_kind, span=span))
            evidence_spans.append(span)

        if any(pattern.search(line) for pattern in _ERROR_PATTERNS):
            span = _build_span(
                category="error",
                start_line=line_number,
                end_line=line_number,
                lines=[raw_line],
                confidence=Decimal("0.9100"),
            )
            errors.append(CopilotErrorEvidence(message=line, span=span))
            evidence_spans.append(span)

        for marker_kind, marker_pattern in _COPILOT_UI_MARKER_PATTERNS:
            if marker_pattern.search(line) is None:
                continue
            span = _build_span(
                category=f"ui_marker:{marker_kind}",
                start_line=line_number,
                end_line=line_number,
                lines=[raw_line],
                confidence=Decimal("0.9200"),
            )
            ui_markers.append(CopilotUIMarker(kind=marker_kind, span=span))
            evidence_spans.append(span)

        if not matched_usage and usage_start_line is not None:
            flush_usage()

    flush_usage()

    activity_markers: list[CopilotActivityMarker] = []
    for pattern_str, template, category in _ACTIVITY_PATTERNS:
        for match in re.finditer(pattern_str, output, re.MULTILINE):
            group1 = match.group(1) if match.lastindex and match.lastindex >= 1 else ""
            activity = template.format(group1) if "{0}" in template else template
            line_no = output[: match.start()].count("\n") + 1
            span = CopilotEvidenceSpan(
                category=f"activity:{category}",
                start_line=line_no,
                end_line=line_no,
                text=match.group(0)[:120],
                confidence=Decimal("0.8500"),
            )
            activity_markers.append(
                CopilotActivityMarker(
                    activity=activity,
                    category=category,
                    span=span,
                )
            )
            evidence_spans.append(span)

    return CopilotOutputParseResult(
        session_ids=tuple(session_ids),
        boundaries=tuple(boundaries),
        usage_snapshots=tuple(usage_snapshots),
        blocking_issues=tuple(blocking_issues),
        errors=tuple(errors),
        ui_markers=tuple(ui_markers),
        activity_markers=tuple(activity_markers),
        evidence_spans=tuple(evidence_spans),
    )
