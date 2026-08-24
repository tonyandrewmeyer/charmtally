"""Tests for the snapshot backfill tool.

The git-facing half is covered by one real repo (the same choice
`test_scan_clone.py` makes: the behaviour under test *is* the git
interaction), kept to a handful of invocations. Everything else here is pure.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
from typing import TYPE_CHECKING

from .. import trend
from ..tools import backfill

if TYPE_CHECKING:
    from pathlib import Path


def _git(args: list[str], cwd: Path, *, when: str | None = None) -> None:
    env = dict(os.environ)
    if when is not None:
        # `rev-list --before` reads the *committer* date, so both have to be
        # set for a fixture commit to land in the past.
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, env=env)


def _repo(root: Path) -> Path:
    """Build a repo with two commits: a charmless one, then one with a charm."""
    root.mkdir(parents=True)
    _git(["init", "-q", "-b", "main"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "T"], root)
    (root / "README.md").write_text("hello\n")
    _git(["add", "-A"], root)
    _git(
        ["-c", "commit.gpgsign=false", "commit", "-q", "-m", "first"],
        root,
        when="2026-02-01T00:00:00Z",
    )
    (root / "charmcraft.yaml").write_text("name: demo\ntype: charm\n")
    _git(["add", "-A"], root)
    _git(
        ["-c", "commit.gpgsign=false", "commit", "-q", "-m", "charm"],
        root,
        when="2026-04-01T00:00:00Z",
    )
    return root


class TestWeeklyDates:
    def test_starts_on_the_first_matching_weekday(self):
        # 2026-01-01 is a Thursday; the first Monday after it is the 5th.
        dates = backfill.weekly_dates(dt.date(2026, 1, 1), dt.date(2026, 1, 26))
        assert dates == [
            dt.date(2026, 1, 5),
            dt.date(2026, 1, 12),
            dt.date(2026, 1, 19),
            dt.date(2026, 1, 26),
        ]

    def test_end_is_inclusive_and_reversed_range_is_empty(self):
        assert backfill.weekly_dates(dt.date(2026, 1, 5), dt.date(2026, 1, 5)) == [
            dt.date(2026, 1, 5)
        ]
        assert backfill.weekly_dates(dt.date(2026, 2, 1), dt.date(2026, 1, 1)) == []

    def test_weekday_is_selectable(self):
        dates = backfill.weekly_dates(dt.date(2026, 1, 1), dt.date(2026, 1, 15), weekday=3)
        assert dates == [dt.date(2026, 1, 1), dt.date(2026, 1, 8), dt.date(2026, 1, 15)]


class TestCutoff:
    def test_mirrors_the_cron_hour(self):
        assert backfill.cutoff(dt.date(2026, 3, 9)) == "2026-03-09 02:00:00 +0000"


class TestUniqueRefs:
    def test_drops_repeat_repos_keeping_order(self):
        rows = [
            _ref("a", "https://github.com/x/a"),
            _ref("b", "https://github.com/x/b"),
            _ref("a-again", "https://github.com/x/a"),
        ]
        assert [r.name for r in backfill.unique_refs(rows)] == ["a", "b"]


class TestSnapshotDates:
    def test_reads_dates_and_ignores_junk(self, tmp_path: Path):
        (tmp_path / "scored-2026-06-15.json").write_text("{}")
        (tmp_path / "scored-2026-06-08.json").write_text("{}")
        (tmp_path / "scored-nope.json").write_text("{}")
        assert backfill._existing_snapshot_dates(tmp_path) == [
            dt.date(2026, 6, 8),
            dt.date(2026, 6, 15),
        ]


class TestCommitAsOf:
    def test_picks_the_last_commit_before_the_cutoff(self, tmp_path: Path):
        repo = _repo(tmp_path / "origin")
        early = backfill.commit_asof(repo, "main", dt.date(2026, 3, 1))
        late = backfill.commit_asof(repo, "main", dt.date(2026, 5, 1))
        assert early and late and early != late

    def test_none_when_the_repo_did_not_exist_yet(self, tmp_path: Path):
        repo = _repo(tmp_path / "origin")
        assert backfill.commit_asof(repo, "main", dt.date(2026, 1, 1)) is None


class TestFirstCommitDate:
    def test_reports_the_oldest_commit(self, tmp_path: Path):
        repo = _repo(tmp_path / "origin")
        assert backfill.first_commit_date(repo, "main") == "2026-02-01"


class TestScanRepoAsOf:
    def test_charm_absent_at_the_date_is_skipped_not_scanned(self, tmp_path: Path):
        origin = _repo(tmp_path / "origin")
        clone = backfill.ensure_full_clone(str(origin), tmp_path / "clone", branch="main")
        assert clone is not None
        ref = _ref("demo", str(origin), branch="main")
        overrides = backfill.corpus.CorpusOverrides.empty()
        feats = backfill.catalogue.load(backfill.catalogue.default_path())
        pats = backfill.catalogue.load_patterns(backfill.catalogue.default_path())

        # March: the repo exists but holds no charm files yet — a repo older
        # than its charm, which is not the same as a repo that did not exist.
        outcome = backfill.scan_repo_asof(ref, clone, dt.date(2026, 3, 1), feats, pats, overrides)
        assert outcome.kind == backfill.NO_CHARM_YET
        assert not outcome.records
        assert "no charmcraft.yaml" in next(iter(outcome.skipped.values()))

        # May: the charm is there, and scans like any other.
        outcome = backfill.scan_repo_asof(ref, clone, dt.date(2026, 5, 1), feats, pats, overrides)
        assert outcome.kind == backfill.SCANNED
        assert not outcome.skipped
        (record,) = outcome.records.values()
        assert record["repo_url"] == str(origin)
        assert "__meta__" in record["features"]

    def test_a_genuinely_new_repo_is_reported_as_not_yet_created(self, tmp_path: Path):
        origin = _repo(tmp_path / "origin")
        clone = backfill.ensure_full_clone(str(origin), tmp_path / "clone", branch="main")
        assert clone is not None
        outcome = backfill.scan_repo_asof(
            _ref("demo", str(origin), branch="main"),
            clone,
            dt.date(2026, 1, 1),
            [],
            [],
            backfill.corpus.CorpusOverrides.empty(),
        )
        assert outcome.kind == backfill.NOT_YET_CREATED
        assert not outcome.records
        # The reason names the date asked about and how new the repo actually
        # is, so "listed later" and "created later" can be told apart by eye.
        reason = next(iter(outcome.skipped.values()))
        assert "did not exist on 2026-01-01" in reason
        assert "first commit 2026-02-01" in reason


class TestSnapshotShape:
    def _snapshot(self, tmp_path: Path, date: dt.date) -> dict:
        origin = _repo(tmp_path / "origin")
        workdir = tmp_path / "work"
        assert backfill.ensure_full_clone(
            str(origin), workdir / "charms" / "origin", branch="main"
        )
        features = backfill.catalogue.default_path()
        return backfill.snapshot_for_date(
            date,
            [_ref("demo", str(origin), branch="main")],
            workdir,
            backfill.catalogue.load(features),
            backfill.catalogue.load_patterns(features),
            backfill.corpus.CorpusOverrides.empty(),
            features,
            backfill.CorpusSource(tmp_path / "corpus.csv", "local"),
        )

    def test_provenance_block_is_written_and_ignored_by_consumers(self, tmp_path: Path):
        snap = self._snapshot(tmp_path, dt.date(2026, 5, 1))
        assert snap["__backfill__"]["cutoff"] == "2026-05-01 02:00:00 +0000"
        assert snap["__backfill__"]["corpus_fixed_across_dates"] is True
        assert snap["__backfill__"]["outcomes"][backfill.SCANNED] == 1
        # Rocks do not time-travel, so the block is absent rather than empty:
        # `trend` reads that as "not scanned", not as "no rocks are rootless".
        assert trend.ROCKS_KEY not in snap
        charms = {k: v for k, v in snap.items() if not k.startswith("__")}
        assert len(charms) == 1
        assert json.loads(json.dumps(snap))

    def test_a_charm_that_did_not_exist_yet_is_in_no_count(self, tmp_path: Path):
        snap = self._snapshot(tmp_path, dt.date(2026, 1, 1))
        assert not {k for k in snap if not k.startswith("__")}
        assert snap["__backfill__"]["outcomes"][backfill.NOT_YET_CREATED] == 1
        # `trend` counts charms, and this one is only in __skipped__ — so it
        # cannot land in a denominator as a charm that failed to adopt.
        snapshot = trend._load_one(_written(tmp_path, snap), "2026-01-01")
        assert snapshot.charms == {}


def _written(tmp_path: Path, snap: dict) -> Path:
    path = tmp_path / "scored-x.json"
    path.write_text(json.dumps(snap, indent=2) + "\n")
    return path


def _ref(name: str, url: str, *, branch: str | None = None):
    return backfill.corpus.CharmRef(
        team="t", name=name, repo_url=url, key_charm=False, branch=branch, notes=""
    )


class TestPlannedRange:
    """`main`'s date planning, via `--dry-run` (no clones, no network)."""

    def _plan(self, tmp_path: Path, capsys, existing: list[str], *extra: str) -> list[str]:
        snaps = tmp_path / "snapshots"
        snaps.mkdir()
        for date in existing:
            (snaps / f"scored-{date}.json").write_text("{}\n")
        csv = tmp_path / "corpus.csv"
        csv.write_text("Team,Charm Name,Repository\nx,foo,https://github.com/canonical/foo\n")
        rc = backfill.main([
            "--start",
            "2026-01-01",
            "--workdir",
            str(tmp_path / "work"),
            "--snapshots-dir",
            str(snaps),
            "--corpus",
            str(csv),
            "--dry-run",
            *extra,
        ])
        assert rc == 0
        return [line.strip() for line in capsys.readouterr().err.splitlines()]

    def test_gaps_after_the_series_starts_are_planned(self, tmp_path: Path, capsys):
        # The old default stopped the day before the earliest snapshot, so a
        # missing week *inside* the series could never be filled.
        lines = self._plan(tmp_path, capsys, ["2026-01-05", "2026-01-19"])
        assert "2026-01-12" in lines

    def test_dates_that_already_have_a_snapshot_are_left_alone(self, tmp_path: Path, capsys):
        lines = self._plan(tmp_path, capsys, ["2026-01-05", "2026-01-19"])
        assert "2026-01-05" not in lines
        assert "2026-01-19" not in lines

    def test_force_replays_dates_that_already_exist(self, tmp_path: Path, capsys):
        lines = self._plan(tmp_path, capsys, ["2026-01-05", "2026-01-19"], "--force")
        assert "2026-01-05" in lines
        assert "2026-01-19" in lines

    def test_an_empty_snapshots_dir_no_longer_needs_an_explicit_end(self, tmp_path: Path, capsys):
        lines = self._plan(tmp_path, capsys, [])
        assert "2026-01-05" in lines
