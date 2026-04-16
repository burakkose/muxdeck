## Summary

-

## Validation

- [ ] `python -m pre_commit run --all-files`
- [ ] `python -m mypy .`
- [ ] `PYTHONPATH=src python -m pytest tests/ -q --tb=short`

## Checklist

- [ ] The diff is scoped to a single task/worktree.
- [ ] No unrelated files, generated artifacts, or local scratch changes are included.
- [ ] Tests were added or updated for behavior changes, or this change is config/docs-only.
- [ ] Any skipped validation or known failures are explained below.
