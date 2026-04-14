# copilot-commander

Contributor bootstrap for the `copilot-commander` Python project.

## Environment

```bash
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e '.[dev]'
```

Use Python 3.14 or newer, and run all project commands from the activated `.venv`.

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
