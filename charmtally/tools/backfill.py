r"""Reconstruct the weekly snapshot series for dates that were never scanned.

The scan is a pure function of a charm's checked-out tree, and git can put any
repo back the way it looked on any date — so a snapshot for a Monday that
predates this project can be *recomputed* rather than guessed::

    uv run python -m charmtally.tools.backfill \\
        --start 2026-01-01 --end 2026-06-08 \\
        --workdir /tmp/charmtally-backfill \\
        --overrides corpus-overrides.yaml

One `snapshots/scored-YYYY-MM-DD.json` per Monday in the range, in the same
shape `scan` → `score` writes, so `trend` and `adoption` pick them up with no
changes anywhere else.

Mechanics
---------
Two phases, because they have opposite cost profiles:

1. **Prepare** — one *full* clone per corpus repo (parallel, network-bound).
   Full, not `--depth 1`: shallow clones are exactly the thing that makes a
   historical checkout impossible. This does not share the weekly scan's
   workdir; point `--workdir` somewhere of its own and expect several GiB.
2. **Replay** — for each date, oldest first: check every repo out at its last
   commit before the cutoff and scan it, then write that date's snapshot.
   Entirely local, so the loop is CPU-bound and re-runnable offline.

Dates already present under `--snapshots-dir` are left alone unless `--force`
is passed, which makes an interrupted run resumable — and makes it hard to
clobber a snapshot that records a real scan. `--end` therefore defaults to
*today*: the range is a range to consider, not a range to write, and a gap in
the middle of the series is as much a gap as one before it. The earlier default
— stop the day before the first existing snapshot — quietly made the prefix the
only fillable region, which left a ragged join the one time the oldest observed
snapshots covered a far smaller corpus than the recomputed ones either side.

The corpus does not time-travel; the readings do
-----------------------------------------------
Every date is replayed against **one** corpus list — today's, or whatever
`--corpus` pins — as though we had known about every charm all along. Only the
readings move in time. That is deliberate: hyrum's `charms.csv` is a record of
who got round to listing a charm, not of when the charm appeared, so replaying
membership would mistake curation lag for adoption and put a step in every
metric on the week a batch of rows landed.

"We knew about it all along" is not the same as "it existed all along", and the
two are told apart per repo, per date:

* **Listed later, but already there** — the repo has commits before the cutoff,
  so it is checked out and scanned like any other. This is the case the fixed
  list exists to catch.
* **Actually new** — the repo's history starts *after* the cutoff. It is
  recorded in `__skipped__` as not yet created, with its first-commit date, and
  contributes to no count for that date. A charm that did not exist cannot have
  adopted anything, and must not sit in a denominator as though it had
  declined to.
* **Repo older than its charm** — the repo existed but held no
  `charmcraft.yaml` / `metadata.yaml` yet (a charm added to an existing repo, a
  monorepo's later sub-charms). Skipped too, with its own reason, because the
  charm did not exist even though the repo did.

The `__backfill__` block tallies those outcomes per date, so a jump in a metric
can be checked against a jump in the population that produced it.

What is and isn't faithful
--------------------------
Faithful: every charm's *code* is the code that was there on the date, and the
population is every charm that existed then, whenever it was listed.

Not faithful, by construction:

* **The feature catalogue and scoring rules are today's.** Backfilled points
  answer "how much of the current catalogue did the ecosystem use back then",
  which is the question the trend page asks. Snapshots taken at the time would
  each carry the catalogue of their week.
* **Charms that have since left the corpus are missing from every date.** The
  list is today's, so a repo deleted, made private, or dropped from
  `charms.csv` is absent even from dates when it was alive and listed. Fixing
  the list cures curation lag, not survivorship.
* **`corpus-overrides.yaml` is today's.** Exclusions written for a charm's
  current shape are applied to its historical shape.
* **The cutoff reads committer dates.** A branch rebased or squashed since the
  date carries the *rewrite's* dates, so such a charm is replayed at whatever
  its rewritten history says was current — usually a later tree than the one
  that really existed that week.

Rocks
-----
`--rocks` replays the other half. The weekly scan reads each `rockcraft.yaml`
straight off `raw.githubusercontent.com` at HEAD, which has no date in it, so
this mode clones instead — `git show <sha>:<path>` off one commit resolved per
repo, which is cheap even for a monorepo holding dozens of rocks::

    uv run python -m charmtally.tools.backfill --rocks \\
        --start 2026-01-01 --workdir /tmp/charmtally-backfill

It **merges** into snapshots that already exist rather than writing new ones:
the rocks half lives in the charm snapshot's `__rocks__` block, in the shape
`scan-rocks --into` writes, so `trend` and `adoption` cannot tell a replayed
block from a scanned one. Dates without a snapshot are skipped — there is
nothing to merge into — and dates that already have a `__rocks__` block are
left alone unless `--force`.

Absence splits three ways here too, and for the same reason: `NOT_YET_CREATED`
(the repo's history starts after the cutoff), `NO_ROCK_YET` (the repo was
there, this rockcraft.yaml was not), and `UNREADABLE`. The first two are left
out of `__rocks__` entirely rather than written as `readable: false`, which
means "we looked and failed" — a rock that did not exist was never there to
look at, and the rootless metric excludes both anyway.

Not faithful, by construction, on this side: `rocks.csv`'s membership is fixed
across dates like the charm corpus (it comes from a code search, so replaying
it would read indexing lag as adoption), its `Archived` / `Fork` columns are
today's and are applied to every date, and `git show` does not follow renames —
a rockcraft.yaml that has since moved reads as `NO_ROCK_YET` before the move.

Cost, at ~350 corpus repos: the prepare phase is the long pole (full clones,
minutes to tens of minutes and a few GiB), and each replayed date then costs
about what one weekly scan costs, minus the network.

Each snapshot records its own provenance in a `__backfill__` block (cutoff,
corpus origin, catalogue digest, per-outcome counts). Consumers skip
`__`-prefixed keys, so it rides along harmlessly — and a later reader can tell
a recomputed point from a scanned one.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .. import catalogue, corpus, scan
from .. import rocks as _rocks
from .. import trend as _trend
from ..cli import _apply_feature_excludes

if TYPE_CHECKING:
    from collections.abc import Iterable

    from ..corpus import CharmRef
    from ..rocks import RockRef

#: Time of day the cutoff lands on, mirroring `scan.yaml`'s 02:00 UTC cron so
#: a backfilled Monday sees what that Monday's run would have seen.
CUTOFF_TIME = "02:00:00 +0000"

#: Weekday the weekly series lands on (Monday, matching the cron).
DEFAULT_WEEKDAY = 0

DEFAULT_JOBS = 8

#: Per-repo outcomes for one date, tallied into the provenance block. The two
#: middle ones are both "no charm here yet" to a counter and different things
#: to a reader, which is the distinction the fixed corpus list makes necessary.
SCANNED = "scanned"
NOT_YET_CREATED = "not-yet-created"
NO_CHARM_YET = "no-charm-yet"
UNREADABLE = "unreadable"
EXCLUDED = "excluded"

#: The rocks-side sibling of `NO_CHARM_YET`: the repo was there on the date,
#: but this rockcraft.yaml was not in it yet.
NO_ROCK_YET = "no-rock-yet"

#: Default `rocks.csv`, matching what the weekly `scan-rocks` reads.
DEFAULT_ROCKS_CSV = Path("rocks.csv")


# ── dates ───────────────────────────────────────────────────────────────────


def weekly_dates(start: dt.date, end: dt.date, weekday: int = DEFAULT_WEEKDAY) -> list[dt.date]:
    """Every `weekday` from `start` to `end` inclusive, oldest first.

    `start` is a boundary, not necessarily a scan date: the first date
    returned is the first matching weekday on or after it, so
    `--start 2026-01-01` (a Thursday) yields Mondays from 2026-01-05.
    """
    if end < start:
        return []
    first = start + dt.timedelta(days=(weekday - start.weekday()) % 7)
    out: list[dt.date] = []
    cursor = first
    while cursor <= end:
        out.append(cursor)
        cursor += dt.timedelta(days=7)
    return out


def cutoff(date: dt.date) -> str:
    """Build the `git rev-list --before` argument for a snapshot dated `date`."""
    return f"{date.isoformat()} {CUTOFF_TIME}"


# ── git plumbing ────────────────────────────────────────────────────────────


def _git_raw(args: list[str], cwd: Path | None = None, timeout: int = 300) -> str | None:
    """Run a git command and return its stdout verbatim, or None on failure.

    Verbatim because `git show <sha>:<path>` returns a *file*, and stripping
    it would quietly edit the bytes the parser sees.
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=None if cwd is None else str(cwd),
            check=True,
            capture_output=True,
            timeout=timeout,
            text=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None
    return proc.stdout


def _git_out(args: list[str], cwd: Path | None = None, timeout: int = 300) -> str | None:
    """Run a git command and return its stdout stripped, or None on failure.

    `scan._git` answers only pass/fail, and every call here wants the output
    (a SHA, a ref name) — so this is a sibling of it, not a replacement.
    """
    out = _git_raw(args, cwd, timeout)
    return None if out is None else out.strip()


def ensure_full_clone(repo_url: str, dest: Path, *, branch: str | None = None) -> Path | None:
    """Clone `repo_url` into `dest` with full history, or refresh what's there.

    A clone left behind by the weekly `scan` is shallow; `--unshallow` it
    rather than refusing, so pointing this at an existing workdir degrades to
    "slow first run" instead of "every checkout fails".

    Returns None if the repo could not be made available at all.
    """
    if dest.exists():
        if (dest / ".git" / "shallow").is_file():
            _git_out(["fetch", "--quiet", "--unshallow", "origin"], dest)
        _git_out(
            ["fetch", "--quiet", "--prune", "origin", "+refs/heads/*:refs/remotes/origin/*"], dest
        )
        _git_out(["remote", "set-head", "origin", "--auto"], dest)
        return dest if (dest / ".git").exists() else None
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["clone", "--quiet", "--no-single-branch"]
    if branch:
        cmd += ["--branch", branch]
    cmd += [repo_url, str(dest)]
    if _git_out(cmd) is None:
        return None
    _git_out(["remote", "set-head", "origin", "--auto"], dest)
    return dest


def tracking_ref(dest: Path, branch: str | None) -> str | None:
    """Return the ref to walk history on: `origin/<branch>` or the repo default.

    The default branch is read from `refs/remotes/origin/HEAD` (which
    `ensure_full_clone` asks git to set) rather than assumed to be `main`:
    a good slice of the corpus is still on `master`, and guessing wrong looks
    identical to "this repo had no commits yet".
    """
    if branch:
        return f"origin/{branch}"
    head = _git_out(["symbolic-ref", "-q", "refs/remotes/origin/HEAD"], dest)
    if head and head.startswith("refs/remotes/"):
        return head[len("refs/remotes/") :]
    for candidate in ("origin/main", "origin/master"):
        if _git_out(["rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}"], dest):
            return candidate
    return None


def commit_asof(dest: Path, ref: str, date: dt.date) -> str | None:
    """SHA of the last commit on `ref` before `date`'s cutoff, or None if there is none.

    None is a meaningful answer, not an error: the repo existed later than
    the date being replayed.
    """
    sha = _git_out(["rev-list", "-1", f"--before={cutoff(date)}", ref], dest)
    return sha or None


def first_commit_date(dest: Path, ref: str) -> str | None:
    """Return the date of the oldest commit on `ref`, or None if unreadable.

    Only asked for when `commit_asof` came up empty, to say *how* new a repo
    is rather than just that it is newer than the date being replayed.
    """
    out = _git_out(["log", "--reverse", "--date=short", "--format=%cd", ref], dest)
    if not out:
        return None
    return out.splitlines()[0].strip() or None


def checkout(dest: Path, sha: str) -> bool:
    """Detach the working tree at `sha`, discarding whatever the last date left.

    `clean -xfd` matters as much as the checkout: a `__pycache__` or a
    `.tox` left over from an earlier date's tree is invisible to `git
    checkout` and visible to the detectors' file walk.
    """
    if _git_out(["checkout", "--quiet", "--force", "--detach", sha], dest) is None:
        return False
    _git_out(["clean", "--quiet", "-xfd"], dest)
    return True


# ── the corpus list ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CorpusSource:
    """The single corpus list every date is replayed against.

    Single on purpose — see the module docstring. `origin` is recorded in each
    snapshot so a series built against a pinned CSV can be told from one built
    against the live list.
    """

    path: Path
    origin: str  # the URL it came from, or "local"


def resolve_corpus(path: Path | None, url: str, workdir: Path) -> CorpusSource:
    """Return the corpus list to use for every date.

    `--corpus` wins when given; otherwise the current list is fetched once and
    cached in the workdir, which is also what makes a re-run reproducible
    without the network.
    """
    if path is not None:
        return CorpusSource(path, "local")
    return CorpusSource(corpus.fetch_to(url, workdir / "corpus.csv"), url)


# ── the readings, at a date ─────────────────────────────────────────────────


def unique_refs(refs: Iterable[CharmRef]) -> list[CharmRef]:
    """Drop repeat rows for the same repo, keeping CSV order — as `scan` does."""
    seen: set[str] = set()
    out: list[CharmRef] = []
    for ref in refs:
        if ref.repo_url not in seen:
            seen.add(ref.repo_url)
            out.append(ref)
    return out


def catalogue_digest(features_path: Path) -> str:
    """Short digest of the catalogue used, for the provenance block.

    Backfilled points are only comparable to each other while this is
    unchanged; recording it makes a mixed series diagnosable instead of
    mysterious.
    """
    return hashlib.sha256(features_path.read_bytes()).hexdigest()[:12]


@dataclass
class RepoOutcome:
    """What one repo produced for one date.

    `kind` is the repo-level verdict the provenance block tallies; `records`
    and `skipped` are merged into the snapshot as-is. A monorepo can carry
    both — some sub-charms scanned, others excluded by overrides — so the two
    are not alternatives.
    """

    kind: str
    records: dict[str, dict] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)


def scan_repo_asof(
    ref: CharmRef,
    dest: Path,
    date: dt.date,
    feats: list,
    pats: list,
    overrides: corpus.CorpusOverrides,
) -> RepoOutcome:
    """Scan one repo as it stood on `date`.

    Mirrors `cli.cmd_scan`'s per-ref handling — monorepo fan-out, sub-charm
    and feature excludes, the same slug scheme — because the output has to be
    indistinguishable from a real snapshot's for `trend` to read the two in
    one series.

    The two ways a listed charm can be absent from a date are kept apart:
    `NOT_YET_CREATED` (no history before the cutoff — the repo itself is
    newer) and `NO_CHARM_YET` (history, but no charm files in it yet). Both
    land in `__skipped__` with a reason naming the date, so neither is read
    later as a charm that had the chance to adopt something and didn't.
    """
    track = tracking_ref(dest, ref.branch)
    if track is None:
        return RepoOutcome(UNREADABLE, skipped={ref.slug: "no remote-tracking branch to replay"})
    sha = commit_asof(dest, track, date)
    if sha is None:
        born = first_commit_date(dest, track)
        detail = f"first commit {born}" if born else "no commit history readable"
        return RepoOutcome(
            NOT_YET_CREATED,
            skipped={ref.slug: f"repo did not exist on {date.isoformat()} ({detail})"},
        )
    if not checkout(dest, sha):
        return RepoOutcome(UNREADABLE, skipped={ref.slug: f"checkout of {sha[:12]} failed"})

    charm_roots = scan.find_charm_roots(dest)
    if not charm_roots:
        return RepoOutcome(
            NO_CHARM_YET,
            skipped={
                ref.slug: (
                    f"repo exists but has no charmcraft.yaml or metadata.yaml "
                    f"at {date.isoformat()}"
                )
            },
        )

    outcome = RepoOutcome(SCANNED)
    if len(charm_roots) == 1 and charm_roots[0] == dest:
        features = scan.scan_charm(dest, feats, pats)
        _apply_feature_excludes(features, overrides, ref.repo_url, "")
        outcome.records[ref.slug] = {
            "name": ref.name,
            "team": ref.team,
            "repo_url": ref.repo_url,
            "features": features,
        }
        return outcome

    for sub in charm_roots:
        rel = sub.relative_to(dest)
        sub_slug = f"{ref.slug}/{rel}"
        reason = overrides.sub_charm_skip_reason(ref.repo_url, str(rel))
        if reason:
            outcome.skipped[sub_slug] = reason
            continue
        features = scan.scan_charm(sub, feats, pats)
        _apply_feature_excludes(features, overrides, ref.repo_url, str(rel))
        outcome.records[sub_slug] = {
            "name": f"{ref.name}/{rel}",
            "team": ref.team,
            "repo_url": ref.repo_url,
            "subpath": str(rel),
            "features": features,
        }
    return outcome


def snapshot_for_date(
    date: dt.date,
    refs: list[CharmRef],
    workdir: Path,
    feats: list,
    pats: list,
    overrides: corpus.CorpusOverrides,
    features_path: Path,
    corpus_source: CorpusSource,
) -> dict:
    """Replay every corpus repo at `date` and return the snapshot dict.

    `refs` is the same list for every date: membership is fixed, only the
    readings move.
    """
    clones = workdir / "charms"

    out: dict[str, object] = {}
    skipped: dict[str, str] = {}
    tally = dict.fromkeys((SCANNED, NOT_YET_CREATED, NO_CHARM_YET, UNREADABLE, EXCLUDED), 0)
    for index, ref in enumerate(refs, start=1):
        adjusted, exclude_reason = overrides.apply(ref)
        if adjusted is None:
            skipped[ref.slug] = exclude_reason or "excluded by corpus-overrides.yaml"
            tally[EXCLUDED] += 1
            continue
        ref = adjusted
        dest = clones / ref.slug
        print(f"  [{index}/{len(refs)}] {ref.name}", file=sys.stderr)
        if not (dest / ".git").exists():
            skipped[ref.slug] = "no local clone (prepare phase failed?)"
            tally[UNREADABLE] += 1
            continue
        outcome = scan_repo_asof(ref, dest, date, feats, pats, overrides)
        out.update(outcome.records)
        skipped.update(outcome.skipped)
        tally[outcome.kind] += 1

    if skipped:
        out["__skipped__"] = skipped
    out["__backfill__"] = {
        "cutoff": cutoff(date),
        "corpus_origin": corpus_source.origin,
        "corpus_fixed_across_dates": True,
        "catalogue_digest": catalogue_digest(features_path),
        "outcomes": tally,
        "rocks": "not scanned by this pass (see --rocks)",
        "tool": "charmtally.tools.backfill",
    }
    return out


# ── prepare phase ───────────────────────────────────────────────────────────


def prepare_clones(refs: list[CharmRef], workdir: Path, *, jobs: int = DEFAULT_JOBS) -> int:
    """Full-clone (or refresh) every repo. Returns the number that failed.

    Parallel because it is all network. Failures are counted, not raised: one
    dead repo out of ~350 should cost that repo, not the run.
    """
    clones = workdir / "charms"
    clones.mkdir(parents=True, exist_ok=True)
    total = len(refs)
    done = 0

    def one(ref: CharmRef) -> bool:
        return ensure_full_clone(ref.repo_url, clones / ref.slug, branch=ref.branch) is not None

    failures = 0
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        for ref, ok in zip(refs, pool.map(one, refs), strict=True):
            done += 1
            if not ok:
                failures += 1
                print(f"  [{done}/{total}] {ref.slug}: clone failed", file=sys.stderr)
            elif done % 25 == 0:
                print(f"  [{done}/{total}] cloned", file=sys.stderr)
    return failures


# ── rocks: the same replay, over rockcraft.yaml ─────────────────────────────


def rock_dest(repo_url: str, clones: Path) -> Path:
    """Clone directory for one rock's repo.

    Keyed by `owner/repo`, not by `RockRef.slug`: the slug carries the
    rockcraft path, and a monorepo defines dozens of rocks that all live in
    one repo. Cloning per slug would clone kubeflow forty times.
    """
    return clones / _rocks.repo_path(repo_url)


def file_at_commit(dest: Path, sha: str, path: str) -> str | None:
    """Contents of `path` in the tree at `sha`, or None if it isn't there.

    `git show` rather than a checkout, because the rocks replay reads one
    small file per rock off a commit resolved once per repo — where a
    checkout would rewrite the whole working tree for every date.

    Renames are not followed: this addresses a path in a tree, so a
    rockcraft.yaml that has since moved reads as absent at dates before the
    move, and lands in the tally as `NO_ROCK_YET`.
    """
    return _git_raw(["show", f"{sha}:{path.lstrip('/')}"], dest)


def group_rocks(refs: Iterable[RockRef]) -> dict[tuple[str, str], list[RockRef]]:
    """Group rocks by the (repo, branch) whose history has to be walked once.

    The branch is part of the key because two rows in `rocks.csv` may name
    the same repo at different branches, and `commit_asof` answers per ref.
    """
    grouped: dict[tuple[str, str], list[RockRef]] = {}
    for ref in refs:
        grouped.setdefault((ref.repo_url, ref.branch), []).append(ref)
    return grouped


@dataclass
class RocksOutcome:
    """Every rock's reading for one date, plus why the absent ones are absent."""

    records: dict[str, dict] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)
    tally: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(
            (SCANNED, NOT_YET_CREATED, NO_ROCK_YET, UNREADABLE), 0
        )
    )

    def note(self, ref: RockRef, kind: str, reason: str) -> None:
        """Record a rock that produced no reading for this date."""
        self.skipped[ref.slug] = reason
        self.tally[kind] += 1


def rocks_for_date(
    date: dt.date, grouped: dict[tuple[str, str], list[RockRef]], clones: Path
) -> RocksOutcome:
    """Read every rock's `rockcraft.yaml` as it stood on `date`.

    Absence is split the same three ways as on the charm side, because the
    rootless metric divides by the rocks it can see: a rock whose repo did
    not exist yet (`NOT_YET_CREATED`), one whose repo existed without this
    rockcraft.yaml in it (`NO_ROCK_YET`), and one we simply could not read
    (`UNREADABLE`). The first two are left out of `__rocks__` entirely rather
    than recorded as unreadable — `readable: False` means "we looked and
    failed", and a rock that did not exist was never there to look at.
    """
    outcome = RocksOutcome()
    for (repo_url, branch), refs in grouped.items():
        dest = rock_dest(repo_url, clones)
        if not (dest / ".git").exists():
            for ref in refs:
                outcome.note(ref, UNREADABLE, "no local clone (prepare phase failed?)")
            continue
        track = tracking_ref(dest, branch or None)
        if track is None:
            for ref in refs:
                outcome.note(ref, UNREADABLE, "no remote-tracking branch to replay")
            continue
        sha = commit_asof(dest, track, date)
        if sha is None:
            born = first_commit_date(dest, track)
            detail = f"first commit {born}" if born else "no commit history readable"
            for ref in refs:
                outcome.note(
                    ref,
                    NOT_YET_CREATED,
                    f"repo did not exist on {date.isoformat()} ({detail})",
                )
            continue
        for ref in refs:
            text = file_at_commit(dest, sha, ref.path)
            if text is None:
                outcome.note(
                    ref,
                    NO_ROCK_YET,
                    f"repo exists but has no {ref.path} at {date.isoformat()}",
                )
                continue
            facts = _rocks.facts_from_rockcraft(text)
            record = {
                "name": ref.name,
                "repo_url": ref.repo_url,
                "path": ref.path,
                "team": ref.team,
                "readable": facts is not None,
                "run_user": facts["run_user"] if facts is not None else None,
            }
            outcome.records[ref.slug] = record
            outcome.tally[SCANNED if facts is not None else UNREADABLE] += 1
    return outcome


def rocks_block(date: dt.date, outcome: RocksOutcome, source: Path) -> dict:
    """Build the `__rocks__` block, in the shape `scan-rocks --into` writes.

    Same keys, so `trend.Snapshot` reads a replayed block and a scanned one
    identically; the extra `backfill` key rides along as provenance the way
    `__backfill__` does for the charm half.
    """
    readable = sum(1 for r in outcome.records.values() if r.get("readable"))
    return {
        "scanned": len(outcome.records),
        "readable": readable,
        "rocks": outcome.records,
        "backfill": {
            "cutoff": cutoff(date),
            "rocks_source": str(source),
            "corpus_fixed_across_dates": True,
            "outcomes": outcome.tally,
            "skipped": outcome.skipped,
            "tool": "charmtally.tools.backfill --rocks",
        },
    }


def merge_rocks_block(path: Path, block: dict) -> None:
    """Write `block` into an existing snapshot as `__rocks__`, leaving the rest alone.

    A merge, not a rewrite: the charm half of these snapshots is expensive to
    recompute and already correct, and the rocks half is the only thing this
    mode has an opinion about.
    """
    snap: dict = json.loads(path.read_text())
    snap[_trend.ROCKS_KEY] = block
    provenance = snap.get("__backfill__")
    if isinstance(provenance, dict):
        provenance["rocks"] = "backfilled (see __rocks__.backfill)"
    path.write_text(json.dumps(snap, indent=2) + "\n")


def prepare_rock_clones(repo_urls: list[str], clones: Path, *, jobs: int = DEFAULT_JOBS) -> int:
    """Full-clone (or refresh) every rock repo. Returns the number that failed.

    The charm prepare's sibling, over a different key space and a different
    subdirectory of the same workdir. A repo that is both a charm and a rock
    repo is cloned twice: the charm replay checks its clone out at each date,
    and sharing one tree would leave the two halves fighting over it.
    """
    clones.mkdir(parents=True, exist_ok=True)
    total = len(repo_urls)
    done = 0

    def one(repo_url: str) -> bool:
        return ensure_full_clone(repo_url, rock_dest(repo_url, clones)) is not None

    failures = 0
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        for repo_url, ok in zip(repo_urls, pool.map(one, repo_urls), strict=True):
            done += 1
            if not ok:
                failures += 1
                print(f"  [{done}/{total}] {repo_url}: clone failed", file=sys.stderr)
            elif done % 25 == 0:
                print(f"  [{done}/{total}] cloned", file=sys.stderr)
    return failures


def has_rocks_block(path: Path) -> bool:
    """Whether this snapshot already carries a `__rocks__` block."""
    try:
        snap = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    return isinstance(snap.get(_trend.ROCKS_KEY), dict)


def run_rocks(args: argparse.Namespace, dates: list[dt.date]) -> int:
    """`--rocks` mode: fill the `__rocks__` block of snapshots that lack one.

    Only dates that *already have* a snapshot are considered — the inverse of
    the charm mode's filter, and for the same reason. The rocks half lives
    inside the charm snapshot, so there is nothing to merge into for a date
    the charm backfill has not written, and writing a rocks-only file would
    put a snapshot with an empty corpus into the series.
    """
    try:
        refs = _rocks.load_csv(args.rocks_csv)
    except OSError as exc:
        print(f"could not read {args.rocks_csv}: {exc}", file=sys.stderr)
        return 1
    if args.limit is not None:
        refs = refs[: args.limit]
    if not refs:
        print(f"no scannable rows in {args.rocks_csv}", file=sys.stderr)
        return 1

    planned: list[tuple[dt.date, Path]] = []
    for date in dates:
        path = args.snapshots_dir / f"scored-{date.isoformat()}.json"
        if not path.is_file():
            print(f"  {date}: no snapshot to merge into, skipping", file=sys.stderr)
            continue
        if not args.force and has_rocks_block(path):
            continue
        planned.append((date, path))
    if not planned:
        print("nothing to do", file=sys.stderr)
        return 0

    grouped = group_rocks(refs)
    repo_urls = sorted({repo_url for repo_url, _ in grouped})
    print(
        f"plan: {len(planned)} dates ({planned[0][0]} → {planned[-1][0]}), "
        f"{len(refs)} rocks across {len(repo_urls)} repos, "
        f"rocks list {args.rocks_csv} (fixed across dates)",
        file=sys.stderr,
    )
    if args.dry_run:
        for date, _ in planned:
            print(f"  {date}", file=sys.stderr)
        return 0

    clones = args.workdir / "rocks"
    if not args.skip_prepare:
        print(f"… cloning {len(repo_urls)} rock repos with full history", file=sys.stderr)
        failures = prepare_rock_clones(repo_urls, clones, jobs=args.jobs)
        print(f"prepare done ({failures} failed)", file=sys.stderr)

    for date, path in planned:
        print(f"… replaying rocks for {date}", file=sys.stderr)
        outcome = rocks_for_date(date, grouped, clones)
        merge_rocks_block(path, rocks_block(date, outcome, args.rocks_csv))
        tally = outcome.tally
        print(
            f"wrote __rocks__ into {path} ({tally[SCANNED]} read; not yet created: "
            f"{tally[NOT_YET_CREATED]}, no rock yet: {tally[NO_ROCK_YET]}, "
            f"unreadable: {tally[UNREADABLE]})",
            file=sys.stderr,
        )
    return 0


# ── CLI ─────────────────────────────────────────────────────────────────────


def _date(text: str) -> dt.date:
    return dt.date.fromisoformat(text)


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    p = argparse.ArgumentParser(
        prog="python -m charmtally.tools.backfill",
        description="Recompute weekly snapshots for dates that were never scanned.",
    )
    p.add_argument("--start", type=_date, required=True, help="First date to consider (ISO).")
    p.add_argument(
        "--end",
        type=_date,
        default=None,
        help="Last date to consider (ISO). Default: today, so every missing "
        "weekday in the range is filled, not just the ones before the series "
        "starts. Dates that already have a snapshot are skipped unless --force.",
    )
    p.add_argument(
        "--weekday",
        type=int,
        default=DEFAULT_WEEKDAY,
        choices=range(7),
        metavar="0-6",
        help="Weekday to land snapshots on, Monday=0 (default: 0, matching the cron).",
    )
    p.add_argument(
        "--workdir",
        type=Path,
        required=True,
        help="Clone tree for the backfill. Needs full history, so keep it separate "
        "from the weekly scan's shallow workdir; expect several GiB.",
    )
    p.add_argument(
        "--snapshots-dir",
        type=Path,
        default=Path("snapshots"),
        help="Where to write scored-<date>.json (default: snapshots).",
    )
    p.add_argument(
        "--overrides",
        type=Path,
        default=None,
        help="Path to corpus-overrides.yaml. Recommended: ./corpus-overrides.yaml.",
    )
    p.add_argument(
        "--features",
        type=Path,
        default=catalogue.default_path(),
        help="Feature catalogue (default: the one inside the package).",
    )
    p.add_argument(
        "--corpus",
        type=Path,
        default=None,
        help="Corpus CSV to replay every date against. Default: fetch --corpus-url "
        "once and cache it in the workdir. The list is fixed across dates either "
        "way — only the readings move in time.",
    )
    p.add_argument(
        "--corpus-url",
        default=corpus.HYRUM_CHARMS_CSV_URL,
        help="URL of the corpus CSV (default: canonical/hyrum charm-list).",
    )
    p.add_argument(
        "--rocks",
        action="store_true",
        help="Backfill the rocks half instead of the charm half: replay each "
        "rock's rockcraft.yaml at every date and merge the result into the "
        "existing snapshot's __rocks__ block. Only dates that already have a "
        "snapshot are touched.",
    )
    p.add_argument(
        "--rocks-csv",
        type=Path,
        default=DEFAULT_ROCKS_CSV,
        help=f"Rocks list to replay every date against (default: {DEFAULT_ROCKS_CSV}). "
        "Fixed across dates, like the charm corpus.",
    )
    p.add_argument("--jobs", type=int, default=DEFAULT_JOBS, help="Parallel clones in prepare.")
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only the first N corpus repos (or rocks, with --rocks) — for a "
        "smoke run, not a real backfill.",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite snapshots that already exist (default: skip them).",
    )
    p.add_argument(
        "--skip-prepare",
        action="store_true",
        help="Assume --workdir is already fully cloned; go straight to replay.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan (dates, corpus list, repo count) and stop.",
    )
    return p


def _snapshot_date(path: Path) -> dt.date | None:
    try:
        return dt.date.fromisoformat(path.stem.removeprefix("scored-"))
    except ValueError:
        return None


def _existing_snapshot_dates(snapshots_dir: Path) -> list[dt.date]:
    found = (_snapshot_date(p) for p in snapshots_dir.glob("scored-*.json"))
    return sorted(d for d in found if d is not None)


def main(argv: list[str] | None = None) -> int:
    """Plan the range, prepare the clones, then replay each date."""
    args = build_parser().parse_args(argv)

    existing = _existing_snapshot_dates(args.snapshots_dir)
    end = args.end
    if end is None:
        end = dt.date.today()
        print(f"end not given; considering every date up to {end}", file=sys.stderr)

    dates = weekly_dates(args.start, end, args.weekday)
    if args.rocks:
        # The rocks half merges into snapshots that already exist, so it plans
        # its own range rather than sharing the "skip dates already written"
        # filter below — which would skip exactly the dates it needs.
        args.workdir.mkdir(parents=True, exist_ok=True)
        return run_rocks(args, dates)
    if not args.force:
        dates = [d for d in dates if d not in set(existing)]
    if not dates:
        print("nothing to do", file=sys.stderr)
        return 0

    args.workdir.mkdir(parents=True, exist_ok=True)
    try:
        source = resolve_corpus(args.corpus, args.corpus_url, args.workdir)
    except OSError as exc:
        print(f"could not read the corpus list: {exc}", file=sys.stderr)
        return 1
    refs = unique_refs(corpus.load(source.path))
    if args.limit is not None:
        refs = refs[: args.limit]
    if not refs:
        print(f"no charms in {source.path}", file=sys.stderr)
        return 1

    print(
        f"plan: {len(dates)} dates ({dates[0]} → {dates[-1]}), {len(refs)} repos, "
        f"corpus {source.origin} (fixed across dates)",
        file=sys.stderr,
    )
    if args.dry_run:
        for date in dates:
            print(f"  {date}", file=sys.stderr)
        return 0

    if not args.skip_prepare:
        print(f"… cloning {len(refs)} repos with full history", file=sys.stderr)
        failures = prepare_clones(refs, args.workdir, jobs=args.jobs)
        print(f"prepare done ({failures} failed)", file=sys.stderr)

    feats = catalogue.load(args.features)
    pats = catalogue.load_patterns(args.features)
    overrides = (
        corpus.load_overrides(args.overrides) if args.overrides else corpus.CorpusOverrides.empty()
    )

    args.snapshots_dir.mkdir(parents=True, exist_ok=True)
    for date in dates:
        print(f"… replaying {date}", file=sys.stderr)
        snap = snapshot_for_date(
            date, refs, args.workdir, feats, pats, overrides, args.features, source
        )
        out = args.snapshots_dir / f"scored-{date.isoformat()}.json"
        out.write_text(json.dumps(snap, indent=2) + "\n")
        records = sum(1 for k in snap if not k.startswith("__"))
        tally = snap["__backfill__"]["outcomes"]  # type: ignore[index]
        print(
            f"wrote {out} ({records} records; not yet created: "
            f"{tally[NOT_YET_CREATED]}, no charm yet: {tally[NO_CHARM_YET]}, "
            f"unreadable: {tally[UNREADABLE]})",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
