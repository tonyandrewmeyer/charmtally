# charmtally

*A feature-adoption survey of the Canonical charm fleet.* `charmtally` maps the
landscape of which `ops` / `pebble` / `jubilant` / `charmlibs` features each
charm uses — descriptive, not a leaderboard: not every feature applies to every
charm.

Browse the latest scan at
**[tonyandrewmeyer.github.io/charmtally/dashboard.html](https://tonyandrewmeyer.github.io/charmtally/dashboard.html)**.

## Install

```sh
uv tool install charmtally          # or: pipx install charmtally
```

Or run straight from a checkout with `uv run charmtally ...`.

## Usage

```sh
# Scan a single already-checked-out charm directory.
charmtally local path/to/my-operator

# Calibration: clone and scan a few charms from the corpus.
# The corpus CSV is fetched from canonical/hyrum on every run by default;
# pass `--corpus <local.csv>` to pin (offline / reproducible scans).
charmtally spike --workdir /tmp/charms --limit 5

# Full corpus scan -> results.json.
charmtally scan \
    --workdir /tmp/charms \
    --overrides corpus-overrides.yaml \
    --out results.json

# Re-score an existing results.json without re-cloning.
charmtally score results.json --overrides corpus-overrides.yaml --out scored.json

# Detect k8s/machine charm pairs (feeds the dashboard's Pairs view).
charmtally pairs scored.json --out pairs.json

# Render the dashboard.
charmtally dashboard scored.json --pairs pairs.json --out dashboard.html

# Render the History page from the dated snapshots under snapshots/.
charmtally trend --snapshots-dir snapshots --live scored.json --out trend.html

# Optional: re-score `worth-considering` records with an LLM.
# Needs OPENROUTER_API_KEY; --dry-run reports what would be sent.
charmtally llm-score scored.json --workdir /tmp/charms --dry-run

# Optional: check LLM verdicts against a human-labelled ground-truth file.
charmtally llm-calibrate scored.json ground-truth.json
```

The weekly [`scan` workflow](.github/workflows/scan.yaml) runs that pipeline
every Monday and commits the refreshed `results.json`, `dashboard.html` and
`trend.html` (plus a dated snapshot under `snapshots/`) back to `main`.
`scored.json`, `llm-scored.json` and `pairs.json` are intermediates and are
not committed.

## Building a rock corpus

There is no curated list of repos that build rocks the way canonical/hyrum
curates the charm list, so `rockfind` bootstraps one from GitHub code search:

```sh
export GITHUB_TOKEN=...          # any token; no scopes needed for public repos
uv run python -m charmtally.tools.rockfind search --out rocks.csv

# Refresh later without losing hand-curated Team / Notes columns.
uv run python -m charmtally.tools.rockfind search --out rocks.csv --merge rocks.csv
```

Code search returns whatever your token can see, **including private and
internal repos you have access to**, and the legacy query language this
endpoint speaks ignores `is:public`. Non-public repos are therefore filtered
out client-side, unconditionally, before anything is written; a token with no
`repo` scope keeps them out of the results to begin with.

Code search caps every query at 1000 results, so the search is partitioned by
file size and each over-cap bucket is split again. Forks and archived repos
are dropped unless `--include-forks` / `--include-archived` are passed, and
`--names` reads the declared `name:` out of each `rockcraft.yaml` (one extra
request per rock) instead of guessing it from the path. The output is a
starting point for curation, not a census: code search only indexes the
default branch of public repos.

`rocks.csv` is committed as source data and is meant to be curated by hand.
The weekly [`rocks` workflow](.github/workflows/rocks.yaml) re-runs the sweep
every Tuesday and opens a pull request with the diff rather than pushing to
`main` — new rows are unreviewed, and a code-search sweep turns up test
fixtures and abandoned experiments alongside real rocks. `--merge` keeps the
`Team` and `Notes` columns you edit, so curation survives the refresh. The
workflow needs a `ROCKS_TOKEN` secret; see the comment at the top of it.

## Development

```sh
uv sync
make unit          # pytest
make lint          # ruff + ty + codespell, exactly what CI runs
make format        # ruff format + ruff check --fix
make pre-commit    # every hook against every file
```
