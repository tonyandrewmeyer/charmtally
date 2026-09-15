"""Resolve the reusable workflows a repo's CI delegates to.

A charm whose `.github/` only calls `uses: canonical/observability/.github/
workflows/charm-pull-request.yaml@main` tells the scan nothing about how it is
tested: the answer is in a repo the corpus has never heard of, let alone
cloned. `repo-file` detectors that opt in with `follow_uses: true` get the
called workflows' text as well as their own repo's, which is what closes the
`testing.concierge` false negative sized in charmtally#121 — 46 repos / 53
charms, about a third of the feature's true population.

Fetched rather than cloned, for the same reason `rocks.py` fetches
`rockcraft.yaml`: the question is answered by one file, and its path is
already spelled out in the `uses:` value. Resolution is transitive because
two of the seven workflow files that account for that gap are only reached at
a second hop — `charm-pull-request.yaml` calls `_charm-quality-checks.yaml`,
and only the latter runs Concierge.

Two switches, on purpose. The catalogue says which detectors *want* the
resolution; `configure()` says whether this run can do it. Nothing here
touches the network until a caller installs a cache, so `charmtally local`,
the test suite and an offline `--corpus` run behave exactly as they did
before — a detector that asked to follow `uses:` simply reads what is on disk.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

RAW_BASE = "https://raw.githubusercontent.com"

#: How many `uses:` hops to follow. Two is what the sized corpus needs; the
#: third is slack for a library that grows a layer, and the cap is what stops
#: a cycle (`a` calls `b` calls `a`) from running forever. Depth is counted
#: per resolution, and the seen-set makes a repeat visit free anyway.
MAX_DEPTH = 3

#: How long a cached workflow stays usable. Long enough to cover a run — the
#: only thing the cache is really for, since the scan's worker processes have
#: no other way to share a fetch — and far short of the weekly cadence, so no
#: two runs read the same cached bytes.
MAX_AGE_SECONDS = 6 * 3600

#: Seconds between the two attempts `fetch_text` makes. Short: the failure it
#: is covering is a dropped connection, not a rate limit, and a scan waiting on
#: it is a scan not scanning.
_RETRY_DELAY = 1.0

#: A cross-repo reusable-workflow call, as distinct from a step's `uses:` of
#: an action. Only the former names a path under `.github/workflows/`, which
#: is what separates the two without parsing the YAML — and parsing is not
#: worth it here: a workflow that fails to parse (templated, or a syntax the
#: loader dislikes) would take its `uses:` edges down with it, and the edge is
#: a flat string on a line of its own either way.
_USES_RE = re.compile(
    r"^\s*(?:-\s*)?uses:\s*['\"]?"
    r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)"
    r"/(?P<path>\.github/workflows/[A-Za-z0-9_./-]+\.ya?ml)"
    r"(?:@(?P<ref>[^\s'\"#]+))?",
    re.MULTILINE,
)


@dataclass(frozen=True)
class UsesRef:
    """One cross-repo `uses:` edge, and where it was written."""

    owner: str
    repo: str
    path: str  # always under .github/workflows/
    ref: str  # git ref; "HEAD" when the call pinned none
    line: int  # 1-indexed line in the file that made the call

    @property
    def slug(self) -> str:
        """`canonical/observability/.github/workflows/x.yaml@main`."""
        return f"{self.owner}/{self.repo}/{self.path}@{self.ref}"

    @property
    def url(self) -> str:
        return f"{RAW_BASE}/{self.owner}/{self.repo}/{self.ref}/{self.path}"


def iter_uses(text: str) -> Iterator[UsesRef]:
    """Yield the cross-repo reusable workflows `text` calls, in source order.

    A same-repo call (`uses: ./.github/workflows/_build.yaml`) yields nothing:
    it is already in the sweep that produced this file, so fetching it would
    re-read what the detector has in hand.
    """
    for match in _USES_RE.finditer(text):
        yield UsesRef(
            owner=match["owner"],
            repo=match["repo"],
            path=match["path"],
            ref=match["ref"] or "HEAD",
            line=text.count("\n", 0, match.start()) + 1,
        )


def fetch_text(url: str, *, timeout: float = 30.0, attempts: int = 2) -> str | None:
    """GET `url`, or None if it can't be read.

    Same contract as `rocks.fetch_text`: every failure means the same thing to
    a caller — this workflow is not readable, so it contributes no evidence —
    and none of them may end a sweep over hundreds of repos. The residual that
    lands here is real and was sized: 24 of the 460 GitHub-hosted corpus repos
    call a workflow that could not be read, private or since renamed.

    Retried once, unlike the rocks fetch, because the two failures do not cost
    the same. A rock that fails to read is one missing row; a workflow that
    fails to read makes every charm delegating to it read as *not doing* the
    thing, which is indistinguishable in the output from having been looked at
    and found wanting. A blip during one run was enough to drop half of one
    charm's evidence in testing.
    """
    for attempt in range(attempts):
        try:
            # ruff: ignore[suspicious-url-open-usage] — URL is built from RAW_BASE, always https.
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError:  # ruff: ignore[try-except-in-loop] — the retry is the point
            # The server answered, and the answer was no. Private, renamed or
            # never there — a retry asks the same question and gets the same
            # reply, so it only costs the sweep time.
            return None
        except (urllib.error.URLError, OSError, ValueError):
            if attempt == attempts - 1:
                return None
            time.sleep(_RETRY_DELAY)
    return None


class WorkflowCache:
    """Fetched workflow text, memoised in memory and on disk.

    On disk because the scan fans out over a *process* pool: the seven files
    that account for the whole gap would otherwise be fetched once per worker
    rather than once per run.

    Within a run, not between them. An entry older than `max_age` is refetched,
    which is what stops a workflow that has since stopped provisioning with
    Concierge from reading as though it still did — the scan re-reads every
    charm at its current commit each week, and a reading frozen at a cached
    file would be exactly the silent staleness `__meta__.stale` exists to make
    visible. The directory still wants to be under the workdir; being caught by
    CI's cache costs nothing, since a week-old entry is refetched anyway.

    Misses are held in memory only, never written. A workflow that could not be
    read makes every charm delegating to it read as not doing the thing, and a
    miss on disk would carry one run's network weather into the next.
    """

    def __init__(
        self,
        cache_dir: Path,
        fetch: Callable[[str], str | None] = fetch_text,
        max_age: float = MAX_AGE_SECONDS,
    ) -> None:
        self.cache_dir = cache_dir
        self.max_age = max_age
        self._fetch = fetch
        self._mem: dict[str, str | None] = {}

    def _path(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
        return self.cache_dir / f"{digest}.yaml"

    def get(self, url: str) -> str | None:
        if url in self._mem:
            return self._mem[url]
        path = self._path(url)
        text: str | None = None
        if self._fresh(path):
            text = path.read_text(encoding="utf-8", errors="replace")
        else:
            text = self._fetch(url)
            if text is not None:
                self._store(path, text)
        self._mem[url] = text
        return text

    def _fresh(self, path: Path) -> bool:
        try:
            return time.time() - path.stat().st_mtime < self.max_age
        except OSError:
            return False

    def _store(self, dest: Path, text: str) -> None:
        """Write `dest` atomically, or not at all.

        Workers race on the same entry — several charms in one repo resolve
        the same call at once — so the write goes to a per-process temporary
        and is renamed into place, which is atomic on POSIX. A cache that
        cannot be written (read-only dir, full disk) costs speed and nothing
        else, so the failure is swallowed rather than raised into a scan.
        """
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(f"{dest.suffix}.{os.getpid()}.tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(dest)
        except OSError:
            pass


# Installed once per process by whoever knows where the workdir is (the `scan`
# command, and its pool initialiser). `None` — the default, and what a test or
# a `charmtally local` run sees — means this run does not follow `uses:` at
# all, so no detector reaches the network by accident.
_CACHE: WorkflowCache | None = None


def configure(cache_dir: Path | None, fetch: Callable[[str], str | None] = fetch_text) -> None:
    """Enable `uses:` resolution for this process, caching under `cache_dir`.

    Passing None disables it again, which is what `--no-follow-uses` does and
    what every test that has not asked for the network gets.
    """
    global _CACHE
    _CACHE = None if cache_dir is None else WorkflowCache(cache_dir, fetch)


def enabled() -> bool:
    """Whether this process will follow `uses:` at all."""
    return _CACHE is not None


@contextlib.contextmanager
def session(cache_dir: Path | None, fetch: Callable[[str], str | None] = fetch_text):
    """Enable resolution for the duration of the block, then put it back.

    `configure` on its own is right for a worker process, which exists only to
    run the scan it was initialised for. A command is not: leaving the global
    set on the way out means whatever runs next in the same process — another
    subcommand, the next test — inherits a fetcher it never asked for.
    """
    global _CACHE
    previous = _CACHE
    configure(cache_dir, fetch)
    try:
        yield
    finally:
        _CACHE = previous


@dataclass(frozen=True)
class Resolved:
    """One reachable workflow: what it says, and the local line that led there.

    `origin` is the edge written in the caller's own repo, even when `ref` was
    reached at the second hop. Evidence has to cite a line a reader can open,
    and at depth 2 the only such line is the first `uses:` — the intermediate
    file belongs to a repo the dashboard has no link for. `ref` names the
    workflow the text actually came from, so a snippet can still say where a
    match was made; `ref is origin` is what "found at the first hop" looks
    like, and the intermediate hops are not worth carrying, since a snippet
    long enough to print them is longer than the dashboard shows.
    """

    ref: UsesRef
    text: str
    origin: UsesRef


def resolve(text: str, *, depth: int = MAX_DEPTH) -> list[Resolved]:
    """Return the workflows `text` reaches through `uses:`, transitively.

    Breadth-first, so a direct call is reported before anything it in turn
    calls, and each workflow appears once however many callers reach it.

    Returns nothing at all when the run has no cache installed.
    """
    if _CACHE is None:
        return []
    out: list[Resolved] = []
    seen: set[str] = set()
    frontier = [(ref, ref) for ref in iter_uses(text)]
    for _ in range(depth):
        if not frontier:
            break
        nxt: list[tuple[UsesRef, UsesRef]] = []
        for ref, origin in frontier:
            if ref.slug in seen:
                continue
            seen.add(ref.slug)
            body = _CACHE.get(ref.url)
            if body is None:
                continue
            out.append(Resolved(ref=ref, text=body, origin=origin))
            nxt.extend((child, origin) for child in iter_uses(body) if child.slug not in seen)
        frontier = nxt
    return out
