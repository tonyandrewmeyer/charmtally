"""Build a corpus CSV of public repos that contain a ``rockcraft.yaml``.

The charm corpus is curated by hand (canonical/hyrum's ``charms.csv``); there
is no equivalent list for rocks, so this tool bootstraps one from GitHub's
code search API::

    export GITHUB_TOKEN=ghp_...
    uv run python -m charmtally.tools.rockfind search --out rocks.csv

Code search is the only index that can answer "which repos hold a file called
``rockcraft.yaml``", but it comes with three sharp edges, all handled here:

* **Authentication is mandatory** — the endpoint 404s/403s without a token.
* **1000 results per query, hard cap.** A single ``filename:rockcraft.yaml``
  query would silently truncate once the ecosystem passes that many files (as
  of August 2026 there are ~1180, so it already does). The
  search is therefore partitioned by file ``size:`` and each bucket that is
  still over the cap is split again, so the union stays complete. Buckets that
  cannot be split any further (all files the same byte size) are reported on
  stderr rather than dropped quietly.
* **30 requests/minute**, separate from the core limit. Requests are paced by
  ``--sleep`` and both rate-limit responses (403 with a reset header, 429 with
  ``Retry-After``) are waited out and retried.

**Code search is scoped to what your token can see**, which includes private
and internal repos you have access to — a Canonical-employee token surfaces
plenty of them. Those must never reach a shared corpus, so every non-public
result is dropped before the CSV is written. The filter is client-side and
unconditional: the legacy query language this endpoint speaks does not honour
``is:public`` (it returns zero hits for both ``is:public`` and ``is:private``),
so there is no server-side way to ask for public-only. Running with a token
that has no ``repo`` scope keeps private code out of the results in the first
place; the filter is the backstop, not the only line of defence.

Code search otherwise indexes the default branch of public, non-empty repos, so the
result is a floor on the real population, not a census. Treat the CSV as a
starting point for curation — ``--merge`` re-runs against an existing file and
preserves the hand-edited ``Team`` and ``Notes`` columns.

Columns mirror the charm corpus (``Team``, ``<X> Name``, ``Repository``,
``Branch (if not the default)``, ``Source``) so the two CSVs read the same way,
plus rock-specific and triage columns (``Rockcraft Path``, ``Stars``,
``Last Push``, ``Archived``, ``Fork``, ``Notes``).
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import yaml

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

GITHUB_API = "https://api.github.com"
#: `filename:` matching is fuzzy — it also returns `rockcraft.yaml.j2` and
#: friends. Only an exact basename is a rock Rockcraft will actually build.
ROCKCRAFT_FILE = "rockcraft.yaml"

#: The REST code-search endpoint speaks the *legacy* query language, where
#: ``path:`` matches a directory and only ``filename:`` matches a basename —
#: ``path:rockcraft.yaml`` returns zero hits there, however well it works in
#: the web UI. Don't "modernise" this without re-checking total_count.
DEFAULT_QUERY = "filename:rockcraft.yaml"
SOURCE = "github-code-search"

#: GitHub refuses to hand out more than this many results for one query.
SEARCH_CAP = 1000
PER_PAGE = 100

#: Byte size of the largest bucket the initial partition covers explicitly;
#: anything larger is swept up by a single open-ended ``size:>N`` bucket.
#: Rockcraft files are small — a 64 KiB ceiling puts every real one below it.
INITIAL_MAX_SIZE = 65536

#: Code search allows 30 requests/minute for an authenticated user.
DEFAULT_SLEEP = 2.0

#: Everything else shares the 5000/hour core limit — a much lighter touch.
DEFAULT_CORE_SLEEP = 0.1

FIELDNAMES = [
    "Team",
    "Rock Name",
    "Repository",
    "Branch (if not the default)",
    "Source",
    "Rockcraft Path",
    "Stars",
    "Last Push",
    "Archived",
    "Fork",
    "Notes",
]


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class HttpClient(Protocol):
    """Minimal GitHub API client — just enough for search + repo lookups."""

    def get_json(self, path: str, params: dict[str, str | int] | None = None) -> dict: ...


class RateLimitError(RuntimeError):
    """Raised when a rate limit could not be waited out within the retry budget."""


@dataclass
class GitHubClient:
    """Token-authenticated GitHub REST client with rate-limit backoff.

    GitHub meters code search and the rest of the API separately — 30
    requests/minute for ``/search/*`` against 5000/hour for everything else —
    so the two are paced separately too: ``sleep_between`` for search,
    ``core_sleep`` for repo and contents lookups. Pacing the (far more
    numerous) repo lookups at the search rate turned a one-minute enrichment
    pass into a twenty-minute one.

    Both the primary limit (403 + ``x-ratelimit-remaining: 0``) and the
    secondary abuse limit (403/429 + ``retry-after``) are honoured by sleeping
    until the stated time and retrying, up to ``retries`` times per request.

    ``sleeper``/``now`` are injected so tests can run without wall-clock waits.
    """

    token: str
    sleep_between: float = DEFAULT_SLEEP
    core_sleep: float = DEFAULT_CORE_SLEEP
    retries: int = 3
    timeout: float = 30.0
    sleeper: Callable[[float], None] = time.sleep
    now: Callable[[], float] = time.time
    _first: bool = True

    def get_json(self, path: str, params: dict[str, str | int] | None = None) -> dict:
        """GET ``path`` (absolute URL or ``/``-rooted API path) and parse JSON."""
        url = path if path.startswith("http") else f"{GITHUB_API}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        pace = self.sleep_between if path.startswith("/search/") else self.core_sleep
        for attempt in range(1, self.retries + 1):
            if self._first:
                self._first = False
            elif pace:
                self.sleeper(pace)
            try:
                return self._request(url)
            except urllib.error.HTTPError as exc:
                delay = self._rate_limit_delay(exc)
                if delay is None or attempt == self.retries:
                    raise
                print(f"  rate limited ({exc.code}); sleeping {delay:.0f}s", file=sys.stderr)
                self.sleeper(delay)
        raise RateLimitError(f"gave up on {url} after {self.retries} attempts")

    def _request(self, url: str) -> dict:
        req = urllib.request.Request(  # ruff: ignore[suspicious-url-open-usage] — https URL built from constants
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "charmtally-rockfind",
            },
        )
        # ruff: ignore[suspicious-url-open-usage]
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode())

    def _rate_limit_delay(self, exc: urllib.error.HTTPError) -> float | None:
        """Seconds to wait before retrying ``exc``, or None if it isn't a limit."""
        if exc.code not in (403, 429):
            return None
        headers = exc.headers or {}
        retry_after = headers.get("retry-after")
        if retry_after:
            try:
                return max(1.0, float(retry_after))
            except ValueError:
                return 60.0
        if headers.get("x-ratelimit-remaining") == "0":
            reset = headers.get("x-ratelimit-reset")
            try:
                return max(1.0, float(reset) - self.now()) if reset else 60.0
            except ValueError:
                return 60.0
        return None


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def _noop(_message: str) -> None:
    pass


def _size_query(base: str, lo: int, hi: int | None) -> str:
    """Return ``base`` narrowed to files of ``lo..hi`` bytes (``hi=None`` → open)."""
    return f"{base} size:>={lo}" if hi is None else f"{base} size:{lo}..{hi}"


def count(client: HttpClient, query: str) -> int:
    """Return how many code-search hits ``query`` has (one cheap request)."""
    data = client.get_json("/search/code", {"q": query, "per_page": 1})
    return int(data.get("total_count") or 0)


def search_pages(client: HttpClient, query: str) -> Iterator[dict]:
    """Yield every code-search item for ``query``, stopping at the 1000 cap."""
    seen = 0
    page = 1
    while seen < SEARCH_CAP:
        data = client.get_json("/search/code", {"q": query, "per_page": PER_PAGE, "page": page})
        items = data.get("items") or []
        if not items:
            return
        yield from items
        seen += len(items)
        if seen >= int(data.get("total_count") or 0):
            return
        page += 1


def search_partitioned(
    client: HttpClient,
    base_query: str = DEFAULT_QUERY,
    *,
    initial_max: int = INITIAL_MAX_SIZE,
    log: Callable[[str], None] = _noop,
) -> list[dict]:
    """Run ``base_query`` in ``size:`` buckets, splitting any that hit the cap.

    Returns the deduplicated union of the items from every bucket. A bucket
    over the cap that can no longer be halved (``lo == hi``) is logged as
    truncated: those results are incomplete and nothing here can fix that
    short of a different partition key.
    """
    items: dict[tuple[str, str], dict] = {}

    def walk(lo: int, hi: int | None) -> None:
        query = _size_query(base_query, lo, hi)
        total = count(client, query)
        if total == 0:
            return
        if total > SEARCH_CAP:
            if hi is None:
                log(f"  {query}: {total} hits — splitting")
                walk(lo, lo * 2)
                walk(lo * 2 + 1, None)
                return
            if hi > lo:
                log(f"  {query}: {total} hits — splitting")
                mid = (lo + hi) // 2
                walk(lo, mid)
                walk(mid + 1, hi)
                return
            log(
                f"warning: {query} has {total} hits but cannot be split further; "
                f"only the first {SEARCH_CAP} are visible"
            )
        log(f"  {query}: {total} hits")
        for item in search_pages(client, query):
            repo = (item.get("repository") or {}).get("full_name") or ""
            path = item.get("path") or ""
            if repo and path:
                items[repo, path] = item

    walk(0, initial_max)
    walk(initial_max + 1, None)
    return list(items.values())


# ---------------------------------------------------------------------------
# Rock refs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RockRef:
    """One ``rockcraft.yaml`` found in a public repo."""

    team: str
    name: str
    repo_url: str
    branch: str | None
    path: str
    stars: int | None = None
    last_push: str = ""
    archived: bool = False
    fork: bool = False
    #: "public", "private" or "internal". Only "public" is ever written out —
    #: see `public_only`.
    visibility: str = "public"
    source: str = SOURCE
    notes: str = ""

    @property
    def full_name(self) -> str:
        """``owner/repo`` — the API's repo identifier."""
        return self.repo_url.removeprefix("https://github.com/")

    @property
    def key(self) -> tuple[str, str]:
        """Identity for dedup/merge: the repo plus the file's path within it."""
        return (self.repo_url, self.path)

    def to_row(self) -> dict[str, str]:
        """Render as a CSV row."""
        return {
            "Team": self.team,
            "Rock Name": self.name,
            "Repository": self.repo_url,
            "Branch (if not the default)": self.branch or "",
            "Source": self.source,
            "Rockcraft Path": self.path,
            "Stars": "" if self.stars is None else str(self.stars),
            "Last Push": self.last_push,
            "Archived": "TRUE" if self.archived else "FALSE",
            "Fork": "TRUE" if self.fork else "FALSE",
            "Notes": self.notes,
        }

    @classmethod
    def from_row(cls, row: dict[str, str]) -> RockRef | None:
        """Parse a CSV row, or return None if it carries no repository."""
        repo = (row.get("Repository") or "").strip()
        if not repo:
            return None
        stars = (row.get("Stars") or "").strip()
        return cls(
            team=(row.get("Team") or "").strip(),
            name=(row.get("Rock Name") or "").strip(),
            repo_url=repo,
            branch=(row.get("Branch (if not the default)") or "").strip() or None,
            path=(row.get("Rockcraft Path") or "").strip(),
            stars=int(stars) if stars.isdigit() else None,
            last_push=(row.get("Last Push") or "").strip(),
            archived=(row.get("Archived") or "").strip().upper() == "TRUE",
            fork=(row.get("Fork") or "").strip().upper() == "TRUE",
            source=(row.get("Source") or SOURCE).strip(),
            notes=(row.get("Notes") or "").strip(),
        )


def _name_from_path(full_name: str, path: str) -> str:
    """Guess the rock's name from where its file sits.

    ``rockcraft.yaml`` at the root → the repo name; ``foo/bar/rockcraft.yaml``
    → ``bar``. Only a guess: ``--names`` reads the declared name instead.
    """
    parent = path.rsplit("/", 1)[0] if "/" in path else ""
    if not parent:
        return full_name.rsplit("/", 1)[-1]
    return parent.rsplit("/", 1)[-1]


def refs_from_items(items: Iterable[dict]) -> list[RockRef]:
    """Turn raw code-search items into `RockRef`s (no extra API calls).

    Drops hits whose basename is not exactly ``rockcraft.yaml``: the search
    endpoint's ``filename:`` is a prefix-ish match, so it returns things like
    ``rockcraft.yaml.j2``.
    """
    out: list[RockRef] = []
    for item in items:
        repo = item.get("repository") or {}
        full_name = repo.get("full_name") or ""
        path = item.get("path") or ""
        if not full_name or not path:
            continue
        if path.rsplit("/", 1)[-1] != ROCKCRAFT_FILE:
            # `filename:rockcraft.yaml` matches `rockcraft.yaml.j2` too —
            # a template that generates a rock, not a rock. Same fuzziness
            # that makes `path:` mean "directory" on this endpoint.
            continue
        out.append(
            RockRef(
                team=(repo.get("owner") or {}).get("login") or full_name.split("/")[0],
                name=_name_from_path(full_name, path),
                repo_url=f"https://github.com/{full_name}",
                branch=None,
                path=path,
                fork=bool(repo.get("fork")),
                visibility="private" if repo.get("private") else "public",
            )
        )
    return sorted(out, key=lambda r: r.key)


def enrich(
    client: HttpClient,
    refs: Iterable[RockRef],
    *,
    log: Callable[[str], None] = _noop,
) -> list[RockRef]:
    """Fill in stars / last push / archived / fork from the repos endpoint.

    One request per distinct repo, not per rock, so a monorepo shipping twenty
    rocks costs one call. A repo that fails to fetch is kept as-is: a missing
    star count is not worth dropping a candidate over.
    """
    cache: dict[str, dict] = {}
    out: list[RockRef] = []
    for ref in refs:
        meta = cache.get(ref.full_name)
        if meta is None:
            try:
                meta = client.get_json(f"/repos/{ref.full_name}")
            except (urllib.error.HTTPError, urllib.error.URLError, RateLimitError) as exc:
                log(f"warning: could not read {ref.full_name}: {exc}")
                meta = {}
            cache[ref.full_name] = meta
        out.append(
            replace(
                ref,
                stars=meta.get("stargazers_count", ref.stars),
                last_push=(meta.get("pushed_at") or ref.last_push or "")[:10],
                archived=bool(meta.get("archived", ref.archived)),
                fork=bool(meta.get("fork", ref.fork)),
                visibility=_visibility(meta, ref.visibility),
            )
        )
    return out


def _visibility(meta: dict, fallback: str) -> str:
    """Read a repo's visibility from its API payload.

    ``visibility`` is the precise answer ("public" / "private" / "internal")
    but only newer payloads carry it; ``private`` is the older boolean and is
    true for internal repos too. A failed lookup (empty ``meta``) keeps
    whatever the search payload said rather than assuming the repo is public.
    """
    declared = meta.get("visibility")
    if declared:
        return str(declared)
    if "private" in meta:
        return "private" if meta["private"] else "public"
    return fallback


def public_only(refs: Iterable[RockRef], *, log: Callable[[str], None] = _noop) -> list[RockRef]:
    """Drop every repo that is not public.

    Not optional and not a flag: code search returns the private and internal
    repos the token can reach, and a corpus of public rocks must not carry
    their names. Anything whose visibility could not be established is dropped
    too — an unknown repo is not a public one.
    """
    all_refs = list(refs)
    kept = [ref for ref in all_refs if ref.visibility == "public"]
    dropped = len(all_refs) - len(kept)
    if dropped:
        log(f"… dropped {dropped} private/internal repos (not public; never written out)")
    return kept


def resolve_names(
    client: HttpClient,
    refs: Iterable[RockRef],
    *,
    log: Callable[[str], None] = _noop,
) -> list[RockRef]:
    """Replace guessed names with the ``name:`` declared in each rockcraft.yaml.

    Costs one request per rock — the expensive option, hence opt-in. A file
    that will not fetch or parse keeps its path-derived guess.
    """
    out: list[RockRef] = []
    for ref in refs:
        name = ref.name
        try:
            payload = client.get_json(f"/repos/{ref.full_name}/contents/{ref.path}")
            declared = _declared_name(payload)
            if declared:
                name = declared
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            RateLimitError,
            ValueError,
            yaml.YAMLError,
        ) as exc:
            log(f"warning: could not read {ref.full_name}/{ref.path}: {exc}")
        out.append(replace(ref, name=name))
    return out


def _declared_name(payload: dict) -> str | None:
    """Pull ``name:`` out of a base64 contents payload, or None."""
    content = payload.get("content")
    if not content or payload.get("encoding") != "base64":
        return None
    text = base64.b64decode(content).decode("utf-8", errors="replace")
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        return None
    name = data.get("name")
    return str(name).strip() if name else None


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


def load(path: Path) -> list[RockRef]:
    """Load a rocks corpus CSV written by this tool."""
    with path.open(newline="") as f:
        return [ref for row in csv.DictReader(f) if (ref := RockRef.from_row(row))]


def write(path: Path, refs: Iterable[RockRef]) -> int:
    """Write ``refs`` as CSV, sorted by repo then path. Returns the row count."""
    rows = sorted(refs, key=lambda r: r.key)
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        for ref in rows:
            writer.writerow(ref.to_row())
    return len(rows)


def merge(existing: Iterable[RockRef], found: Iterable[RockRef]) -> list[RockRef]:
    """Merge a fresh search into a curated CSV, keeping the hand-edited bits.

    ``Team`` and ``Notes`` are the columns a human curates, so a non-empty
    existing value wins over anything the search inferred. Rows that the
    search no longer returns are retained rather than deleted — a repo can
    drop out because it went private or fell out of the index, and silently
    shrinking a curated corpus is worse than carrying a stale row.
    """
    by_key = {ref.key: ref for ref in existing}
    for ref in found:
        old = by_key.get(ref.key)
        if old is None:
            by_key[ref.key] = ref
            continue
        by_key[ref.key] = replace(
            ref,
            team=old.team or ref.team,
            notes=old.notes,
            branch=old.branch or ref.branch,
        )
    return sorted(by_key.values(), key=lambda r: r.key)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _token(explicit: str | None) -> str:
    token = explicit or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise SystemExit(
            "error: GitHub code search requires authentication; set GITHUB_TOKEN "
            "(or pass --token). A token with no scopes is enough for public repos."
        )
    return token


def cmd_search(args: argparse.Namespace) -> int:
    """Search GitHub for rockcraft.yaml files and write the corpus CSV."""

    def log(message: str) -> None:
        print(message, file=sys.stderr)

    client = GitHubClient(
        token=_token(args.token), sleep_between=args.sleep, core_sleep=args.core_sleep
    )
    log(f"… searching GitHub for {args.query!r}")
    items = search_partitioned(client, args.query, log=log)
    refs = public_only(refs_from_items(items), log=log)
    log(f"… {len(refs)} rockcraft.yaml files in {len({r.repo_url for r in refs})} public repos")

    if not args.no_enrich:
        log("… fetching repo metadata")
        # Re-filter: the repos endpoint knows about internal visibility, which
        # the search payload's `private` boolean cannot distinguish.
        refs = public_only(enrich(client, refs, log=log), log=log)
    if args.names:
        log("… reading declared rock names")
        refs = resolve_names(client, refs, log=log)

    kept = [
        ref
        for ref in refs
        if (args.include_archived or not ref.archived) and (args.include_forks or not ref.fork)
    ]
    dropped = len(refs) - len(kept)
    if dropped:
        log(f"… dropped {dropped} archived/fork rows (--include-archived/--include-forks to keep)")

    if args.merge is not None and args.merge.exists():
        before = load(args.merge)
        kept = merge(before, kept)
        log(f"… merged with {len(before)} existing rows from {args.merge}")

    if len(kept) < args.min_rows:
        # A scoped token, a query-language change (`path:` silently returning
        # zero is not hypothetical), or a search outage all look like "the
        # corpus shrank" rather than an error. Refuse to write in that case:
        # an unattended run must not hand a plausible-looking but gutted CSV
        # to whatever consumes it next.
        log(
            f"error: only {len(kept)} rows found, expected at least {args.min_rows}; "
            f"refusing to write {args.out}"
        )
        return 1

    written = write(args.out, kept)
    log(f"→ wrote {written} rows to {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m charmtally.tools.rockfind``."""
    parser = argparse.ArgumentParser(
        prog="rockfind", description="Build a corpus CSV of public repos containing rocks."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    search = sub.add_parser("search", help="search GitHub code for rockcraft.yaml files")
    search.add_argument("--out", type=Path, default=Path("rocks.csv"), help="output CSV")
    search.add_argument("--query", default=DEFAULT_QUERY, help="code search query")
    search.add_argument("--token", default=None, help="GitHub token (else GITHUB_TOKEN/GH_TOKEN)")
    search.add_argument(
        "--sleep", type=float, default=DEFAULT_SLEEP, help="seconds between code-search requests"
    )
    search.add_argument(
        "--core-sleep",
        type=float,
        default=DEFAULT_CORE_SLEEP,
        help="seconds between repo/contents requests",
    )
    search.add_argument("--names", action="store_true", help="read name: from each rockcraft.yaml")
    search.add_argument("--no-enrich", action="store_true", help="skip per-repo metadata lookups")
    search.add_argument("--include-forks", action="store_true", help="keep forked repos")
    search.add_argument("--include-archived", action="store_true", help="keep archived repos")
    search.add_argument(
        "--merge", type=Path, default=None, help="merge into this existing CSV, keeping Team/Notes"
    )
    search.add_argument(
        "--min-rows",
        type=int,
        default=0,
        help="fail without writing if fewer than this many rows were found",
    )
    search.set_defaults(func=cmd_search)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
