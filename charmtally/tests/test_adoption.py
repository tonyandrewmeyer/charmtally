"""Tests for charmtally.adoption: the metric computations, their eligibility
and feature-drift guards, and the scorecard page they render into."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from .. import adoption, cli, trend
from ..dashboard import render_adoption

if TYPE_CHECKING:
    from pathlib import Path


def _charm(
    *,
    features: dict[str, bool] | None = None,
    meta: dict | None = None,
) -> dict:
    """One charm record shaped like a scored.json entry."""
    feature_block: dict[str, dict] = {
        fname: {"present": present, "evidence": [], "score": "clear-gap"}
        for fname, present in (features or {}).items()
    }
    feature_block["__meta__"] = {
        "is_reactive": False,
        "is_legacy_classic": False,
        "has_integration_tests": False,
        **(meta or {}),
    }
    return {"name": "c", "team": "t", "repo_url": "https://x/c", "features": feature_block}


def _snapshot(charms: dict, date: str = "2026-06-11") -> trend.Snapshot:
    names: set[str] = set()
    for charm in charms.values():
        names |= {k for k in charm["features"] if k != "__meta__"}
    return trend.Snapshot(date=date, charms=charms, feature_names=frozenset(names))


# --- eligibility ------------------------------------------------------------


def test_eligible_excludes_reactive_and_legacy_classic() -> None:
    snap = _snapshot({
        "modern": _charm(features={"ops.typed-relation": True}),
        "reactive": _charm(features={"ops.typed-relation": False}, meta={"is_reactive": True}),
        "classic": _charm(
            features={"ops.typed-relation": False}, meta={"is_legacy_classic": True}
        ),
    })

    assert set(adoption.eligible_charms(snap)) == {"modern"}


def test_reactive_charms_are_out_of_the_denominator() -> None:
    snap = _snapshot({
        "a": _charm(features={"ops.typed-relation": True}),
        "b": _charm(features={"ops.typed-relation": False}, meta={"is_reactive": True}),
    })

    point = adoption.compute_typed_relation(snap)

    assert point is not None
    assert (point["numerator"], point["denominator"], point["value"]) == (1, 1, 100.0)


# --- typed relation ---------------------------------------------------------


def test_typed_relation_percent_and_breakdown() -> None:
    snap = _snapshot({
        "a": _charm(features={"ops.typed-relation": True}),
        "b": _charm(features={"ops.typed-relation": False}),
        "c": _charm(features={"ops.typed-relation": False}),
        "d": _charm(features={"ops.typed-relation": False}),
    })

    point = adoption.compute_typed_relation(snap)

    assert point is not None
    assert point["value"] == 25.0
    assert point["breakdown"] == {"typed": 25.0, "untyped": 75.0}


def test_typed_relation_absent_from_catalogue_yields_no_point() -> None:
    """Feature-drift guard: a snapshot predating the feature has no data point."""
    snap = _snapshot({"a": _charm(features={"ops.collect-status": True})})

    assert adoption.compute_typed_relation(snap) is None


# --- integration testing ----------------------------------------------------


def _testing_charm(*, jubilant: bool, pytest_operator: bool, has_tests: bool) -> dict:
    return _charm(
        features={
            "jubilant.integration-tests": jubilant,
            "testing.pytest-operator": pytest_operator,
        },
        meta={"has_integration_tests": has_tests},
    )


def test_integration_testing_splits_four_ways() -> None:
    snap = _snapshot({
        "jub": _testing_charm(jubilant=True, pytest_operator=False, has_tests=True),
        "pyop": _testing_charm(jubilant=False, pytest_operator=True, has_tests=True),
        "other": _testing_charm(jubilant=False, pytest_operator=False, has_tests=True),
        "none": _testing_charm(jubilant=False, pytest_operator=False, has_tests=False),
    })

    point = adoption.compute_integration_testing(snap)

    assert point is not None
    assert point["value"] == 25.0  # the headline is the jubilant share
    assert point["counts"] == {
        "jubilant": 1,
        "pytest-operator": 1,
        "other-integration-tests": 1,
        "no-integration-tests": 1,
    }


def test_part_migrated_charm_counts_as_jubilant() -> None:
    snap = _snapshot({
        "both": _testing_charm(jubilant=True, pytest_operator=True, has_tests=True),
    })

    point = adoption.compute_integration_testing(snap)

    assert point is not None
    assert point["counts"]["jubilant"] == 1
    assert point["counts"]["pytest-operator"] == 0


def test_integration_testing_without_pytest_operator_scanned_is_partial() -> None:
    """Older snapshots have no pytest-operator column — don't report it as 0%."""
    snap = _snapshot({
        "jub": _charm(
            features={"jubilant.integration-tests": True}, meta={"has_integration_tests": True}
        ),
        "other": _charm(
            features={"jubilant.integration-tests": False}, meta={"has_integration_tests": True}
        ),
    })

    point = adoption.compute_integration_testing(snap)

    assert point is not None
    assert point["value"] == 50.0
    assert "pytest-operator" not in point["breakdown"]
    assert point["partial"]


# --- charmlibs share --------------------------------------------------------


def _libs_charm(*, charmlibs: int, charmhub: int) -> dict:
    return _charm(
        features={"ops.collect-status": True},
        meta={"charmlibs_count": charmlibs, "library_count": charmhub},
    )


def test_charmlibs_share_is_the_mean_of_per_charm_ratios() -> None:
    snap = _snapshot({
        "all-charmlibs": _libs_charm(charmlibs=2, charmhub=0),  # 100%
        "half": _libs_charm(charmlibs=1, charmhub=1),  # 50%
        "none": _libs_charm(charmlibs=0, charmhub=3),  # 0%
    })

    point = adoption.compute_charmlibs_share(snap)

    assert point is not None
    assert point["value"] == 50.0
    assert point["counts"]["charms-using-any-charmlib"] == 2
    assert point["denominator"] == 3


def test_charms_with_no_libraries_are_tracked_not_averaged() -> None:
    snap = _snapshot({
        "half": _libs_charm(charmlibs=1, charmhub=1),
        "bare": _libs_charm(charmlibs=0, charmhub=0),
    })

    point = adoption.compute_charmlibs_share(snap)

    assert point is not None
    assert point["value"] == 50.0  # the bare charm has no ratio to contribute
    assert point["denominator"] == 1
    assert point["counts"]["no-libraries"] == 1
    assert point["breakdown"]["no-libraries"] == 50.0


def test_charmlibs_share_needs_the_count_in_meta() -> None:
    """A scan predating charmlibs counting yields no point, not a 0% one."""
    snap = _snapshot({
        "a": _charm(features={"ops.collect-status": True}, meta={"library_count": 2})
    })

    assert adoption.compute_charmlibs_share(snap) is None


# --- series -----------------------------------------------------------------


def test_compute_series_skips_dates_missing_the_inputs() -> None:
    old = _snapshot({"a": _charm(features={"ops.collect-status": True})}, date="2026-06-11")
    new = _snapshot({"a": _charm(features={"ops.typed-relation": True})}, date="2026-06-18")

    series = adoption.compute_series([old, new])

    assert [p["date"] for p in series[adoption.TYPED_RELATION]] == ["2026-06-18"]


def test_pending_metric_has_an_empty_series() -> None:
    snap = _snapshot({"a": _charm(features={"ops.typed-relation": True})})

    assert adoption.compute_series([snap])[adoption.PEBBLE] == []


def test_compute_series_only_filters_to_one_metric() -> None:
    snap = _snapshot({"a": _charm(features={"ops.typed-relation": True})})

    series = adoption.compute_series([snap], only=adoption.TYPED_RELATION)

    assert set(series) == {adoption.TYPED_RELATION}


def test_latest_and_delta() -> None:
    series = [{"value": 10.0}, {"value": 12.5}]

    latest, delta = adoption.latest_and_delta(series)

    assert latest == {"value": 12.5}
    assert delta == 2.5


def test_latest_and_delta_needs_two_points() -> None:
    assert adoption.latest_and_delta([]) == (None, None)
    assert adoption.latest_and_delta([{"value": 1.0}]) == ({"value": 1.0}, None)


def test_metric_by_key() -> None:
    assert adoption.metric_by_key(adoption.TYPED_RELATION) is not None
    assert adoption.metric_by_key("no-such-metric") is None


# --- rendering + CLI --------------------------------------------------------


def test_render_adoption_includes_cards_for_pending_metrics() -> None:
    snap = _snapshot({"a": _charm(features={"ops.typed-relation": True})})
    series = adoption.compute_series([snap])

    html = render_adoption(list(adoption.METRICS), series)

    assert "charm tech adoption" in html
    for metric in adoption.METRICS:
        assert metric.title in html
    pebble = adoption.metric_by_key(adoption.PEBBLE)
    assert pebble is not None
    assert pebble.pending in html


def test_cli_adoption_writes_html_and_json(tmp_path: Path) -> None:
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()
    (snapshots / "scored-2026-06-11.json").write_text(
        json.dumps({"a": _charm(features={"ops.typed-relation": True})})
    )
    out = tmp_path / "adoption.html"

    rc = cli.main([
        "adoption",
        "--snapshots-dir",
        str(snapshots),
        "--live",
        str(tmp_path / "missing.json"),
        "--out",
        str(out),
        "--json",
    ])

    assert rc == 0
    assert out.is_file()
    payload = json.loads((tmp_path / "adoption.json").read_text())
    keys = {m["key"] for m in payload["metrics"]}
    assert keys == {m.key for m in adoption.METRICS}


def test_cli_adoption_rejects_an_unknown_metric(tmp_path: Path) -> None:
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()
    (snapshots / "scored-2026-06-11.json").write_text(
        json.dumps({"a": _charm(features={"ops.typed-relation": True})})
    )

    rc = cli.main([
        "adoption",
        "--snapshots-dir",
        str(snapshots),
        "--live",
        str(tmp_path / "missing.json"),
        "--out",
        str(tmp_path / "adoption.html"),
        "--metric",
        "no-such-metric",
    ])

    assert rc == 2
