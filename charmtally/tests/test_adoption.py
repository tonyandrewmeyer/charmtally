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
        # A recent commit by default, so a charm is active unless a test says
        # otherwise. Pass `last_commit` to age it; use `_undated` to model a
        # snapshot taken before the scan recorded the field at all.
        "last_commit": "2026-06-01T09:00:00+00:00",
        **(meta or {}),
    }
    return {"name": "c", "team": "t", "repo_url": "https://x/c", "features": feature_block}


def _undated(charm: dict) -> dict:
    """The same charm as scanned before `last_commit` existed: no key at all."""
    charm["features"]["__meta__"].pop("last_commit", None)
    return charm


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


def test_active_excludes_charms_dormant_for_two_years() -> None:
    snap = _snapshot(
        {
            "fresh": _charm(meta={"last_commit": "2026-05-01T09:00:00+00:00"}),
            "just-inside": _charm(meta={"last_commit": "2024-07-01T09:00:00+00:00"}),
            "dormant": _charm(meta={"last_commit": "2023-01-05T09:00:00+00:00"}),
        },
        date="2026-06-11",
    )

    assert set(adoption.active_charms(snap)) == {"fresh", "just-inside"}


def test_dormancy_is_measured_against_the_snapshot_not_today() -> None:
    """The same charm is active in an old snapshot and dormant in a new one."""
    charm = {"c": _charm(meta={"last_commit": "2022-03-01T09:00:00+00:00"})}

    assert set(adoption.active_charms(_snapshot(charm, date="2023-06-11"))) == {"c"}
    assert adoption.active_charms(_snapshot(charm, date="2026-06-11")) == {}


def test_charms_without_a_commit_date_stay_in_the_denominator() -> None:
    """A missing `last_commit` means the scan did not look, not "dormant"."""
    snap = _snapshot({"unknown": _undated(_charm()), "null": _charm(meta={"last_commit": None})})

    assert set(adoption.active_charms(snap)) == {"unknown", "null"}
    assert not adoption.has_commit_dates(_snapshot({"unknown": _undated(_charm())}))
    assert adoption.has_commit_dates(snap)


def test_dormant_charm_leaves_the_eligible_denominator() -> None:
    snap = _snapshot(
        {
            "a": _charm(
                features={"ops.typed-relation": True, "ops.typed-config": False},
                meta={"last_commit": "2026-05-01"},
            ),
            "b": _charm(
                features={"ops.typed-relation": False, "ops.typed-config": False},
                meta={"last_commit": "2020-01-01"},
            ),
        },
        date="2026-06-11",
    )

    point = adoption.compute_typed_relation(snap)
    assert point is not None
    assert point["denominator"] == 1
    assert point["value"] == 100.0
    assert point["partial"] == ""


def test_snapshot_without_commit_dates_is_flagged_partial() -> None:
    snap = _snapshot({
        "a": _undated(_charm(features={"ops.typed-relation": True, "ops.typed-config": False}))
    })

    point = adoption.compute_typed_relation(snap)
    assert point is not None
    assert point["denominator"] == 1
    assert adoption.DORMANT_UNKNOWN in point["partial"]


def test_integration_testing_drops_dormant_charms_too() -> None:
    """Jubilant ignores eligibility, but not activity."""
    snap = _snapshot(
        {
            "live": _charm(
                features={"jubilant.integration-tests": True},
                meta={"last_commit": "2026-05-01"},
            ),
            "reactive": _charm(
                features={"jubilant.integration-tests": False},
                meta={"is_reactive": True, "last_commit": "2026-05-01"},
            ),
            "dormant": _charm(
                features={"jubilant.integration-tests": False},
                meta={"last_commit": "2019-01-01"},
            ),
        },
        date="2026-06-11",
    )

    point = adoption.compute_integration_testing(snap)
    assert point is not None
    assert point["denominator"] == 2  # the reactive charm stays, the dormant one goes
    assert point["counts"]["jubilant"] == 1


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


def test_typed_config_counts_towards_typed_juju_data() -> None:
    """load_config / load_params adopters count, relation API or not."""
    snap = _snapshot({
        "a": _charm(features={"ops.typed-relation": True, "ops.typed-config": False}),
        "b": _charm(features={"ops.typed-relation": False, "ops.typed-config": True}),
        "c": _charm(features={"ops.typed-relation": True, "ops.typed-config": True}),
        "d": _charm(features={"ops.typed-relation": False, "ops.typed-config": False}),
    })

    point = adoption.compute_typed_relation(snap)

    assert point is not None
    assert point["value"] == 75.0
    assert point["partial"] == ""


def test_typed_relation_alone_when_typed_config_not_yet_scanned() -> None:
    """Snapshots predating `ops.typed-config` keep their point, flagged partial."""
    snap = _snapshot({
        "a": _charm(features={"ops.typed-relation": True}),
        "b": _charm(features={"ops.typed-relation": False}),
    })

    point = adoption.compute_typed_relation(snap)

    assert point is not None
    assert point["value"] == 50.0
    assert point["partial"] == "ops.typed-config not yet scanned"


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


def test_integration_testing_counts_reactive_and_legacy_classic_charms() -> None:
    """Jubilant drives a deployed model, so any charm can adopt it: unlike the
    ops-API metrics this one measures against the whole corpus."""
    snap = _snapshot({
        "modern": _testing_charm(jubilant=True, pytest_operator=False, has_tests=True),
        "reactive": _charm(
            features={"jubilant.integration-tests": True, "testing.pytest-operator": False},
            meta={"is_reactive": True, "has_integration_tests": True},
        ),
        "classic": _charm(
            features={"jubilant.integration-tests": False, "testing.pytest-operator": False},
            meta={"is_legacy_classic": True, "has_integration_tests": False},
        ),
    })

    point = adoption.compute_integration_testing(snap)

    assert point is not None
    assert point["denominator"] == 3
    assert point["counts"]["jubilant"] == 2
    assert point["counts"]["no-integration-tests"] == 1


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


def _census_charm(*, charmlibs: list[str], charmhub: list[str]) -> dict:
    return _charm(
        features={"ops.collect-status": True},
        meta={
            "charmlibs_count": len(charmlibs),
            "charmlibs_names": charmlibs,
            "library_count": len(charmhub),
            "library_names": charmhub,
        },
    )


def test_census_counts_libraries_by_where_they_are_available() -> None:
    snap = _snapshot({
        # `pathops` is on no Charmhub; `loki_k8s` is on no charmlibs;
        # tls-certificates is on both, whichever side this charm took.
        "a": _census_charm(charmlibs=["pathops"], charmhub=["loki_k8s"]),
        "b": _census_charm(charmlibs=["interfaces.tls_certificates"], charmhub=[]),
    })

    point = adoption.compute_charmlibs_share(snap)

    assert point is not None
    assert point["counts"]["libraries-charmlibs-only"] == 1  # pathops
    assert point["counts"]["libraries-charmhub-only"] == 1  # loki_k8s
    assert point["counts"]["libraries-on-both"] == 1  # tls-certificates


def test_census_counts_a_paired_library_once_under_either_spelling() -> None:
    """The dist-name and import spellings of one charmlib are not two libraries."""
    snap = _snapshot({
        "a": _census_charm(charmlibs=["interfaces.tls"], charmhub=[]),
        "b": _census_charm(charmlibs=["interfaces.tls_certificates"], charmhub=[]),
    })

    point = adoption.compute_charmlibs_share(snap)

    assert point is not None
    assert point["counts"]["libraries-on-both"] == 1
    assert point["counts"]["paired-uses-charmlibs"] == 2  # both charms, one library


def test_paired_uses_split_by_the_side_each_charm_took() -> None:
    snap = _snapshot({
        "moved": _census_charm(charmlibs=["interfaces.tls"], charmhub=[]),
        "stayed": _census_charm(charmlibs=[], charmhub=["tls_certificates_interface"]),
        "midway": _census_charm(
            charmlibs=["interfaces.tls"], charmhub=["tls_certificates_interface"]
        ),
        # Unpaired on both sides: contributes to neither bucket.
        "elsewhere": _census_charm(charmlibs=["pathops"], charmhub=["loki_k8s"]),
    })

    point = adoption.compute_charmlibs_share(snap)

    assert point is not None
    assert point["counts"]["paired-uses-charmlibs"] == 1
    assert point["counts"]["paired-uses-charmhub"] == 1
    assert point["counts"]["paired-uses-both"] == 1
    assert point["breakdown"]["paired-uses-charmlibs"] == 33.3


def test_paired_uses_are_counted_per_library_not_per_charm() -> None:
    """One charm consuming two paired libraries contributes two uses."""
    snap = _snapshot({
        "a": _census_charm(charmlibs=["interfaces.tls", "rollingops"], charmhub=["loki_k8s"]),
    })

    point = adoption.compute_charmlibs_share(snap)

    assert point is not None
    assert point["counts"]["paired-uses-charmlibs"] == 2


def test_charmlibs_share_needs_the_count_in_meta() -> None:
    """A scan predating charmlibs counting yields no point, not a 0% one."""
    snap = _snapshot({
        "a": _charm(features={"ops.collect-status": True}, meta={"library_count": 2})
    })

    assert adoption.compute_charmlibs_share(snap) is None


# --- charmhub listing -------------------------------------------------------


def _listed_charm(verdict: str | None, **meta: object) -> dict:
    return _charm(
        features={"ops.collect-status": True}, meta={"charmhub_listing": verdict, **meta}
    )


def test_charmhub_listed_splits_unlisted_from_absent() -> None:
    """The two are different problems: a tick-box, and a release process."""
    snap = _snapshot({
        "on-store": _listed_charm(adoption.charmhub.LISTED),
        "hidden": _listed_charm(adoption.charmhub.UNLISTED),
        "nowhere": _listed_charm(adoption.charmhub.ABSENT),
        "also-listed": _listed_charm(adoption.charmhub.LISTED),
    })

    point = adoption.compute_charmhub_listed(snap)

    assert point is not None
    assert point["value"] == 50.0
    assert point["counts"] == {"listed": 2, "unlisted on Charmhub": 1, "not on Charmhub": 1}


def test_a_charm_the_store_did_not_answer_for_leaves_the_denominator() -> None:
    """Otherwise an outage would report as charms falling off Charmhub."""
    snap = _snapshot({
        "on-store": _listed_charm(adoption.charmhub.LISTED),
        "no-answer": _listed_charm(None),
    })

    point = adoption.compute_charmhub_listed(snap)

    assert point is not None
    assert point["denominator"] == 1
    assert point["value"] == 100.0


def test_charmhub_listed_counts_reactive_charms_too() -> None:
    """Publishing is independent of what the charm is built on."""
    snap = _snapshot({
        "modern": _listed_charm(adoption.charmhub.LISTED),
        "reactive": _listed_charm(adoption.charmhub.ABSENT, is_reactive=True),
    })

    point = adoption.compute_charmhub_listed(snap)

    assert point is not None
    assert point["denominator"] == 2


def test_charmhub_listed_still_drops_dormant_charms() -> None:
    snap = _snapshot(
        {
            "fresh": _listed_charm(adoption.charmhub.LISTED),
            "dormant": _listed_charm(
                adoption.charmhub.ABSENT, last_commit="2023-01-05T09:00:00+00:00"
            ),
        },
        date="2026-06-11",
    )

    point = adoption.compute_charmhub_listed(snap)

    assert point is not None
    assert point["denominator"] == 1


def test_charmhub_listed_needs_the_key_in_meta() -> None:
    """A scan that never asked yields no point, not a corpus that is 0% listed."""
    snap = _snapshot({"a": _charm(features={"ops.collect-status": True})})

    assert adoption.compute_charmhub_listed(snap) is None


def test_a_scan_that_asked_and_got_nothing_is_not_a_point() -> None:
    """`--no-charmhub` leaves no key; an outage leaves keys with no verdicts."""
    snap = _snapshot({"a": _listed_charm(None), "b": _listed_charm(None)})

    assert adoption.compute_charmhub_listed(snap) is None


# --- series -----------------------------------------------------------------


def test_compute_series_skips_dates_missing_the_inputs() -> None:
    old = _snapshot({"a": _charm(features={"ops.collect-status": True})}, date="2026-06-11")
    new = _snapshot({"a": _charm(features={"ops.typed-relation": True})}, date="2026-06-18")

    series = adoption.compute_series([old, new])

    assert [p["date"] for p in series[adoption.TYPED_RELATION]] == ["2026-06-18"]


def test_metric_with_unscanned_inputs_has_an_empty_series() -> None:
    """A snapshot predating rocks (and charm-user) yields no rootless point."""
    snap = _snapshot({"a": _charm(features={"ops.typed-relation": True})})

    assert adoption.compute_series([snap])[adoption.ROOTLESS] == []


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


# --- rootless (charms + rocks) ---------------------------------------------


def _k8s_charm(charm_user: str | None) -> dict:
    return _charm(meta={"has_containers": True, "charm_user": charm_user})


def _rock(run_user: str | None, *, readable: bool = True) -> dict:
    return {"name": "r", "readable": readable, "run_user": run_user}


def _rootless_snapshot(charms: dict, rocks: dict) -> trend.Snapshot:
    snap = _snapshot(charms)
    return trend.Snapshot(
        date=snap.date,
        charms=snap.charms,
        feature_names=snap.feature_names,
        rocks=rocks,
        rocks_scanned=True,
    )


def test_rootless_counts_both_halves_in_one_denominator() -> None:
    snap = _rootless_snapshot(
        {
            "nonroot": _k8s_charm("non-root"),
            "sudoer": _k8s_charm("sudoer"),
            "explicit-root": _k8s_charm("root"),
            "unset": _k8s_charm(None),
        },
        {"a": _rock("_daemon_"), "b": _rock(None)},
    )

    point = adoption.compute_rootless(snap)

    assert point is not None
    assert point["denominator"] == 6
    assert point["counts"] == {
        "run-user: _daemon_": 1,
        "charm-user: non-root": 1,
        "charm-user: sudoer": 1,
        "other non-root user": 0,
        "root (or unset)": 3,
    }
    assert point["numerator"] == 3
    assert point["value"] == 50.0


def test_rootless_excludes_machine_charms() -> None:
    """`charm-user` only affects k8s charms, so a machine charm can't adopt it."""
    snap = _rootless_snapshot(
        {"machine": _charm(meta={"has_containers": False}), "k8s": _k8s_charm("non-root")},
        {},
    )

    point = adoption.compute_rootless(snap)

    assert point is not None
    assert point["denominator"] == 1


def test_rootless_excludes_unreadable_rocks() -> None:
    """An unfetchable rockcraft.yaml is a gap, not a rock running as root."""
    snap = _rootless_snapshot(
        {"k8s": _k8s_charm("non-root")},
        {"ok": _rock(None), "gone": _rock(None, readable=False)},
    )

    point = adoption.compute_rootless(snap)

    assert point is not None
    assert point["denominator"] == 2


def test_rootless_buckets_an_unknown_user_value_separately() -> None:
    snap = _rootless_snapshot({"typo": _k8s_charm("nonroot")}, {"r": _rock("_pebble_")})

    point = adoption.compute_rootless(snap)

    assert point is not None
    assert point["counts"]["other non-root user"] == 2
    assert point["value"] == 100.0


def test_rootless_needs_both_halves_scanned() -> None:
    """Half a corpus would still read as a corpus-wide share."""
    charms_only = _snapshot({"k8s": _k8s_charm("non-root")})
    assert adoption.compute_rootless(charms_only) is None

    rocks_only = trend.Snapshot(
        date="2026-06-11",
        charms={"k8s": _charm(meta={"has_containers": True})},
        feature_names=frozenset(),
        rocks={"r": _rock("_daemon_")},
        rocks_scanned=True,
    )
    assert adoption.compute_rootless(rocks_only) is None


# --- rendering + CLI --------------------------------------------------------


def test_render_adoption_includes_cards_for_pending_metrics() -> None:
    snap = _snapshot({"a": _charm(features={"ops.typed-relation": True})})
    series = adoption.compute_series([snap])

    html = render_adoption(list(adoption.METRICS), series)

    assert "charm tech adoption" in html
    for metric in adoption.METRICS:
        assert metric.title in html
    charmlibs = adoption.metric_by_key(adoption.CHARMLIBS_SHARE)
    assert charmlibs is not None
    assert charmlibs.pending in html


def test_render_adoption_does_not_escape_authored_markup() -> None:
    """detail / denominator_note / caveats carry deliberate <code> markup."""
    # A jubilant point, so the card renders its denominator_note too — the
    # note only appears alongside a headline value.
    snap = _snapshot({"jub": _testing_charm(jubilant=True, pytest_operator=False, has_tests=True)})
    series = adoption.compute_series([snap])

    html = render_adoption(list(adoption.METRICS), series)

    assert "&lt;code&gt;" not in html
    assert "<code>run-user</code>" in html  # a rootless caveat
    assert "actively-maintained charms" in html  # the jubilant detail
    assert "not just eligible charms" in html  # the jubilant denominator_note


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
