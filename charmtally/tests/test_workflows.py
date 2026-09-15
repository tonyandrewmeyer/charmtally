"""Reusable-workflow resolution: the `uses:` edges, the cache, the hops."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from charmtally import workflows

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _no_network() -> None:
    """Leave the module disabled after every test.

    `configure` is process state, so a test that enables resolution and does
    not put it back would hand the next test a live fetcher.
    """
    workflows.configure(None)
    yield
    workflows.configure(None)


# ── iter_uses ───────────────────────────────────────────────────────────────


def test_iter_uses_reads_a_cross_repo_call() -> None:
    text = """\
jobs:
  ci:
    uses: canonical/observability/.github/workflows/charm-pull-request.yaml@main
"""
    (ref,) = workflows.iter_uses(text)
    assert ref.owner == "canonical"
    assert ref.repo == "observability"
    assert ref.path == ".github/workflows/charm-pull-request.yaml"
    assert ref.ref == "main"
    assert ref.line == 3
    assert ref.url == (
        "https://raw.githubusercontent.com/canonical/observability/main"
        "/.github/workflows/charm-pull-request.yaml"
    )


def test_iter_uses_defaults_an_unpinned_call_to_head() -> None:
    (ref,) = workflows.iter_uses("    uses: canonical/foo/.github/workflows/x.yml\n")
    assert ref.ref == "HEAD"


def test_iter_uses_ignores_an_action_and_a_same_repo_call() -> None:
    """Neither is a cross-repo workflow the scan has to go and fetch.

    An action (`actions/checkout@v4`) is not a workflow at all, and a `./`
    call is already in the sweep that produced the calling file.
    """
    text = """\
jobs:
  ci:
    steps:
      - uses: actions/checkout@v4
      - uses: canonical/setup-lxd@main
    uses: ./.github/workflows/_build.yaml
"""
    assert list(workflows.iter_uses(text)) == []


# ── the cache ───────────────────────────────────────────────────────────────


def test_cache_fetches_once_and_reuses_the_file(tmp_path: Path) -> None:
    calls: list[str] = []

    def fetch(url: str) -> str | None:
        calls.append(url)
        return "body\n"

    cache = workflows.WorkflowCache(tmp_path, fetch)
    assert cache.get("https://example/x") == "body\n"
    # A fresh cache over the same directory: the in-memory memo cannot be what
    # answers, which is the property the process pool depends on.
    assert workflows.WorkflowCache(tmp_path, fetch).get("https://example/x") == "body\n"
    assert calls == ["https://example/x"]


def test_cache_holds_a_miss_in_memory_only(tmp_path: Path) -> None:
    """A miss must not outlive the process that saw it.

    On disk it would carry one run's network weather into the next, and a
    workflow that could not be read reads as a charm not doing the thing.
    """
    calls: list[str] = []

    def fetch(url: str) -> str | None:
        calls.append(url)
        return None

    cache = workflows.WorkflowCache(tmp_path, fetch)
    assert cache.get("https://example/x") is None
    assert cache.get("https://example/x") is None
    assert calls == ["https://example/x"]  # memoised within the process
    assert workflows.WorkflowCache(tmp_path, fetch).get("https://example/x") is None
    assert len(calls) == 2  # but a fresh process asks again
    assert list(tmp_path.iterdir()) == []


def test_cache_refetches_a_stale_entry(tmp_path: Path) -> None:
    """An entry older than the TTL is re-read, so a weekly run starts cold.

    A workflow that has stopped provisioning with Concierge has to be able to
    say so; a cache with no expiry would freeze the reading indefinitely.
    """
    bodies = iter(["old\n", "new\n"])

    def fetch(_url: str) -> str:
        return next(bodies)

    assert workflows.WorkflowCache(tmp_path, fetch).get("https://example/x") == "old\n"
    assert workflows.WorkflowCache(tmp_path, fetch).get("https://example/x") == "old\n"
    expired = workflows.WorkflowCache(tmp_path, fetch, max_age=-1)
    assert expired.get("https://example/x") == "new\n"


def test_fetch_retries_a_dropped_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """A blip is worth a second ask.

    One during testing dropped half a real charm's evidence, and a workflow
    that fails to read is reported as a charm not doing the thing.
    """
    monkeypatch.setattr(workflows, "_RETRY_DELAY", 0)
    calls = []

    def urlopen(url: str, timeout: float = 30.0) -> None:
        calls.append(url)
        raise OSError("connection reset")

    monkeypatch.setattr(workflows.urllib.request, "urlopen", urlopen)
    assert workflows.fetch_text("https://example/x") is None
    assert len(calls) == 2


def test_fetch_does_not_retry_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 404 is the server's real answer; asking twice gets it twice."""
    monkeypatch.setattr(workflows, "_RETRY_DELAY", 0)
    calls = []

    def urlopen(url: str, timeout: float = 30.0) -> None:
        calls.append(url)
        raise workflows.urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    monkeypatch.setattr(workflows.urllib.request, "urlopen", urlopen)
    assert workflows.fetch_text("https://example/x") is None
    assert len(calls) == 1


# ── resolve ─────────────────────────────────────────────────────────────────


_CALLER = """\
jobs:
  ci:
    uses: canonical/observability/.github/workflows/charm-pull-request.yaml@main
"""

_HOP1 = """\
jobs:
  quality:
    uses: canonical/observability/.github/workflows/_charm-quality-checks.yaml@main
"""

_HOP2 = "    - run: sudo concierge prepare --trace\n"

_REMOTE = {
    "https://raw.githubusercontent.com/canonical/observability/main"
    "/.github/workflows/charm-pull-request.yaml": _HOP1,
    "https://raw.githubusercontent.com/canonical/observability/main"
    "/.github/workflows/_charm-quality-checks.yaml": _HOP2,
}


def _configure(tmp_path: Path, remote: dict[str, str] | None = None) -> None:
    body = _REMOTE if remote is None else remote
    workflows.configure(tmp_path, lambda url: body.get(url))


def test_resolve_returns_nothing_when_the_run_has_no_cache() -> None:
    assert workflows.resolve(_CALLER) == []


def test_resolve_follows_a_second_hop(tmp_path: Path) -> None:
    _configure(tmp_path)
    hits = workflows.resolve(_CALLER)
    assert [h.ref.path for h in hits] == [
        ".github/workflows/charm-pull-request.yaml",
        ".github/workflows/_charm-quality-checks.yaml",
    ]
    deep = hits[1]
    assert deep.text == _HOP2
    # The edge a reader can click is the one written in the caller's own repo,
    # even though the answer is two files away.
    assert deep.origin.line == 3
    assert deep.origin.path == ".github/workflows/charm-pull-request.yaml"
    # The first hop found itself, so origin and ref are the same edge there.
    assert hits[0].origin is hits[0].ref


def test_resolve_stops_at_a_cycle(tmp_path: Path) -> None:
    a_url = "https://raw.githubusercontent.com/o/r/HEAD/.github/workflows/a.yaml"
    b_url = "https://raw.githubusercontent.com/o/r/HEAD/.github/workflows/b.yaml"
    _configure(
        tmp_path,
        {
            a_url: "    uses: o/r/.github/workflows/b.yaml\n",
            b_url: "    uses: o/r/.github/workflows/a.yaml\n",
        },
    )
    hits = workflows.resolve("    uses: o/r/.github/workflows/a.yaml\n")
    assert [h.ref.path for h in hits] == [
        ".github/workflows/a.yaml",
        ".github/workflows/b.yaml",
    ]


def test_resolve_skips_a_workflow_it_cannot_read(tmp_path: Path) -> None:
    """Private or renamed is the sized residual, and must not fail the scan."""
    _configure(tmp_path, {})
    assert workflows.resolve(_CALLER) == []
