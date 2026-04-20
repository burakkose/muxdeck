# AGENTS.md

Guidance for human and AI contributors working in this repository.

## Mission

Build `copilot-commander` as a high-quality Python 3.14+ Textual TUI with strong architecture, strict typing, and reliable local/CLI/cloud-agent workflows.

## Non-negotiable standards

- No sloppy work.
- No guessing about APIs, behavior, or requirements when evidence is available.
- No claiming tests passed unless you actually ran them.
- No broad, silent exception handling.
- No dead code, placeholder branches, or speculative abstractions without immediate value.
- No unrelated edits “while you are here.”

## File and ownership boundaries

- Respect task ownership. Only edit files required for the active task.
- Keep cross-cutting repo guidance in `.github/**` and `AGENTS.md`.
- Do not change product code, packaging, or docs owned by another task unless explicitly asked.
- Prefer the **smallest reviewable task scope** that still produces a coherent result.
- If a request spans multiple layers or concerns, split it into smaller todos before editing.

## Branch, worktree, and task conventions

- Prefer **one task per branch**.
- Every active agent must work from its **own dedicated git worktree**. Do not let multiple agents share the same checkout or branch workspace at the same time.
- Create each agent worktree from the intended integration branch, which should be either `main` or the relevant long-lived feature branch for that effort.
- Branch names should be descriptive, for example:
  - `copilot/agentic-copilot-setup`
  - `feat/session-browser`
  - `fix/tmux-pane-parsing`
- Prefer **one worktree per active task** when parallelizing work.
- Keep worktrees clean: no mixed-purpose changes, no lingering generated files, no untracked scratch artifacts.
- Before starting, confirm the task scope and avoid bleeding into adjacent todos.
- After creating or switching to a worktree, install the repository hooks with `python -m pre_commit install --hook-type pre-commit --hook-type pre-push`.
- After an agent finishes and validates its scoped change, merge that worktree branch back into the originating `main` or feature branch through the normal review/integration flow.

## Implementation expectations

- Favor clear architecture boundaries:
  - UI/Textual
  - application services
  - domain logic
  - infrastructure adapters
- Keep business logic out of widgets and shell commands out of presentation code.
- Introduce abstractions only when they simplify testing or isolate an external dependency.
- Prefer explicit state models and typed result objects over unstructured dictionaries.

## Testing and validation expectations

- Run the relevant checks before finishing any code task.
- Preferred validation stack, when configured:
  - `python -m pre_commit run --all-files`
  - `python -m pytest`
  - `ruff check .`
  - `ruff format --check .`
  - `mypy .`
- If the repo does not yet provide a tool, use the strongest available smoke validation and report the gap.
- New behavior should come with tests unless the task is documentation/config-only.
- Validate edge cases and failure modes, not only the happy path.

## Command execution rules

- Run commands from the repository root unless there is a documented reason not to.
- Prefer the local virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

- Use explicit commands and deterministic flags.
- Use `git --no-pager` for scripted reads.
- Avoid interactive commands unless the task specifically requires them.

## Git hygiene

- Keep diffs reviewable and scoped.
- Re-read your diff before finishing.
- Do not commit unrelated formatting churn.
- Do not overwrite user work or discard changes you did not make.
- Do not develop directly on shared integration branches from an agent session; isolate the work in the agent worktree first, then merge back after review.
- If you create a commit, keep it **single-purpose** and tied to one scoped task.
- Use a clear imperative commit message that names the subsystem and change, for example:
  - `Add tmux pane parser`
  - `Implement SQLite migration runner`
  - `Fix worktree assignment validation`

## Done means done

A task is not complete until:

- the requested files are updated,
- the change is internally consistent,
- relevant validation has been run,
- the final report states what changed and what was verified.

## When blocked

- Stop making speculative edits.
- Record the exact blocker, its impact, and the evidence.
- Leave the repository in a clean, understandable state.
