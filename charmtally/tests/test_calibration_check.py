"""Tests for charmtally.tools.calibration_check."""

from __future__ import annotations

import json

import pytest
import yaml

from ..catalogue import default_path, load_patterns
from ..tools.calibration_check import (
    ACCEPTED,
    AGREE,
    ARCH_PATTERNS,
    REGRESSION,
    SKIPPED_ABSENT,
    SKIPPED_INHERITED,
    SKIPPED_VERDICT,
    UNFIXED_FP,
    evaluate,
    evaluate_row,
    in_bucket,
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


def test_rows_outside_the_two_buckets_are_not_evaluated():
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
