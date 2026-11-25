# Job: <short title>

## Goal
<One paragraph: what must exist or work when this job is done, and why.>

## Context
- Language/stack: <e.g. Python 3.11, stdlib only>
- Key files to read first: <paths>
- Conventions: <style, error handling, naming>

## Scope (may change)
- <e.g. src/roman.py (new)>
- <e.g. README.md (usage section only)>

## Out of scope (must NOT change)
- <e.g. tests/ (acceptance tests are fixed by the reviewer)>
- <e.g. public API of src/core.py>
- No new third-party dependencies, no network access, no git commits/pushes.

## Acceptance (must pass, the reviewer re-runs these independently)
```sh
<e.g. python3 -m unittest discover -s tests -v>
```

## Done criteria
- All acceptance commands exit 0.
- Only files in "Scope" changed.
- <extra checks: edge cases handled, docstrings, CLI --help works, ...>

## Notes for the agent
- Keep PROGRESS.md at the repo root updated as a short checklist (it is not part of the deliverable).
- If something is ambiguous, pick the simplest reasonable option and note it in PROGRESS.md.
