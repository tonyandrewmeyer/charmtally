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
clobber a snapshot that records a real scan.

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
* **Rocks are omitted.** The `__rocks__` block needs a `rockcraft.yaml` per
  rock as of the date, which the raw-fetch path cannot express, and `rocks.csv`
  is curated now anyway. Backfilled snapshots carry no rocks block, which
  `trend.Snapshot.rocks_scanned` already reports as "not scanned" — so the
  rootless metric shows a short series rather than a fake collapse to root.
* **`corpus-overrides.yaml` is today's.** Exclusions written for a charm's
  current shape are applied to its historical shape.
* **The cutoff reads committer dates.** A branch rebased or squashed since the
  date carries the *rewrite's* dates, so such a charm is replayed at whatever
  its rewritten history says was current — usually a later tree than the one
  that really existed that week.

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
from ..cli import _apply_feature_excludes

if TYPE_CHECKING:
    from collections.abc import Iterable

    from ..corpus import CharmRef

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


def _git_out(args: list[str], cwd: Path | None = None, timeout: int = 300) -> str | None:
    """Run a git command and return its stdout stripped, or None on failure.

    `scan._git` answers only pass/fail, and every call here wants the output
    (a SHA, a ref name) — so this is a sibling of it, not a replacement.
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
    return proc.stdout.strip()


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
        "rocks": "not scanned (backfill cannot time-travel rockcraft.yaml)",
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
        help="Last date to consider (ISO). Default: the day before the earliest "
        "existing snapshot, so a backfill stops where the real series begins.",
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
    p.add_argument("--jobs", type=int, default=DEFAULT_JOBS, help="Parallel clones in prepare.")
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only the first N corpus repos — for a smoke run, not a real backfill.",
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
        if not existing:
            print("no existing snapshots to bound the range; pass --end", file=sys.stderr)
            return 2
        end = existing[0] - dt.timedelta(days=1)
        print(
            f"end not given; stopping at {end} (earliest snapshot: {existing[0]})", file=sys.stderr
        )

    dates = weekly_dates(args.start, end, args.weekday)
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
