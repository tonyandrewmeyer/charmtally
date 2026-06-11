# Contributing to charmtally

Thanks for your interest in `charmtally`. It's a small personal project that
scans a corpus of public charms for feature adoption — issues, bug reports,
and pull requests are all welcome.

## Development setup

`charmtally` uses [`uv`](https://docs.astral.sh/uv/) for dependency management:

```console
uv sync
uv run pre-commit install
```

## Running checks

```console
make lint          # ruff + codespell
make format        # ruff format + ruff check --fix
make test          # the unit suite (runs in <1s)
make pre-commit    # all pre-commit hooks against every file
```

## Pull requests

PR titles must follow the
[Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/) format
with one of the following types and **no scope**:

```
<type>[!]: <description>

  type ∈ { chore, ci, docs, feat, fix, perf, refactor, revert, test }
```

This is enforced by the `validate-pr-title` workflow. Use `!` to mark a
breaking change. Examples:

- `feat: detect ops.unit.set_workload_version`
- `fix: handle empty corpus rows`
- `refactor!: rename score "n/a" to "not-applicable"`

The body of each commit should explain *why*, not what — the diff already
says what.

## Tests

Add tests for new detectors in `charmtally/tests/test_detectors.py`
(positive + negative). New scoring rules belong in `test_scoring.py`. The
unit suite must stay fast — if a new test takes more than a few hundred
milliseconds, it's probably doing too much.

## Generated artefacts

`results.json`, `scored.json`, `dashboard.html`, and `snapshots/scored-*.json`
are written by the weekly `scan` workflow and committed back to `main`.
Don't hand-edit them and don't include them in feature PRs — rebase onto
the latest scan commit if there's drift.

## Issues

Please open an issue *before* a substantial PR so we can talk about the
approach. For security issues, see [SECURITY.md](SECURITY.md) instead.
