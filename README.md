# copilot-commander

A local Textual operator console for discovering and monitoring GitHub Copilot CLI panes in tmux.

## Environment

```bash
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e '.[dev]'
```

Use Python 3.14 or newer, and run all project commands from the activated `.venv`.

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

## Discovery model

- Discovery uses `tmux list-panes -a`, so it scans panes across **all windows** on the current tmux server, not just the current window.
- A pane is treated as a probable Copilot agent when its current command looks like Copilot or its recent pane output contains Copilot markers.
- For a tmux setup with **one session and five windows**, Copilot panes in any of those windows should appear on the dashboard after the next refresh.
- If the app is attached to a different tmux socket/server than the one holding your panes, open `5 · Setup` and switch to the matching socket.

## Quality gates

```bash
python -m ruff check .
python -m ruff format --check .
python -m mypy .
python -m pytest
copilot-commander
```

## Layout

- `src/copilot_commander/`: application package
- `tests/unit/`: fast unit coverage
- `tests/integration/`: integration-marked tests
