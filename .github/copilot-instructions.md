# Copilot repository instructions for `copilot-commander`

This repository is being developed toward **Python 3.14+** and a **Textual-based TUI**. Optimize for correctness, determinism, and maintainability over speed. If packaging metadata temporarily lags behind this direction, do not change unrelated ownership files just to reconcile it; instead, write clean forward-compatible code where feasible and note the mismatch.

## Core engineering rules

- Use **Python 3.14+** language features when they improve clarity, but prefer straightforward code over novelty.
- Write **fully typed** code. Treat missing or weak typing as a defect.
- Avoid `Any`, untyped dicts, stringly-typed state, broad `except`, and hidden side effects.
- Prefer small, composable functions; explicit data flow; and dependency injection over globals.
- Prefer **small, clearly scoped tasks and patches** over broad multi-concern changes.
- If a request spans multiple subsystems, decompose it into smaller reviewable steps before editing.
- Keep patches surgical. Do not refactor unrelated areas opportunistically.
- Do not invent APIs, CLI flags, config formats, or behavior. Verify first.

## Architecture boundaries

- Keep business logic out of Textual widgets.
- Separate concerns into distinct layers:
  - **UI layer**: Textual `App`, screens, widgets, bindings, presentation formatting.
  - **Application layer**: orchestration, command handling, workflows, task coordination.
  - **Domain layer**: pure models, rules, validation, parsing, state transitions.
  - **Infrastructure layer**: git, tmux, sqlite, filesystem, subprocess, network adapters.
- UI code may call application services; application services may call domain and infrastructure; domain should remain dependency-light and framework-agnostic.
- Prefer `Protocol`, `dataclass`, and small immutable value objects for boundaries.
- Put persistence and shell integration behind narrow interfaces so they are testable.

## Typing and Python standards

- All new functions, methods, class attributes, and module constants should be typed.
- Prefer `pathlib.Path`, `enum.StrEnum`, `dataclasses`, and `collections.abc` interfaces.
- Use `X | None` instead of `Optional[X]` unless needed for compatibility.
- Model structured data with typed classes instead of ad-hoc nested dictionaries.
- Raise precise exceptions with actionable messages; do not silently swallow failures.

## Textual patterns

- Keep widgets focused on rendering, input handling, and emitting messages/events.
- Prefer explicit messages/actions over reaching deep into sibling widgets.
- Use `reactive` state carefully; keep derived state centralized and predictable.
- For long-running or blocking work, use workers/background tasks instead of freezing the UI thread.
- Ensure bindings, commands, and focus behavior are discoverable and testable.
- Favor deterministic rendering logic so snapshot/testing strategies remain practical.

## Git, tmux, sqlite, and shell patterns

- Use `git` through small adapter functions or service objects, not scattered shell snippets.
- Use `git --no-pager` in commands and keep subprocess invocations explicit.
- Prefer `subprocess.run(..., check=False, text=True, capture_output=True)` wrapped by typed helpers.
- Treat tmux as infrastructure: isolate session/window/pane management behind adapters and parse outputs carefully.
- For sqlite, use parameterized queries only; manage transactions intentionally; keep schema and query logic out of UI code.
- Avoid hidden cwd assumptions. Pass paths explicitly and resolve from the repository root when needed.

## Testing expectations

- Add or update tests with every behavior change unless the change is documentation/config-only.
- Prefer fast, deterministic tests that do not require network access or real user interaction.
- Test domain and application behavior directly before relying on end-to-end UI coverage.
- Mock shell/tmux/git boundaries at adapter seams; do not over-mock pure logic.
- When introducing parsing or state transitions, include success and failure cases.

## Preferred commands

Run commands from the repository root and prefer the project virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
python -m pre_commit install --hook-type pre-commit --hook-type pre-push
python -m pytest
python -m pre_commit run --all-files
ruff check .
ruff format --check .
mypy .
```

If a tool is not configured yet, use the closest supported validation and state clearly what could and could not be run.

## Quality gates before finishing work

- Type expectations satisfied.
- New behavior covered by tests where applicable.
- Relevant checks run locally when available.
- No placeholder code, commented-out code, or dead branches left behind.
- No undocumented architectural shortcuts or boundary violations.
- No misleading claims about work completed, tests run, or behavior verified.

## Change hygiene

- Keep commits and diffs easy to review.
- If making commits, keep them single-purpose and use clear imperative messages that name the subsystem and intent.
- Update nearby docs/instructions when behavior or workflow changes.
- Preserve repository ownership boundaries; do not edit unrelated files.
- When blocked, say exactly what is blocked, why, and what evidence was gathered.
