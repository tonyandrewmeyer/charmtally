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
| Type-check | `uv run ty check` (whole package; not file-scoped) |

Full suite: `make lint`, `make format`, `make unit`. `make pre-commit` runs
every hook against every file. `make lint` is what CI runs — ruff, ty and
codespell, repo-wide.

## Commit attribution

AI commits MUST include a `Co-Authored-By` trailer with the model name —
e.g. `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

## Project shape

`charmtally` scans a corpus of public charms for which `ops` / `pebble` /
`jubilant` / `charmlibs` features each charm uses, and renders a static
HTML dashboard. There is no service, no DB, no auth.

Pipeline (also the order of the CLI subcommands in `charmtally/cli.py`):

```
corpus CSV ─┬─► scan ─► results.json ─► score ─► scored.json ─┐
            │                                                 │
overrides ──┘                       (optional) llm-score ─────┤
                                               pairs ─► pairs.json
                                                              │
                    snapshots/scored-*.json ─► trend ─► trend.html
                                                              │
                                          dashboard ─► dashboard.html
```

- The corpus is **fetched at run time** from canonical/hyrum's
  `charm-list/charms.csv` (`corpus.HYRUM_CHARMS_CSV_URL`); there is no CSV
  checked into this repo. `--corpus <local.csv>` pins it for offline runs.
- `charmtally/features.yaml` — catalogue of features the detectors look for,
  plus the `architecture:` patterns. It lives **inside the package**, not at
  the repo root: it is package data, and resolving it relative to the repo
  root left every installed wheel with a CLI that died on a missing file.
  Reach for it via `catalogue.default_path()`, never `__file__` arithmetic.
- `corpus-overrides.yaml` — per-charm exclusions and feature-skip rules
  (silences shim-charm FPs, etc.). Loaded by `charmtally/corpus.py`.

## Generated artefacts — do NOT hand-edit

`results.json`, `dashboard.html`, `trend.html` and `snapshots/scored-*.json`
are rewritten by the weekly `scan` workflow
(`.github/workflows/scan.yaml`). Treat them as build output that happens to
live in git (the dashboard is served via GitHub Pages from `main`). Don't
revert their contents when rebasing; rebase your work *onto* the latest
scan commit instead.

`scored.json`, `llm-scored.json` and `pairs.json` are intermediates: the
workflow produces them but does not commit them, and they're gitignored.
Snapshots stay uncompressed — near-identical text deltas to tens of KiB in
git, whereas gzipped snapshots would each be an undeltifiable blob.

## Test conventions

- Tests live in `charmtally/tests/`, mirroring the module under test.
- `pytest` only — no async, no fixtures-as-modules. The full suite runs in
  about a second; keep it that way. `test_scan_clone.py` shells out to real
  git because the behaviour under test *is* the git interaction — keep its
  repo setup to as few `git` invocations as possible.
- Tests are excluded from `ty` (see `[tool.ty.src]`); the package is not.

## Detector kinds (`charmtally/detectors.py`)

All triggered from `features.yaml`, in three groups.

Per Python file in scope: `import` · `call` · `observe-event` · `regex`.

Per Python file, backing the architecture axis: `ast-init-call` ·
`ast-observe-shared-handler` · `ast-shared-method`.

File-independent, reading the charm root directly (these are the only ones
that can see non-Python files — `_select_files` returns `*.py` only):
`yaml-key` · `pytest-config-key` · `requires-interface` · `relation-count`.

A charm is matched against every feature and pattern, so `scan_charm`
builds one `CharmSource` and passes it to every `detect_feature` call;
each file is read and parsed once per charm, not once per feature. New
per-file detector kinds must read from the `SourceFile` they're handed
rather than re-reading the path.

When adding a new detector kind, also add at least one positive and one
negative test in `tests/test_detectors.py`.

## Workflows

- `ci.yaml` — pytest matrix (3.10 / 3.12) + `make lint`.
- `zizmor.yaml` + `actionlint.yaml` — audit workflow files.
- `dependency-review.yaml` — PR-only gate on new dependency CVEs / licences.
- `scan.yaml` — weekly cron + `workflow_dispatch`. Runs
  scan → score → (optional llm-score) → pairs → dashboard → snapshot →
  trend, then pushes refreshed artefacts back to `main` via an explicit
  token-in-URL remote. The charm workdir is cached between runs and
  `ensure_clone` refreshes each clone, so a cached charm is re-scanned at
  the current commit rather than the one it was first cloned at.

Pin third-party actions to a commit SHA with the version in a trailing
comment. `actions/*` and `pypa/*` may ride a tag (matches the wider
ecosystem convention).

## What's worth asking before changing

- Scoring rules in `charmtally/scoring.py` — the rationale strings are
  user-facing; if you change a rule, update the rationale too.
- `charmtally/features.yaml` — adding a feature is fine; renaming or removing
  one breaks every downstream snapshot. Prefer additive changes.
- The LLM prompt or model in `charmtally/llm_score.py` — both feed
  `prompt_version()`, so editing either invalidates every cached verdict
  and the next run re-spends against the budget.
