"""Tests for charmtally.trend: snapshot loading + adoption/timeline/diff
computation, including the corpus-drift / feature-drift / rename guards."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from .. import cli, trend
from ..dashboard import render_trend


def _charm(*, present: dict[str, bool], repo_url: str = "https://x/c") -> dict:
    features = {
        fname: {"present": is_present, "evidence": [], "score": "clear-gap"} for fname, is_present in present.items()
    }
    return {"name": "c", "team": "t", "repo_url": repo_url, "features": features}


def _write_snapshot(dir_: Path, date: str, charms: dict) -> Path:
    path = dir_ / f"scored-{date}.json"
    path.write_text(json.dumps(charms))
    return path


# --- loader -----------------------------------------------------------------


def test_load_snapshots_globs_and_sorts_by_date(tmp_path: Path) -> None:
    _write_snapshot(tmp_path, "2026-06-22", {"a": _charm(present={"f": True})})
    _write_snapshot(tmp_path, "2026-06-11", {"a": _charm(present={"f": False})})
    _write_snapshot(tmp_path, "2026-06-15", {"a": _charm(present={"f": False})})

    snapshots = trend.load_snapshots(tmp_path)

    assert [s.date for s in snapshots] == ["2026-06-11", "2026-06-15", "2026-06-22"]


def test_load_snapshots_ignores_non_matching_files(tmp_path: Path) -> None:
    _write_snapshot(tmp_path, "2026-06-11", {"a": _charm(present={"f": True})})
    (tmp_path / "README.md").write_text("not a snapshot")
    (tmp_path / "results.json").write_text("{}")

    snapshots = trend.load_snapshots(tmp_path)

    assert len(snapshots) == 1


def test_load_snapshots_merges_live_as_latest_date(tmp_path: Path) -> None:
    _write_snapshot(tmp_path, "2026-06-11", {"a": _charm(present={"f": False})})
    live = tmp_path / "scored.json"
    live.write_text(json.dumps({"a": _charm(present={"f": True})}))

    snapshots = trend.load_snapshots(tmp_path, live, today=dt.date(2026, 6, 20))

    assert [s.date for s in snapshots] == ["2026-06-11", "2026-06-20"]
    assert snapshots[-1].charms["a"]["features"]["f"]["present"] is True


def test_load_snapshots_skips_live_when_todays_snapshot_already_exists(tmp_path: Path) -> None:
    _write_snapshot(tmp_path, "2026-06-11", {"a": _charm(present={"f": False})})
    _write_snapshot(tmp_path, "2026-06-20", {"a": _charm(present={"f": True})})
    live = tmp_path / "scored.json"
    live.write_text(json.dumps({"a": _charm(present={"f": False})}))  # stale copy — must be ignored

    snapshots = trend.load_snapshots(tmp_path, live, today=dt.date(2026, 6, 20))

    assert [s.date for s in snapshots] == ["2026-06-11", "2026-06-20"]
    assert snapshots[-1].charms["a"]["features"]["f"]["present"] is True


def test_load_snapshots_skips_missing_live_path(tmp_path: Path) -> None:
    _write_snapshot(tmp_path, "2026-06-11", {"a": _charm(present={"f": False})})

    snapshots = trend.load_snapshots(tmp_path, tmp_path / "does-not-exist.json")

    assert [s.date for s in snapshots] == ["2026-06-11"]


def test_load_snapshots_excludes_skipped_key(tmp_path: Path) -> None:
    _write_snapshot(
        tmp_path,
        "2026-06-11",
        {"a": _charm(present={"f": True}), "__skipped__": {"b": "clone failed"}},
    )

    snapshots = trend.load_snapshots(tmp_path)

    assert list(snapshots[0].charms) == ["a"]


# --- adoption ----------------------------------------------------------------


def test_compute_adoption_percent_per_date() -> None:
    s1 = trend.Snapshot(
        date="2026-06-11",
        charms={
            "a": _charm(present={"f": True}),
            "b": _charm(present={"f": False}),
        },
        feature_names=frozenset({"f"}),
    )
    s2 = trend.Snapshot(
        date="2026-06-22",
        charms={
            "a": _charm(present={"f": True}),
            "b": _charm(present={"f": True}),
        },
        feature_names=frozenset({"f"}),
    )

    series = trend.compute_adoption([s1, s2])

    assert series["f"] == [
        {"date": "2026-06-11", "present": 1, "total": 2, "percent": 50.0},
        {"date": "2026-06-22", "present": 2, "total": 2, "percent": 100.0},
    ]


def test_compute_adoption_skips_dates_before_feature_existed() -> None:
    """Feature-catalogue drift: a feature added later has no data point for
    snapshots taken before it existed, instead of a misleading 0%."""
    s1 = trend.Snapshot(date="2026-06-11", charms={"a": _charm(present={})}, feature_names=frozenset())
    s2 = trend.Snapshot(
        date="2026-06-22",
        charms={"a": _charm(present={"new-feature": True})},
        feature_names=frozenset({"new-feature"}),
    )

    series = trend.compute_adoption([s1, s2])

    assert [p["date"] for p in series["new-feature"]] == ["2026-06-22"]


def test_compute_adoption_feature_filter() -> None:
    s1 = trend.Snapshot(
        date="2026-06-11",
        charms={"a": _charm(present={"f": True, "g": False})},
        feature_names=frozenset({"f", "g"}),
    )

    series = trend.compute_adoption([s1], feature="f")

    assert list(series) == ["f"]


# --- timeline ------------------------------------------------------------


def test_compute_timeline_states() -> None:
    s1 = trend.Snapshot(
        date="2026-06-11",
        charms={"a": _charm(present={"f": False})},
        feature_names=frozenset({"f"}),
    )
    s1.charms["a"]["features"]["f"]["score"] = "clear-gap"
    s2 = trend.Snapshot(
        date="2026-06-22",
        charms={"a": _charm(present={"f": True})},
        feature_names=frozenset({"f"}),
    )

    rows = trend.compute_timeline([s1, s2])

    assert len(rows) == 1
    row = rows[0]
    assert row["charm"] == "a"
    assert row["feature"] == "f"
    assert [c["state"] for c in row["cells"]] == ["clear-gap", "present"]


def test_compute_timeline_marks_corpus_drift() -> None:
    """A charm absent from an older snapshot is 'not-in-corpus', not a gap."""
    s1 = trend.Snapshot(date="2026-06-11", charms={}, feature_names=frozenset({"f"}))
    s2 = trend.Snapshot(
        date="2026-06-22",
        charms={"a": _charm(present={"f": True})},
        feature_names=frozenset({"f"}),
    )

    rows = trend.compute_timeline([s1, s2])

    assert len(rows) == 1
    states = [c["state"] for c in rows[0]["cells"]]
    assert states == ["not-in-corpus", "present"]


def test_compute_timeline_marks_feature_drift() -> None:
    """A feature not yet in the catalogue at an earlier date is 'not-scanned'."""
    s1 = trend.Snapshot(date="2026-06-11", charms={"a": _charm(present={})}, feature_names=frozenset())
    s2 = trend.Snapshot(
        date="2026-06-22",
        charms={"a": _charm(present={"new-feature": True})},
        feature_names=frozenset({"new-feature"}),
    )

    rows = trend.compute_timeline([s1, s2])

    assert len(rows) == 1
    states = [c["state"] for c in rows[0]["cells"]]
    assert states == ["not-scanned", "present"]


def test_compute_timeline_omits_rows_entirely_not_in_corpus_or_scanned() -> None:
    """A (charm, feature) pair that's never actually observable across the
    whole range shouldn't produce a row of nothing but placeholders."""
    s1 = trend.Snapshot(date="2026-06-11", charms={}, feature_names=frozenset())
    s2 = trend.Snapshot(date="2026-06-22", charms={}, feature_names=frozenset({"f"}))

    rows = trend.compute_timeline([s1, s2])

    assert rows == []


# --- diff ------------------------------------------------------------------


def test_compute_diff_detects_regression_and_adoption() -> None:
    base = trend.Snapshot(
        date="2026-06-11",
        charms={
            "a": _charm(present={"f": True}),
            "b": _charm(present={"f": False}),
        },
        feature_names=frozenset({"f"}),
    )
    latest = trend.Snapshot(
        date="2026-06-22",
        charms={
            "a": _charm(present={"f": False}),
            "b": _charm(present={"f": True}),
        },
        feature_names=frozenset({"f"}),
    )

    diff = trend.compute_diff(base, latest)

    assert diff["base_date"] == "2026-06-11"
    assert diff["latest_date"] == "2026-06-22"
    flips = {(f["charm"], f["kind"]) for f in diff["flips"]}
    assert flips == {("a", "regression"), ("b", "adoption")}


def test_compute_diff_skips_charms_absent_from_either_snapshot() -> None:
    """Corpus drift: a charm only in one snapshot produces no flip."""
    base = trend.Snapshot(
        date="2026-06-11",
        charms={"a": _charm(present={"f": True})},
        feature_names=frozenset({"f"}),
    )
    latest = trend.Snapshot(
        date="2026-06-22",
        charms={"b": _charm(present={"f": True})},
        feature_names=frozenset({"f"}),
    )

    diff = trend.compute_diff(base, latest)

    assert diff["flips"] == []


def test_compute_diff_skips_features_not_scanned_in_both() -> None:
    """Feature-catalogue drift: a feature only scanned in one snapshot
    produces no flip, even if the raw present/absent values differ."""
    base = trend.Snapshot(
        date="2026-06-11",
        charms={"a": _charm(present={})},
        feature_names=frozenset(),
    )
    latest = trend.Snapshot(
        date="2026-06-22",
        charms={"a": _charm(present={"new-feature": True})},
        feature_names=frozenset({"new-feature"}),
    )

    diff = trend.compute_diff(base, latest)

    assert diff["flips"] == []


def test_compute_diff_detects_possible_rename() -> None:
    base = trend.Snapshot(
        date="2026-06-11",
        charms={"old-slug": _charm(present={"f": True}, repo_url="https://x/repo")},
        feature_names=frozenset({"f"}),
    )
    latest = trend.Snapshot(
        date="2026-06-22",
        charms={"new-slug": _charm(present={"f": True}, repo_url="https://x/repo")},
        feature_names=frozenset({"f"}),
    )

    diff = trend.compute_diff(base, latest)

    assert diff["possible_renames"] == [{"old_slug": "old-slug", "new_slug": "new-slug", "repo_url": "https://x/repo"}]
    assert diff["flips"] == []


def test_compute_diff_does_not_guess_ambiguous_renames() -> None:
    """Two disappeared slugs sharing a repo_url with two appeared slugs is
    ambiguous — no rename should be reported rather than a wrong guess."""
    base = trend.Snapshot(
        date="2026-06-11",
        charms={
            "old-a": _charm(present={"f": True}, repo_url="https://x/repo"),
            "old-b": _charm(present={"f": True}, repo_url="https://x/repo"),
        },
        feature_names=frozenset({"f"}),
    )
    latest = trend.Snapshot(
        date="2026-06-22",
        charms={
            "new-a": _charm(present={"f": True}, repo_url="https://x/repo"),
            "new-b": _charm(present={"f": True}, repo_url="https://x/repo"),
        },
        feature_names=frozenset({"f"}),
    )

    diff = trend.compute_diff(base, latest)

    assert diff["possible_renames"] == []


# --- select_base / select_range ------------------------------------------


def test_select_base_defaults_to_earliest() -> None:
    snapshots = [
        trend.Snapshot(date=d, charms={}, feature_names=frozenset()) for d in ["2026-06-11", "2026-06-15", "2026-06-22"]
    ]
    assert trend.select_base(snapshots, None).date == "2026-06-11"


def test_select_base_honours_since() -> None:
    snapshots = [
        trend.Snapshot(date=d, charms={}, feature_names=frozenset()) for d in ["2026-06-11", "2026-06-15", "2026-06-22"]
    ]
    assert trend.select_base(snapshots, "2026-06-14").date == "2026-06-15"


def test_select_base_returns_none_when_nothing_matches() -> None:
    snapshots = [trend.Snapshot(date="2026-06-11", charms={}, feature_names=frozenset())]
    assert trend.select_base(snapshots, "2099-01-01") is None


# --- integration against the real 6 snapshots -----------------------------


def test_against_real_snapshots() -> None:
    snapshot_dir = Path(__file__).resolve().parent.parent.parent / "snapshots"
    snapshots = trend.load_snapshots(snapshot_dir)

    assert [s.date for s in snapshots] == [
        "2026-06-11",
        "2026-06-15",
        "2026-06-22",
        "2026-06-25",
        "2026-06-29",
        "2026-07-06",
        "2026-07-13",
    ]

    base, latest = snapshots[0], snapshots[-1]
    diff = trend.compute_diff(base, latest)
    assert diff["base_date"] == "2026-06-11"
    assert diff["latest_date"] == "2026-07-13"
    # Every flip must be between charms present in both ends of the range —
    # the corpus grew from ~344 to 761 charms over this window, so most
    # charms are correctly excluded by the corpus-drift guard.
    common_slugs = set(base.charms) & set(latest.charms)
    for f in diff["flips"]:
        assert f["charm"] in common_slugs

    adoption = trend.compute_adoption(snapshots)
    assert set(adoption) == base.feature_names | latest.feature_names
    # Features present in the live catalogue but not in the earliest
    # snapshot must not get a fabricated 0% data point for that date.
    newer_only = latest.feature_names - base.feature_names
    for fname in newer_only:
        dates = {p["date"] for p in adoption[fname]}
        assert base.date not in dates

    timeline = trend.compute_timeline(snapshots, feature="ops.secrets")
    assert timeline
    for row in timeline:
        assert row["feature"] == "ops.secrets"
        assert len(row["cells"]) == len(snapshots)


# --- dashboard.render_trend -------------------------------------------------


def test_render_trend_produces_history_page() -> None:
    diff = {
        "base_date": "2026-06-11",
        "latest_date": "2026-06-22",
        "flips": [
            {"charm": "a", "feature": "f", "from": True, "to": False, "kind": "regression"},
            {"charm": "b", "feature": "f", "from": False, "to": True, "kind": "adoption"},
        ],
        "possible_renames": [{"old_slug": "old", "new_slug": "new", "repo_url": "https://x/repo"}],
    }
    adoption = {"f": [{"date": "2026-06-11", "present": 1, "total": 2, "percent": 50.0}]}
    timeline = [
        {"charm": "a", "feature": "f", "cells": [{"date": "2026-06-11", "state": "clear-gap"}]},
    ]

    html = render_trend(diff, adoption, timeline)

    assert "charmtally — history" in html
    assert "old" in html and "new" in html  # rename surfaced
    assert "2026-06-11" in html and "2026-06-22" in html


# --- CLI smoke ---------------------------------------------------------------


def test_cli_trend_parses_all_flags_and_writes_output(tmp_path: Path) -> None:
    snapshots_dir = tmp_path / "snapshots"
    snapshots_dir.mkdir()
    _write_snapshot(snapshots_dir, "2026-06-11", {"a": _charm(present={"f": False})})
    _write_snapshot(snapshots_dir, "2026-06-22", {"a": _charm(present={"f": True})})
    out = tmp_path / "trend.html"

    rc = cli.main([
        "trend",
        "--snapshots-dir",
        str(snapshots_dir),
        "--live",
        str(tmp_path / "does-not-exist.json"),
        "--feature",
        "f",
        "--since",
        "2026-06-11",
        "--out",
        str(out),
        "--json",
    ])

    assert rc == 0
    assert out.is_file()
    assert out.with_suffix(".json").is_file()
    payload = json.loads(out.with_suffix(".json").read_text())
    assert payload["diff"]["base_date"] == "2026-06-11"
    assert payload["diff"]["latest_date"] == "2026-06-22"


def test_cli_trend_errors_without_snapshots(tmp_path: Path) -> None:
    empty_dir = tmp_path / "snapshots"
    empty_dir.mkdir()

    rc = cli.main([
        "trend",
        "--snapshots-dir",
        str(empty_dir),
        "--live",
        str(tmp_path / "does-not-exist.json"),
        "--out",
        str(tmp_path / "trend.html"),
    ])

    assert rc == 1
