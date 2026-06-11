# Agent Instructions

## Package manager

Use **uv**: `uv sync`, `uv run <cmd>`. Dev tooling lives in
`[dependency-groups] dev` in `pyproject.toml` — do not move it to
`[project.optional-dependencies]`. CI runs `uv sync` (no extras / groups
needed; uv installs the default dev group).

## File-scoped commands

| Task | Command |
|------|---------|
| Lint | `uv run ruff check path/to/file.py` |
| Format | `uv run ruff format path/to/file.py` |
| Test | `uv run pytest charmtally/tests/test_X.py` |

Full suite: `make lint`, `make format`, `make test`. `make pre-commit` runs
every hook against every file.

## Commit attribution

AI commits MUST include a `Co-Authored-By` trailer with the model name —
e.g. `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

## Project shape

`charmtally` scans a corpus of public charms for which `ops` / `pebble` /
`jubilant` / `charmlibs` features each charm uses, and renders a static
HTML dashboard. There is no service, no DB, no auth.

Pipeline (also the order of the CLI subcommands in `charmtally/cli.py`):

```
corpus CSV ─┬─► scan   ─► results.json
            │
overrides ──┴─► score  ─► scored.json  ─► dashboard ─► dashboard.html
```

- `may-2026.csv` — the corpus (source: canonical/hyrum's charm list).
- `features.yaml` — catalogue of features the detectors look for.
- `corpus-overrides.yaml` — per-charm exclusions and feature-skip rules
  (silences shim-charm FPs, etc.). Loaded by `charmtally/corpus.py`.

## Generated artefacts — do NOT hand-edit

`results.json`, `scored.json`, `dashboard.html`, and `snapshots/scored-*.json`
are rewritten by the weekly `scan` workflow
(`.github/workflows/scan.yml`). Treat them as build output that happens to
live in git (the dashboard is served via GitHub Pages from `main`). Don't
revert their contents when rebasing; rebase your work *onto* the latest
scan commit instead.

## Test conventions

- Tests live in `charmtally/tests/`, mirroring the module under test.
- `pytest` only — no async, no fixtures-as-modules. The full suite runs in
  under a second; keep it that way.

## Detector kinds (`charmtally/detectors.py`)

Four kinds, all triggered from `features.yaml`:
`import` · `call` · `observe-event` · `regex`. `yaml-key` is deferred. When
adding a new detector kind, also add at least one positive and one
negative test in `tests/test_detectors.py`.

## Workflows

- `ci.yml` — pytest matrix (3.10 / 3.12) + ruff lint + ruff format check.
- `zizmor.yml` + `actionlint.yml` — audit workflow files.
- `dependency-review.yml` — PR-only gate on new dependency CVEs / licences.
- `scan.yml` — weekly cron + `workflow_dispatch`. Pushes refreshed
  artefacts back to `main` via an explicit token-in-URL remote.

Pin third-party actions to a commit SHA with the version in a trailing
comment. `actions/*` and `pypa/*` may ride a tag (matches the wider
ecosystem convention).

## What's worth asking before changing

- Scoring rules in `charmtally/scoring.py` — the rationale strings are
  user-facing; if you change a rule, update the rationale too.
- `features.yaml` — adding a feature is fine; renaming or removing one
  breaks every downstream snapshot. Prefer additive changes.
