# copilot-commander

A local Textual operator console for discovering and monitoring GitHub Copilot CLI panes in tmux.

## Environment

```bash
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e '.[dev]'
python -m pre_commit install --hook-type pre-commit --hook-type pre-push
```

Use Python 3.14 or newer, and run all project commands from the activated `.venv`. CI and Copilot automation both run on Python 3.14.

## Run the app

```bash
source .venv/bin/activate
python -m copilot_commander
```

Run the operator console from a tmux window when possible. If your panes live on another tmux socket, use the Setup screen to select that server.

## Dashboard usage

- `r` refreshes discovery immediately
- `j` / `k` move the dashboard selection
- `/` focuses the filter box
- `1` switches to the dashboard
- `2` switches to worktrees
- `3` switches to replay
- `4` switches to sessions
- `5` switches to setup
- `?` opens the in-app help screen

### Replay

- `m` focuses the marker list, `t` focuses the transcript
- `M` opens the multi-session picker — enter a comma-separated list of
  session ids to merge their events and log chunks into a single
  chronologically-ordered timeline with per-agent badges and
  `agent_switch` markers at every transition
- `space` / `p` toggles play/pause for time-driven playback (the virtual
  clock advances real-elapsed × speed and auto-pauses at the end)
- `,` / `.` step the clock to the previous / next entry timestamp
- `<` / `>` cycle the playback speed through `0.5×, 1×, 2×, 4×, MAX`
- `:` opens a small modal that accepts an absolute `HH:MM[:SS]` time or a
  signed delta like `+30s`, `-1m`, `+2h` and jumps the clock there

## Worktrees usage

- `j` / `k` move the worktree selection
- `s` / `enter` preview an agent start intent for the selected worktree
- `x` executes the current start intent
- `c` creates a new worktree for the selected repository
- `a` selects an existing worktree by path
- `d` deletes the selected worktree
- `P` prunes stale worktrees

## Replay usage

- `j` / `k` / `↑` / `↓` move the transcript or marker selection
- `/` focuses the transcript filter; `esc` returns focus to the list
- `m` focuses the marker list, `t` focuses the transcript
- `v` toggles parsed vs raw chunk view
- `f` toggles follow-latest mode
- `[` / `]` jump to the previous / next marker
- `a` jumps to the next activity marker, `x` to the next problem (error or blocking)
- `F` jumps to the next file edit; the diff panel renders the unified diff for the
  selected file mutation when the session is tied to a worktree
- `b` toggles a bookmark on the selected entry
- `n` opens a prompt to attach a note to the selected entry
- `N` cycles to the next bookmarked or noted entry
- `e` cycles the export format: text → JSON → Markdown (suitable for pasting into GitHub) → text
- `g` reloads the latest session

## Discovery model

- Discovery uses `tmux list-panes -a`, so it scans panes across **all windows** on the current tmux server, not just the current window.
- A pane is treated as a probable Copilot agent when its current command looks like Copilot or its recent pane output contains Copilot markers.
- For a tmux setup with **one session and five windows**, Copilot panes in any of those windows should appear on the dashboard after the next refresh.
- If the app is attached to a different tmux socket/server than the one holding your panes, open `5 · Setup` and switch to the matching socket.

## Quality gates

```bash
python -m pre_commit run --all-files
python -m ruff check .
python -m ruff format --check .
python -m mypy .
PYTHONPATH=src python -m pytest tests/ -q --tb=short
copilot-commander
```

Run `python -m pre_commit run --all-files` before opening a pull request to catch formatting and repository-hygiene issues locally. The pre-push hook runs `mypy` and `pytest` so new worktrees stay aligned with CI.

## Layout

- `src/copilot_commander/`: application package
- `tests/unit/`: fast unit coverage
- `tests/integration/`: integration-marked tests
