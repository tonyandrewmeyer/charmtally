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


class TestScanRepoAsOf:
    def test_charm_absent_at_the_date_is_skipped_not_scanned(self, tmp_path: Path):
        origin = _repo(tmp_path / "origin")
        clone = backfill.ensure_full_clone(str(origin), tmp_path / "clone", branch="main")
        assert clone is not None
        ref = _ref("demo", str(origin), branch="main")
        overrides = backfill.corpus.CorpusOverrides.empty()
        feats = backfill.catalogue.load(backfill.catalogue.default_path())
        pats = backfill.catalogue.load_patterns(backfill.catalogue.default_path())

        # February: the repo exists but holds no charm files.
        records, skipped = backfill.scan_repo_asof(
            ref, clone, dt.date(2026, 3, 1), feats, pats, overrides
        )
        assert not records
        assert "no charmcraft.yaml" in next(iter(skipped.values()))

        # May: the charm is there, and scans like any other.
        records, skipped = backfill.scan_repo_asof(
            ref, clone, dt.date(2026, 5, 1), feats, pats, overrides
        )
        assert not skipped
        (record,) = records.values()
        assert record["repo_url"] == str(origin)
        assert "__meta__" in record["features"]

    def test_no_commit_before_the_date_is_reported_as_such(self, tmp_path: Path):
        origin = _repo(tmp_path / "origin")
        clone = backfill.ensure_full_clone(str(origin), tmp_path / "clone", branch="main")
        assert clone is not None
        records, skipped = backfill.scan_repo_asof(
            _ref("demo", str(origin), branch="main"),
            clone,
            dt.date(2026, 1, 1),
            [],
            [],
            backfill.corpus.CorpusOverrides.empty(),
        )
        assert not records
        assert "no commit before 2026-01-01" in next(iter(skipped.values()))


class TestSnapshotShape:
    def test_provenance_block_is_written_and_ignored_by_consumers(self, tmp_path: Path):
        origin = _repo(tmp_path / "origin")
        workdir = tmp_path / "work"
        assert backfill.ensure_full_clone(
            str(origin), workdir / "charms" / "origin", branch="main"
        )
        csv_path = tmp_path / "corpus.csv"
        csv_path.write_text(
            f"Team,Charm Name,Repository,Branch (if not the default)\ndemo,demo,{origin},main\n"
        )
        features = backfill.catalogue.default_path()
        snap = backfill.snapshot_for_date(
            dt.date(2026, 5, 1),
            backfill.CorpusAtDate(csv_path, "pinned", None),
            workdir,
            backfill.catalogue.load(features),
            backfill.catalogue.load_patterns(features),
            backfill.corpus.CorpusOverrides.empty(),
            features,
        )
        assert "__backfill__" in snap
        assert snap["__backfill__"]["cutoff"] == "2026-05-01 02:00:00 +0000"
        assert trend.ROCKS_KEY not in snap
        # One real charm record, and it survives a JSON round-trip.
        charms = {k: v for k, v in snap.items() if not k.startswith("__")}
        assert len(charms) == 1
        assert json.loads(json.dumps(snap))


def _ref(name: str, url: str, *, branch: str | None = None):
    return backfill.corpus.CharmRef(
        team="t", name=name, repo_url=url, key_charm=False, branch=branch, notes=""
    )
