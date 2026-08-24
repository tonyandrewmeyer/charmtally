"""Scan the rocks corpus for the handful of facts `rockcraft.yaml` carries.

Rocks are **not cloned**. Everything the adoption scorecard asks of a rock
today lives in one file whose path `rocks.csv` already records, so a scan is
a few hundred `raw.githubusercontent.com` GETs rather than a few hundred
clones — seconds, no workdir, no cache to invalidate. If a future metric
needs to see the rest of a rock's tree, that is the point to reach for the
charm-side clone machinery; until then this is deliberately the cheap path.

`rocks.csv` is source data curated by hand (see `tools/rockfind.py`), and
carries `Archived` / `Fork` columns. Both are dropped here: a fork's
`run-user` says nothing about the fork owner's practice, and an archived
repo's says nothing about current practice.

The scan is read-only over public repos — `rockfind.public_only` already
dropped every private hit before the CSV was written — so no token is
needed, and an unauthenticated raw fetch is the whole mechanism.
"""

from __future__ import annotations

import csv
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

RAW_BASE = "https://raw.githubusercontent.com"
#: Parallel fetches. The ceiling is politeness, not throughput: ~900 small
#: files against a CDN, and raw.githubusercontent is not the API rate limiter.
DEFAULT_WORKERS = 16
#: `run-user`'s only supported value today (UID/GID 584792). Recorded rather
#: than assumed: rockcraft may add shared users, and a value we don't know
#: should surface as itself rather than be silently bucketed as root.
DAEMON_USER = "_daemon_"


@dataclass(frozen=True)
class RockRef:
    """One row of `rocks.csv`, reduced to what a scan needs."""

    name: str
    repo_url: str
    path: str  # rockcraft.yaml path, relative to the repo root
    branch: str = ""  # empty means "the repo's default branch"
    team: str = ""

    @property
    def slug(self) -> str:
        """Stable identity for this rock across scans.

        `owner/repo` plus the rockcraft path, because a monorepo holds many
        rocks and the rock *name* alone collides across repos (a dozen forks
        of katib all define `file-metrics-collector`).
        """
        return f"{_repo_path(self.repo_url)}:{self.path}"


def _repo_path(repo_url: str) -> str:
    """`https://github.com/canonical/foo.git` → `canonical/foo`."""
    trimmed = repo_url.strip().rstrip("/")
    if trimmed.endswith(".git"):
        trimmed = trimmed[: -len(".git")]
    _, _, tail = trimmed.partition("github.com/")
    return tail or trimmed


def _is_true(value: str) -> bool:
    return value.strip().upper() == "TRUE"


def load_csv(path: Path) -> list[RockRef]:
    """Read `rocks.csv`, dropping archived repos and forks.

    Rows missing a repository or a rockcraft path are dropped too: both are
    required to build a fetch URL, and a row without them is a curation
    error rather than a rock that happens to be unreadable.
    """
    refs: list[RockRef] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if _is_true(row.get("Archived", "")) or _is_true(row.get("Fork", "")):
                continue
            repo = (row.get("Repository") or "").strip()
            rock_path = (row.get("Rockcraft Path") or "").strip()
            if not repo or not rock_path:
                continue
            refs.append(
                RockRef(
                    name=(row.get("Rock Name") or "").strip(),
                    repo_url=repo,
                    path=rock_path,
                    branch=(row.get("Branch (if not the default)") or "").strip(),
                    team=(row.get("Team") or "").strip(),
                )
            )
    return refs


def raw_url(ref: RockRef) -> str:
    """Raw-content URL for this rock's `rockcraft.yaml`.

    Uses the `HEAD` ref when the CSV names no branch — raw resolves it to the
    repo's default branch, so the scan doesn't need an API call per row just
    to learn whether the default is `main` or `master`.
    """
    return f"{RAW_BASE}/{_repo_path(ref.repo_url)}/{ref.branch or 'HEAD'}/{ref.path.lstrip('/')}"


def fetch_text(url: str, *, timeout: float = 30.0) -> str | None:
    """GET `url`, or None if it can't be read.

    Every failure mode is the same to a caller — the file isn't readable, so
    the rock has no facts this scan — and none of them should abort a sweep
    over hundreds of repos.
    """
    try:
        # ruff: ignore[suspicious-url-open-usage] — URL is built from RAW_BASE, always https.
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def facts_from_rockcraft(text: str) -> dict | None:
    """Extract the scanned facts from one `rockcraft.yaml`, or None if unparsable.

    `run_user` is None when the key is absent, which is rockcraft's own
    default and means the rock runs as root — a real reading, not a gap. An
    unparsable file is the gap, and is reported as one.
    """
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if not isinstance(doc, dict):
        return None
    run_user = doc.get("run-user")
    return {
        "run_user": run_user if isinstance(run_user, str) else None,
        "name": doc.get("name") if isinstance(doc.get("name"), str) else None,
    }


def scan_rock(ref: RockRef, fetch: Callable[[str], str | None] = fetch_text) -> dict:
    """Scan one rock. `readable` false means nothing could be read for it."""
    record: dict = {
        "name": ref.name,
        "repo_url": ref.repo_url,
        "path": ref.path,
        "team": ref.team,
        "readable": False,
        "run_user": None,
    }
    text = fetch(raw_url(ref))
    if text is None:
        return record
    facts = facts_from_rockcraft(text)
    if facts is None:
        return record
    record["readable"] = True
    record["run_user"] = facts["run_user"]
    return record


def scan_rocks(
    refs: Iterable[RockRef],
    *,
    fetch: Callable[[str], str | None] = fetch_text,
    workers: int = DEFAULT_WORKERS,
) -> dict[str, dict]:
    """Scan every ref concurrently, keyed by `RockRef.slug`.

    Unreadable rocks are kept in the result with `readable: False` rather
    than dropped: a consumer needs to tell "this rock runs as root" from
    "this rock could not be read", and dropping them would collapse the two.
    """
    ordered = list(refs)
    if not ordered:
        return {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        records = list(pool.map(lambda ref: scan_rock(ref, fetch=fetch), ordered))
    return {ref.slug: record for ref, record in zip(ordered, records, strict=True)}
