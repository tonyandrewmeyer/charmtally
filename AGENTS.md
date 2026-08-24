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
            │                                     ▲           │
overrides ──┘                       (optional) llm-score ─────┤
                                               pairs ─► pairs.json
rocks.csv ─────────────────► scan-rocks ─────────┘            │
                             (__rocks__ block)                │
                    snapshots/scored-*.json ─┬─► trend ─► trend.html
                                             │                │
                                             └─► adoption ─► adoption.html
                                                              │
                                          dashboard ─► dashboard.html
```

- The corpus is **fetched at run time** from canonical/hyrum's
  `charm-list/charms.csv` (`corpus.HYRUM_CHARMS_CSV_URL`); there is no CSV
  checked into this repo. `--corpus <local.csv>` pins it for offline runs.
  A failed fetch retries, then falls back to the cached copy under the
  workdir rather than failing the run.
- `charmtally/features.yaml` — catalogue of features the detectors look for,
  plus the `architecture:` patterns. It lives **inside the package**, not at
  the repo root: it is package data, and resolving it relative to the repo
  root left every installed wheel with a CLI that died on a missing file.
  Reach for it via `catalogue.default_path()`, never `__file__` arithmetic.
- `corpus-overrides.yaml` — per-charm exclusions and feature-skip rules
  (silences shim-charm FPs, etc.). Loaded by `charmtally/corpus.py`.
- `charmtally/rocks.py` — the rocks half of the scan. Reads `rocks.csv` and
  fetches each row's `rockcraft.yaml` from `raw.githubusercontent.com`;
  rocks are **not cloned**, because every fact the scorecard wants of a rock
  is in that one file and the CSV already records its path. Archived repos
  and forks are dropped. `scan-rocks --into scored.json` parks the result in
  a `__rocks__` block, so the weekly dated snapshot carries it and a
  rocks-backed metric gets history without a second snapshot series. Every
  consumer of a scored file already skips `__`-prefixed keys, which is why
  the block can live there. Reach for the charm-side clone machinery only
  when a metric needs to see the rest of a rock's tree.
- `charmtally/adoption.py` — the charm-tech adoption scorecard: a
  deliberately tiny set (3–6) of headline metrics over the same dated
  snapshots `trend` reads. Distinct from `trend.py`, which covers the whole
  catalogue and answers "what changed". A metric returns `None` for a
  snapshot whose inputs weren't scanned yet, so a newly added metric shows a
  short series rather than a fake run of zeros. The default denominator is
  `eligible_charms` — the corpus minus reactive and legacy-classic charms —
  but it is a default, not a law: it excludes charms that could never adopt
  an *ops-era API*, so a metric about tooling that works whatever the charm
  is built on (jubilant) or about a population that isn't charms at all
  (rootless, which spans rocks + k8s charms) sets its own denominator and
  names it in `Metric.denominator_note`, which the card renders.

## Generated artefacts — do NOT hand-edit

`results.json`, `dashboard.html`, `trend.html`, `adoption.html` and
`snapshots/scored-*.json` are rewritten by the weekly `scan` workflow
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

## Tools (`charmtally/tools/`)

Not part of the pipeline; each is run with `uv run python -m charmtally.tools.X`.

- `audit.py` — calibration sampling over `scored.json`.
- `backfill.py` — recomputes `snapshots/scored-<date>.json` for weeks that
  were never scanned, by full-cloning each corpus repo and checking it out at
  its last commit before each date's 02:00 UTC cutoff. Separate workdir from
  the weekly scan's on purpose: that one is shallow, and a shallow clone
  cannot be checked out at a past commit. Snapshots it writes carry a
  `__backfill__` provenance block and no `__rocks__` block (rocks are fetched
  raw, not cloned, so they don't time-travel); the corpus list only goes back
  to hyrum's first `charms.csv` (2026-06-03), before which it falls back to
  that earliest copy and says so in the block. Existing snapshots are left
  alone unless `--force`.
- `rockfind.py` — builds a rocks corpus CSV from GitHub code search
  (`filename:rockcraft.yaml`). The REST search endpoint speaks the *legacy*
  query language: `path:` matches a directory there, so only `filename:`
  finds the file — `path:rockcraft.yaml` returns zero hits. Queries are capped
  at 1000 results, hence the `size:` partitioning; the ecosystem is already
  over that cap. Results are scoped to the caller's token and *do* include
  private/internal repos (~6% of hits on a Canonical token), so `public_only`
  drops every non-public repo before the CSV is written — that filter is not
  optional and must not become a flag.

`rocks.csv` is **source data, not build output**: unlike `results.json` and
friends it is meant to be hand-edited. The weekly `rocks` workflow refreshes it
via `--merge`, which preserves the `Team` and `Notes` columns a human curates
and never deletes a row that fell out of the search index. Edit those columns
freely; the sweep will not clobber them.

## Workflows

- `ci.yaml` — pytest matrix (3.10 / 3.12) + `make lint`.
- `zizmor.yaml` + `actionlint.yaml` — audit workflow files.
- `dependency-review.yaml` — PR-only gate on new dependency CVEs / licences.
- `rocks.yaml` — weekly cron + `workflow_dispatch`. Re-runs `rockfind` and
  opens/updates a PR against `rocks.csv` (a rolling `rocks-refresh` branch,
  force-pushed). Needs `ROCKS_TOKEN`: a fine-grained PAT scoped to this
  repo only, stored as an *environment* secret on the `rocks` environment
  (repo-level secrets are readable by every workflow; this token can write).
  The default `GITHUB_TOKEN` won't do — it can only code-search the repo it
  belongs to, and a PR opened with it would not trigger `ci.yaml`. Unlike
  `scan.yaml` this proposes rather than pushes, because the CSV is source data
  that wants review.
- `scan.yaml` — weekly cron + `workflow_dispatch`. Runs
  scan → score → (optional llm-score) → scan-rocks → pairs → dashboard →
  snapshot → trend, then pushes refreshed artefacts back to `main` via an explicit
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
- `adoption.METRICS` — the point of the scorecard is that it is short and
  stable. Adding a metric is a product decision, not a refactor; metric keys
  are also bookmark/JSON identifiers, so rename nothing.
- The LLM prompt or model in `charmtally/llm_score.py` — both feed
  `prompt_version()`, so editing either invalidates every cached verdict
  and the next run re-spends against the budget.
