"""Tests for charmtally.tools.calibration_check."""

from __future__ import annotations

import json

import pytest
import yaml

from ..catalogue import default_path, load, load_patterns
from ..tools.calibration_check import (
    ACCEPTED,
    AGREE,
    ARCH_PATTERNS,
    BUCKETS,
    CLEAR_GAP_PREFIX,
    REGRESSION,
    SKIPPED_ABSENT,
    SKIPPED_INHERITED,
    SKIPPED_VERDICT,
    UNFIXED_FP,
    clear_gap_feature,
    evaluate,
    evaluate_row,
    in_bucket,
    in_clear_gap,
    main,
    render,
    to_json,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _results(*charms: tuple[str, list[str]], skipped: dict | None = None) -> dict:
    """Build a minimal results.json from (slug, architecture) pairs."""
    out: dict = {
        slug: {
            "name": slug,
            "team": "",
            "repo_url": f"https://github.com/canonical/{slug}",
            "features": {"__meta__": {"architecture": list(archs), "repo_sha": "abc123"}},
        }
        for slug, archs in charms
    }
    if skipped is not None:
        out["__skipped__"] = skipped
    return out


def _with_feature(results: dict, slug: str, feature: str, **record) -> dict:
    """Add one feature record to a charm built by `_results`."""
    results[slug]["features"][feature] = dict(record)
    return results


def _record(slug: str, bucket: str, verdict: str, **extra) -> dict:
    rec = {
        "slug": slug,
        "bucket": bucket,
        "feature": f"architecture:{bucket}",
        "verdict": verdict,
        "round": 26,
        "date": "2026-07-26",
        "reason": "a one-line reason",
        "source_line": 1999,
    }
    rec.update(extra)
    return rec


def _ledger(*records: dict) -> dict:
    return {"version": 1, "records": list(records)}


def _exceptions(*entries: dict) -> dict:
    return {"version": 1, "exceptions": list(entries)}


def _kinds(report) -> list[str]:
    return [o.kind for o in report.outcomes]


# ---------------------------------------------------------------------------
# Bucket membership
# ---------------------------------------------------------------------------


def test_in_bucket_reconcile_reads_the_raw_architecture_list():
    assert in_bucket("reconcile", {"architecture": ["reconcile"]})
    assert not in_bucket("reconcile", {"architecture": []})
    assert not in_bucket("reconcile", {"architecture": ["part-reconcile"]})


def test_in_bucket_reconcile_ignores_the_dashboards_single_pick():
    # The regression this guards: dashboard._primary_arch ranks component-graph
    # above reconcile, so folding paas_charm into component-graph (#42) moved
    # charms out of the *displayed* reconcile bucket with no detector change.
    # Membership is the detector's own output, so both labels count.
    meta = {"architecture": ["reconcile", "component-graph"]}
    assert in_bucket("reconcile", meta)
    assert not in_bucket("delta", meta)


def test_in_bucket_delta_is_the_residual():
    assert in_bucket("delta", {"architecture": []})
    assert in_bucket("delta", {})
    assert not in_bucket("delta", {"architecture": ["component-graph"]})


def test_in_bucket_delta_ignores_reactive_and_legacy_classic():
    # `charm-containerd` is a reactive charm the ledger adjudicated TP on the
    # delta/reconcile axis. _primary_arch calls it "reactive"; the axis the
    # round ruled on is still "no holistic pattern matched".
    meta = {"architecture": [], "is_reactive": True, "is_legacy_classic": True}
    assert in_bucket("delta", meta)


def test_clear_gap_membership_is_the_detector_not_the_score():
    # The whole reason the bucket is readable: `present` is one detector, while
    # `score` folds in the architecture axis, is_reactive, is_legacy_classic,
    # status-set-directly and the relation list. A charm scored not-applicable
    # because it reconciles is still missing a collect-status handler.
    features = {"ops.collect-status": {"present": False, "score": "not-applicable"}}
    assert in_clear_gap("ops.collect-status", features)
    features = {"ops.collect-status": {"present": True}}
    assert not in_clear_gap("ops.collect-status", features)


def test_clear_gap_membership_is_none_when_the_feature_was_never_looked_for():
    # `scan --feature` or a catalogue rename, not "looked and found nothing".
    assert in_clear_gap("ops.collect-status", {}) is None


def test_clear_gap_feature_names_the_feature_or_nothing():
    assert clear_gap_feature("clear-gap:ops.collect-status") == "ops.collect-status"
    assert clear_gap_feature("reconcile") is None
    assert clear_gap_feature("delta") is None


def test_clear_gap_buckets_name_a_real_feature():
    # Same guard as ARCH_PATTERNS: a bucket naming a feature `features.yaml`
    # has since renamed would skip every one of its rows rather than fail.
    catalogue = {f.name for f in load(default_path())}
    named = [b.removeprefix(CLEAR_GAP_PREFIX) for b in BUCKETS if b.startswith(CLEAR_GAP_PREFIX)]
    assert named
    assert set(named) <= catalogue


def test_arch_patterns_match_the_catalogue():
    # `delta` is the residual, so a new pattern landing in features.yaml
    # without being listed here would silently shrink it and clear delta rows
    # that should have failed.
    catalogue = {p.name for p in load_patterns(default_path())}
    assert catalogue == set(ARCH_PATTERNS)


# ---------------------------------------------------------------------------
# Row evaluation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bucket", "verdict", "archs", "expected"),
    [
        ("reconcile", "TP", ["reconcile"], AGREE),
        ("reconcile", "TP", [], REGRESSION),
        ("reconcile", "FP", [], AGREE),
        ("reconcile", "FP", ["reconcile"], UNFIXED_FP),
        ("delta", "TP", [], AGREE),
        ("delta", "TP", ["reconcile"], REGRESSION),
        ("delta", "FP", ["reconcile"], AGREE),
        ("delta", "FP", [], UNFIXED_FP),
    ],
)
def test_evaluate_row_classifies_each_combination(bucket, verdict, archs, expected):
    results = _results(("a-charm", archs))
    assert evaluate_row(_record("a-charm", bucket, verdict), results).kind == expected


@pytest.mark.parametrize(
    ("verdict", "present", "expected"),
    [
        ("TP", False, AGREE),  # the gap the round read is still open
        ("TP", True, REGRESSION),  # gap closed — an upstream adoption, or a detector change
        ("FP", True, AGREE),  # the miscall was fixed and the feature now reads present
        ("FP", False, UNFIXED_FP),  # the detector still cannot see it
    ],
)
def test_evaluate_row_classifies_a_clear_gap_row(verdict, present, expected):
    bucket = "clear-gap:ops.collect-status"
    results = _with_feature(
        _results(("a-charm", [])), "a-charm", "ops.collect-status", present=present
    )
    assert evaluate_row(_record("a-charm", bucket, verdict), results).kind == expected


def test_a_clear_gap_row_ignores_the_architecture_label():
    # A `reconcile` charm scores not-applicable for collect-status, but the
    # ledger row is about the handler, not the score. Reading membership off
    # the score instead turns five round-15 TPs into regressions that say
    # nothing about collect-status.
    results = _with_feature(
        _results(("a-charm", ["reconcile"])),
        "a-charm",
        "ops.collect-status",
        present=False,
        score="not-applicable",
    )
    record = _record("a-charm", "clear-gap:ops.collect-status", "TP")
    assert evaluate_row(record, results).kind == AGREE


def test_a_feature_missing_from_the_scan_is_skipped_not_failed():
    results = _results(("a-charm", []))
    record = _record("a-charm", "clear-gap:ops.collect-status", "TP")
    outcome = evaluate_row(record, results)
    assert outcome.kind == SKIPPED_ABSENT
    assert "ops.collect-status not in the scan output" in outcome.note


@pytest.mark.parametrize("verdict", ["NA", "other", "unadjudicated"])
def test_only_tp_and_fp_are_checkable(verdict):
    # Folding `other` into TP or FP would be re-adjudicating, so it is skipped
    # and the skip is visible in the output.
    results = _results(("a-charm", ["reconcile"]))
    outcome = evaluate_row(_record("a-charm", "delta", verdict), results)
    assert outcome.kind == SKIPPED_VERDICT
    assert verdict in outcome.note


def test_inherited_verdicts_are_skipped():
    results = _results(("twin", []))
    record = _record("twin", "reconcile", "TP", inherited_from="original")
    outcome = evaluate_row(record, results)
    assert outcome.kind == SKIPPED_INHERITED
    assert "original" in outcome.note


def test_counts_toward_precision_does_not_skip_a_row():
    # The deliberate decision: that flag governs precision arithmetic, which
    # this check does not compute. Honouring it would drop two genuine
    # verdicts (superset-k8s-operator, postgresql-test-app).
    results = _results(("a-charm", []))
    record = _record("a-charm", "reconcile", "TP", counts_toward_precision=False)
    assert evaluate_row(record, results).kind == REGRESSION


def test_a_charm_missing_from_the_scan_is_skipped_not_failed():
    outcome = evaluate_row(_record("gone", "reconcile", "TP"), _results())
    assert outcome.kind == SKIPPED_ABSENT


def test_an_absent_charm_reports_the_scans_own_skip_reason():
    results = _results(skipped={"gone": "not-a-charm — decoy charmcraft.yaml"})
    outcome = evaluate_row(_record("gone", "reconcile", "TP"), results)
    assert outcome.kind == SKIPPED_ABSENT
    assert "decoy charmcraft.yaml" in outcome.note


def test_the_current_verdict_is_checked_not_the_superseded_history():
    # superset-k8s-operator's shape: TP, four rounds of unadjudicated, TP again.
    # The top-level fields hold the most recent event, so a checkable verdict
    # is not skipped because an earlier one was not.
    record = _record(
        "a-charm",
        "reconcile",
        "TP",
        history=[
            {"round": 41, "verdict": "unadjudicated", "superseded": True},
            {"round": 46, "verdict": "unadjudicated", "superseded": True},
        ],
    )
    results = _results(("a-charm", ["reconcile"]))
    assert evaluate_row(record, results).kind == AGREE


def test_rows_outside_the_checked_buckets_are_not_evaluated():
    ledger = _ledger(
        _record("a-charm", "clear-gap:ops.secrets", "TP"),
        _record("b-charm", "reconcile", "TP"),
    )
    report = evaluate(_results(("a-charm", []), ("b-charm", ["reconcile"])), ledger)
    assert [o.slug for o in report.outcomes] == ["b-charm"]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


def test_an_exception_accepts_a_divergence():
    ledger = _ledger(_record("a-charm", "reconcile", "TP"))
    exc = _exceptions({
        "slug": "a-charm",
        "bucket": "reconcile",
        "ledger_verdict": "TP",
        "accepted": "2026-09-11",
        "reason": "moved buckets",
    })
    report = evaluate(_results(("a-charm", [])), ledger, exc)
    assert _kinds(report) == [ACCEPTED]
    assert report.ok()


def test_an_exception_does_not_silence_the_opposite_divergence():
    # Written for a TP that left the bucket; must not also accept an FP that
    # came back into it.
    ledger = _ledger(_record("a-charm", "reconcile", "FP"))
    exc = _exceptions({
        "slug": "a-charm",
        "bucket": "reconcile",
        "ledger_verdict": "TP",
        "accepted": "2026-09-11",
        "reason": "moved buckets",
    })
    report = evaluate(_results(("a-charm", ["reconcile"])), ledger, exc)
    assert _kinds(report) == [UNFIXED_FP]
    assert not report.ok()
    assert report.invalid_exceptions


def test_editing_the_ledger_row_invalidates_its_exception():
    # The other documented way to accept a change. The exception names the
    # verdict it was written against, so it fails loudly rather than carrying
    # over onto a verdict nobody checked it against.
    ledger = _ledger(_record("a-charm", "reconcile", "FP"))
    exc = _exceptions({
        "slug": "a-charm",
        "bucket": "reconcile",
        "ledger_verdict": "TP",
        "accepted": "2026-09-11",
        "reason": "stale",
    })
    report = evaluate(_results(("a-charm", ["reconcile"])), ledger, exc)
    entry, why = report.invalid_exceptions[0]
    assert entry["slug"] == "a-charm"
    assert "'TP'" in why and "'FP'" in why


def test_an_exception_for_a_row_that_now_agrees_is_stale():
    ledger = _ledger(_record("a-charm", "reconcile", "TP"))
    exc = _exceptions({
        "slug": "a-charm",
        "bucket": "reconcile",
        "ledger_verdict": "TP",
        "accepted": "2026-09-11",
        "reason": "was diverging",
    })
    report = evaluate(_results(("a-charm", ["reconcile"])), ledger, exc)
    assert _kinds(report) == [AGREE]
    assert [e["slug"] for e in report.stale_exceptions] == ["a-charm"]
    # Good news, so not a failure by default; --strict makes it one.
    assert report.ok()
    assert not report.ok(strict=True)


def test_an_exception_naming_an_unknown_row_is_invalid():
    ledger = _ledger(_record("a-charm", "reconcile", "TP"))
    exc = _exceptions({
        "slug": "typo-charm",
        "bucket": "reconcile",
        "ledger_verdict": "TP",
        "accepted": "2026-09-11",
        "reason": "?",
    })
    report = evaluate(_results(("a-charm", ["reconcile"])), ledger, exc)
    assert not report.ok()
    assert "no such" in report.invalid_exceptions[0][1]


def test_no_exceptions_file_is_not_an_error():
    ledger = _ledger(_record("a-charm", "reconcile", "TP"))
    report = evaluate(_results(("a-charm", ["reconcile"])), ledger, None)
    assert report.ok()


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def test_a_failure_carries_enough_to_triage_it():
    # The three innocent-or-guilty explanations a human has to tell apart:
    # the detector regressed, the ledger row is stale, or the charm changed
    # upstream. All three need the recorded verdict, its round and line, what
    # the detector says now, and the SHA that was scanned.
    ledger = _ledger(
        _record("a-charm", "reconcile", "TP", verdict_as_written="TP*", round=26, source_line=1999)
    )
    text = render(evaluate(_results(("a-charm", ["part-reconcile"])), ledger))
    assert "REGRESSION  a-charm  [reconcile]" in text
    assert "round 26" in text
    assert "2026-07-26" in text
    assert "CALIBRATION.md:1999" in text
    assert 'written "TP*"' in text
    assert "a one-line reason" in text
    assert "not in `reconcile` — architecture: [part-reconcile]" in text
    assert "repo_sha abc123" in text
    assert "FAIL" in text


def test_a_clear_gap_failure_names_the_feature_not_the_architecture():
    ledger = _ledger(_record("a-charm", "clear-gap:ops.collect-status", "TP"))
    results = _with_feature(
        _results(("a-charm", ["reconcile"])), "a-charm", "ops.collect-status", present=True
    )
    text = render(evaluate(results, ledger))
    assert "REGRESSION  a-charm  [clear-gap:ops.collect-status]" in text
    assert "not in `clear-gap:ops.collect-status` — ops.collect-status present" in text
    # The architecture list belongs to the other two buckets; printing it here
    # would invite reading the divergence as an architecture move.
    assert "architecture:" not in text


def test_a_clear_gap_row_still_in_the_bucket_reports_its_score():
    ledger = _ledger(_record("a-charm", "clear-gap:ops.collect-status", "FP"))
    results = _with_feature(
        _results(("a-charm", [])),
        "a-charm",
        "ops.collect-status",
        present=False,
        score="clear-gap",
    )
    text = render(evaluate(results, ledger))
    assert (
        "still in `clear-gap:ops.collect-status` — ops.collect-status absent, scored clear-gap"
        in text
    )


def test_the_failure_output_explains_how_to_accept_a_change():
    ledger = _ledger(_record("a-charm", "reconcile", "TP"))
    text = render(evaluate(_results(("a-charm", [])), ledger))
    assert "calibration-exceptions.yaml" in text
    assert "calibration-ledger.yaml" in text
    assert "`history`" in text


def test_skips_are_reported_not_just_counted():
    ledger = _ledger(
        _record("gone", "reconcile", "TP"),
        _record("twin", "reconcile", "TP", inherited_from="original"),
        _record("vague", "delta", "unadjudicated"),
    )
    text = render(evaluate(_results(("twin", ["reconcile"]), ("vague", [])), ledger))
    assert "gone [reconcile] — not in the scan output" in text
    assert "twin [reconcile] — verdict inherited from original" in text
    assert "unadjudicated: 1" in text
    assert "PASS" in text


def test_a_clean_run_says_pass():
    ledger = _ledger(_record("a-charm", "reconcile", "TP"), _record("b-charm", "delta", "TP"))
    text = render(evaluate(_results(("a-charm", ["reconcile"]), ("b-charm", [])), ledger))
    assert text.endswith("PASS")
    assert "FAILURES" not in text


def test_json_output_round_trips():
    ledger = _ledger(_record("a-charm", "reconcile", "TP"))
    payload = to_json(evaluate(_results(("a-charm", [])), ledger))
    assert payload["ok"] is False
    assert payload["outcomes"][0]["kind"] == REGRESSION
    assert payload["outcomes"][0]["source_line"] == 1999
    json.dumps(payload)  # must be serialisable


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _write(tmp_path, ledger, results, exceptions=None):
    (tmp_path / "calibration-ledger.yaml").write_text(yaml.safe_dump(ledger))
    (tmp_path / "results.json").write_text(json.dumps(results))
    if exceptions is not None:
        (tmp_path / "calibration-exceptions.yaml").write_text(yaml.safe_dump(exceptions))
    return [
        "--ledger", str(tmp_path / "calibration-ledger.yaml"),
        "--results", str(tmp_path / "results.json"),
        "--exceptions", str(tmp_path / "calibration-exceptions.yaml"),
    ]  # fmt: skip


def test_main_exits_zero_when_the_detectors_agree(tmp_path, capsys):
    argv = _write(
        tmp_path, _ledger(_record("a", "reconcile", "TP")), _results(("a", ["reconcile"]))
    )
    assert main(argv) == 0
    assert capsys.readouterr().out.strip().endswith("PASS")


def test_main_exits_one_on_a_regression(tmp_path, capsys):
    argv = _write(tmp_path, _ledger(_record("a", "reconcile", "TP")), _results(("a", [])))
    assert main(argv) == 1
    assert "REGRESSION" in capsys.readouterr().out


def test_main_strict_fails_on_a_stale_exception(tmp_path, capsys):
    exc = _exceptions({
        "slug": "a",
        "bucket": "reconcile",
        "ledger_verdict": "TP",
        "accepted": "2026-09-11",
        "reason": "x",
    })
    argv = _write(
        tmp_path, _ledger(_record("a", "reconcile", "TP")), _results(("a", ["reconcile"])), exc
    )
    assert main(argv) == 0
    assert main([*argv, "--strict"]) == 1
    assert "STALE" in capsys.readouterr().out


def test_main_emits_json(tmp_path, capsys):
    argv = _write(tmp_path, _ledger(_record("a", "reconcile", "TP")), _results(("a", [])))
    assert main([*argv, "--format", "json"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_main_reports_a_missing_input_file(tmp_path, capsys):
    assert (
        main(["--ledger", str(tmp_path / "nope.yaml"), "--results", str(tmp_path / "r.json")]) == 2
    )
    assert "no such file" in capsys.readouterr().err
