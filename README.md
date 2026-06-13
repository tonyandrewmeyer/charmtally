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

# Render the dashboard.
charmtally dashboard scored.json --out dashboard.html
```

The weekly [`scan` workflow](.github/workflows/scan.yml) runs that pipeline
every Monday and commits the refreshed `dashboard.html` (plus a dated snapshot
under `snapshots/`) back to `main`.

## Development

```sh
uv sync
uv run pytest
uv run ruff check
uv run pre-commit run --all-files
```
