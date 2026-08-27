"""Tests for charmtally.snapshot: thinning a scored file down to the readings,
and the guarantee that thin and fat snapshots coexist in one directory."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from .. import adoption, cli, snapshot, trend

if TYPE_CHECKING:
    from pathlib import Path


def _fat_charm(
    *,
    present: dict[str, bool],
    repo_url: str = "https://x/c",
    meta: dict | None = None,
) -> dict:
    """A charm record in the shape `charmtally score` writes it."""
    features: dict[str, dict] = {
        fname: {
            "present": is_present,
            "evidence": [{"path": "src/charm.py", "line": 12, "snippet": "x" * 200}],
            "score": "clear-gap",
            "rationale": "a sentence of user-facing prose about the gap",
        }
        for fname, is_present in present.items()
    }
    features["__meta__"] = meta if meta is not None else {"is_reactive": False}
    return {"name": "c", "team": "t", "repo_url": repo_url, "features": features}


# --- what thinning keeps and drops ------------------------------------------


def test_thin_drops_evidence_and_rationale_keeps_the_readings() -> None:
    thinned = snapshot.thin({"a": _fat_charm(present={"f": True})})

    rec = thinned["a"]["features"]["f"]
    assert rec == {"present": True, "score": "clear-gap"}


def test_thin_keeps_the_charm_header() -> None:
    thinned = snapshot.thin({"a": _fat_charm(present={"f": True}, repo_url="https://x/y")})

    assert thinned["a"]["name"] == "c"
    assert thinned["a"]["team"] == "t"
    assert thinned["a"]["repo_url"] == "https://x/y"


def test_thin_keeps_meta_whole() -> None:
    meta = {
        "is_reactive": False,
        "charmlibs_count": 3,
        "library_names": ["charms.foo.v0.bar"],
        "architecture": ["component-graph"],
    }
    thinned = snapshot.thin({"a": _fat_charm(present={"f": True}, meta=meta)})

    assert thinned["a"]["features"]["__meta__"] == meta


def test_thin_preserves_key_absence_rather_than_defaulting() -> None:
    """A present feature with no note has no `score`; it must not gain one.

    `cmd_score` pops `score` off such a record on purpose, and
    `adoption._has_charmlibs_data` reads key presence as "this scan looked".
    Filling in a default here would turn "not measured" into a measurement.
    """
    fat = {"a": {"features": {"f": {"present": True, "evidence": []}, "__meta__": {}}}}

    rec = snapshot.thin(fat)["a"]["features"]["f"]

    assert rec == {"present": True}
    assert "score" not in rec


def test_thin_passes_top_level_blocks_through_untouched() -> None:
    fat = {
        "a": _fat_charm(present={"f": True}),
        "__skipped__": {"b": "excluded by corpus-overrides.yaml"},
        "__backfill__": {"cutoff": "2026-06-01T02:00:00Z"},
        "__rocks__": {"scanned": 1, "readable": 1, "rocks": {"r": {"readable": True}}},
    }

    thinned = snapshot.thin(fat)

    assert thinned["__skipped__"] == fat["__skipped__"]
    assert thinned["__backfill__"] == fat["__backfill__"]
    assert thinned["__rocks__"] == fat["__rocks__"]


def test_thin_marks_the_snapshot() -> None:
    thinned = snapshot.thin({"a": _fat_charm(present={"f": True})})

    assert thinned[snapshot.THIN_KEY]["dropped"] == ["evidence", "rationale"]


def test_thin_does_not_mutate_its_input() -> None:
    fat = {"a": _fat_charm(present={"f": True})}

    snapshot.thin(fat)

    assert fat["a"]["features"]["f"]["evidence"]
    assert "rationale" in fat["a"]["features"]["f"]


# --- thin and fat snapshots in one directory --------------------------------


def _mixed_dir(tmp_path: Path) -> Path:
    """One fat snapshot and one thin one, a week apart.

    The eight-plus already-committed fat snapshots are the entire history and
    cannot be regenerated, so the loader has to keep reading them alongside
    everything written from now on.
    """
    charms = {
        "a": _fat_charm(present={"f": False, "g": True}),
        "b": _fat_charm(present={"f": True, "g": True}, repo_url="https://x/b"),
    }
    (tmp_path / "scored-2026-06-01.json").write_text(json.dumps(charms))
    later = {
        "a": _fat_charm(present={"f": True, "g": True}),
        "b": _fat_charm(present={"f": True, "g": False}, repo_url="https://x/b"),
    }
    (tmp_path / "scored-2026-06-08.json").write_text(json.dumps(snapshot.thin(later)))
    return tmp_path


def test_loader_reads_a_mixed_directory(tmp_path: Path) -> None:
    snapshots = trend.load_snapshots(_mixed_dir(tmp_path))

    assert [s.date for s in snapshots] == ["2026-06-01", "2026-06-08"]
    assert all(s.feature_names == frozenset({"f", "g"}) for s in snapshots)


def test_diff_across_a_fat_base_and_a_thin_latest(tmp_path: Path) -> None:
    base, latest = trend.load_snapshots(_mixed_dir(tmp_path))

    diff = trend.compute_diff(base, latest)

    assert diff["flips"] == [
        {"charm": "a", "feature": "f", "from": False, "to": True, "kind": "adoption"},
        {"charm": "b", "feature": "g", "from": True, "to": False, "kind": "regression"},
    ]


def test_timeline_reads_cell_state_out_of_a_thin_snapshot(tmp_path: Path) -> None:
    snapshots = trend.load_snapshots(_mixed_dir(tmp_path))

    rows = {
        (r["charm"], r["feature"]): [c["state"] for c in r["cells"]]
        for r in trend.compute_timeline(snapshots)
    }

    assert rows["a", "f"] == ["clear-gap", "present"]
    assert rows["b", "g"] == ["present", "clear-gap"]


# --- thinning changes no number anything downstream reports -----------------


def _snapshot_pair(tmp_path: Path, charms: dict) -> tuple[Path, Path]:
    fat_dir = tmp_path / "fat"
    thin_dir = tmp_path / "thin"
    fat_dir.mkdir()
    thin_dir.mkdir()
    (fat_dir / "scored-2026-06-01.json").write_text(json.dumps(charms))
    (thin_dir / "scored-2026-06-01.json").write_text(json.dumps(snapshot.thin(charms)))
    return fat_dir, thin_dir


def test_every_trend_and_adoption_number_survives_thinning(tmp_path: Path) -> None:
    """The point of the exercise: dropping those fields is not a data change."""
    charms = {
        "a": _fat_charm(
            present={"ops.typed-relation": True, "jubilant.integration-tests": True},
            meta={
                "is_reactive": False,
                "is_legacy_classic": False,
                "has_containers": True,
                "charm_user": "non-root",
                "charmlibs_count": 2,
                "library_count": 2,
                "has_integration_tests": True,
            },
        ),
        "b": _fat_charm(
            present={"ops.typed-relation": False, "jubilant.integration-tests": False},
            repo_url="https://x/b",
            meta={
                "is_reactive": False,
                "is_legacy_classic": False,
                "has_containers": True,
                "charm_user": None,
                "charmlibs_count": 0,
                "library_count": 4,
                "has_integration_tests": False,
            },
        ),
        "__rocks__": {
            "scanned": 2,
            "readable": 2,
            "rocks": {
                "r1": {"readable": True, "run_user": "_daemon_"},
                "r2": {"readable": True, "run_user": None},
            },
        },
    }
    fat_dir, thin_dir = _snapshot_pair(tmp_path, charms)

    fat = trend.load_snapshots(fat_dir)
    thin = trend.load_snapshots(thin_dir)

    assert trend.compute_adoption(thin) == trend.compute_adoption(fat)
    assert trend.compute_timeline(thin) == trend.compute_timeline(fat)
    assert trend.encode_timeline(trend.compute_timeline(thin)) == trend.encode_timeline(
        trend.compute_timeline(fat)
    )
    assert adoption.compute_series(thin) == adoption.compute_series(fat)


# --- the CLI ----------------------------------------------------------------


def test_cmd_snapshot_writes_a_dated_thin_file(tmp_path: Path) -> None:
    scored = tmp_path / "scored.json"
    scored.write_text(json.dumps({"a": _fat_charm(present={"f": True})}))
    out_dir = tmp_path / "snapshots"

    rc = cli.main([
        "snapshot",
        str(scored),
        "--snapshots-dir",
        str(out_dir),
        "--date",
        "2026-06-15",
    ])

    assert rc == 0
    written = json.loads((out_dir / "scored-2026-06-15.json").read_text())
    assert written["a"]["features"]["f"] == {"present": True, "score": "clear-gap"}
    assert snapshot.THIN_KEY in written


def test_cmd_snapshot_out_overrides_the_dated_name(tmp_path: Path) -> None:
    scored = tmp_path / "scored.json"
    scored.write_text(json.dumps({"a": _fat_charm(present={"f": True})}))
    out = tmp_path / "elsewhere.json"

    assert cli.main(["snapshot", str(scored), "--out", str(out)]) == 0
    assert "evidence" not in json.loads(out.read_text())["a"]["features"]["f"]
