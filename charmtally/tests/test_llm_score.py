"""Tests for charmtally/llm_score.py.

Coverage targets (AI-DESIGN.md §10.4 + task spec):
  - _validate_output: valid/invalid verdict, rationale length, evidence_path rule
  - Cache: hit, miss, save, TTL prune
  - score_worth_considering: happy path, shim-charm rule, non-eligible features,
    max_calls cap, budget_exhausted, clear-gap escalation, parse-error handling,
    invalid-path handling
  - Disagreement log: escalated / parse-error / invalid-path / budget-stop entries
  - run_preflight_calibration: pass/fail precision criterion
  - CLI: llm-score / llm-calibrate subcommands, --dry-run, --prune-cache
  - LLM cannot author file paths (post-processor rejects invalid paths)
  - Cost-cap exceeded path
  - Per-feature sample selection (only LLM_ELIGIBLE_FEATURES processed)
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from ..llm_score import (
    LLM_ELIGIBLE_FEATURES,
    _cache_key,
    _detect_parse_failure_kind,
    _get_source_excerpts,
    _load_cache_entry,
    _save_cache_entry,
    _validate_output,
    count_worth_considering,
    prune_cache,
    run_preflight_calibration,
    score_worth_considering,
)

# ── Test doubles ─────────────────────────────────────────────────────────────


class FakeLLMClient:
    """Deterministic fake LLM client for unit tests."""

    def __init__(self, responses: list[str] | None = None, exhausted: bool = False) -> None:
        self._responses = list(responses or [])
        self._exhausted = exhausted
        self.calls: list[tuple[str, str, int]] = []
        self._default = json.dumps({
            "verdict": "worth-considering",
            "rationale": "No strong signal. Keeping as worth-considering.",
            "evidence_path": None,
        })

    def complete(self, prompt: str, system: str, max_tokens: int) -> str:
        self.calls.append((prompt, system, max_tokens))
        if self._responses:
            return self._responses.pop(0)
        return self._default

    def budget_exhausted(self) -> bool:
        return self._exhausted


def _clear_gap_response(rationale: str = "Clear gap; feature absent.", path: str | None = None) -> str:
    return json.dumps({"verdict": "clear-gap", "rationale": rationale, "evidence_path": path})


def _wc_response(rationale: str = "Uncertain; keeping as worth-considering.") -> str:
    return json.dumps({"verdict": "worth-considering", "rationale": rationale, "evidence_path": None})


def _make_scored(
    slug: str,
    feature: str,
    score: str,
    rationale: str = "test rationale",
    repo_url: str = "https://github.com/canonical/test-charm",
    has_containers: bool = False,
    architecture: list[str] | None = None,
    subpath: str = "",
) -> dict:
    charm: dict = {
        "name": slug.split("/")[-1],
        "team": "charm-tech",
        "repo_url": repo_url,
        "features": {
            "__meta__": {
                "has_containers": has_containers,
                "relations": [],
                "config_keys": [],
                "secret_like_config": [],
                "secret_typed_config": [],
                "is_reactive": False,
                "is_legacy_classic": False,
                "architecture": architecture or [],
            },
            feature: {
                "present": False,
                "score": score,
                "rationale": rationale,
                "evidence": [],
            },
        },
    }
    if subpath:
        charm["subpath"] = subpath
    return {slug: charm}


# ── _validate_output ──────────────────────────────────────────────────────────


def test_validate_accepts_valid_with_path():
    raw = json.dumps({
        "verdict": "clear-gap",
        "rationale": "Config key read from self.config.",
        "evidence_path": "src/charm.py",
    })  # noqa: E501
    result = _validate_output(raw, {"src/charm.py"})
    assert result is not None
    assert result["verdict"] == "clear-gap"
    assert result["evidence_path"] == "src/charm.py"


def test_validate_accepts_valid_null_path():
    raw = json.dumps({"verdict": "worth-considering", "rationale": "Not enough signal.", "evidence_path": None})
    result = _validate_output(raw, set())
    assert result is not None
    assert result["verdict"] == "worth-considering"
    assert result["evidence_path"] is None


def test_validate_rejects_invalid_verdict():
    raw = json.dumps({"verdict": "not-applicable", "rationale": "Some rationale.", "evidence_path": None})
    assert _validate_output(raw, set()) is None


def test_validate_rejects_rationale_too_long():
    long_rationale = "x" * 201
    raw = json.dumps({"verdict": "clear-gap", "rationale": long_rationale, "evidence_path": None})
    assert _validate_output(raw, set()) is None


def test_validate_rejects_empty_rationale():
    raw = json.dumps({"verdict": "clear-gap", "rationale": "", "evidence_path": None})
    assert _validate_output(raw, set()) is None


def test_validate_rejects_evidence_path_not_in_allowed():
    raw = json.dumps({"verdict": "clear-gap", "rationale": "Found it.", "evidence_path": "invented/path.py"})
    assert _validate_output(raw, {"src/charm.py"}) is None


def test_validate_rejects_invalid_json():
    assert _validate_output("not-json-at-all", set()) is None


def test_validate_rejects_non_dict_json():
    assert _validate_output('["verdict", "clear-gap"]', set()) is None


def test_validate_accepts_exact_boundary_rationale():
    rationale = "x" * 200
    raw = json.dumps({"verdict": "worth-considering", "rationale": rationale, "evidence_path": None})
    assert _validate_output(raw, set()) is not None


def test_validate_rejects_path_not_in_empty_allowed():
    raw = json.dumps({"verdict": "clear-gap", "rationale": "Found gap.", "evidence_path": "some/file.py"})
    assert _validate_output(raw, set()) is None


# ── _detect_parse_failure_kind ────────────────────────────────────────────────


def test_detect_failure_kind_invalid_json():
    assert _detect_parse_failure_kind("not json", set()) == "parse-error"


def test_detect_failure_kind_invalid_path():
    raw = json.dumps({"verdict": "clear-gap", "rationale": "x", "evidence_path": "invented.py"})
    assert _detect_parse_failure_kind(raw, {"src/charm.py"}) == "invalid-path"


# ── Cache ─────────────────────────────────────────────────────────────────────


def test_cache_miss_returns_none(tmp_path):
    key = _cache_key("https://github.com/org/repo", "abc123", "ops.secrets", "0.0.1")
    assert _load_cache_entry(tmp_path, key) is None


def test_cache_save_and_load(tmp_path):
    key = _cache_key("https://github.com/org/repo", "abc123", "ops.secrets", "0.0.1")
    entry = {
        "key": key,
        "verdict": "clear-gap",
        "rationale": "Gap confirmed.",
        "timestamp": "2026-06-29T00:00:00Z",
        "ttl_days": 30,
    }
    _save_cache_entry(tmp_path, key, entry)
    loaded = _load_cache_entry(tmp_path, key)
    assert loaded is not None
    assert loaded["verdict"] == "clear-gap"


def test_cache_prune_removes_old_entry(tmp_path):
    key = "deadbeef" * 8  # 64-char hex
    old_ts = (datetime.now(timezone.utc) - timedelta(days=31)).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = {"key": key, "verdict": "clear-gap", "timestamp": old_ts, "ttl_days": 30}
    _save_cache_entry(tmp_path, key, entry)
    removed = prune_cache(tmp_path, ttl_days=30)
    assert removed == 1
    assert _load_cache_entry(tmp_path, key) is None


def test_cache_prune_keeps_fresh_entry(tmp_path):
    key = "cafebabe" * 8
    fresh_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = {"key": key, "verdict": "worth-considering", "timestamp": fresh_ts, "ttl_days": 30}
    _save_cache_entry(tmp_path, key, entry)
    removed = prune_cache(tmp_path, ttl_days=30)
    assert removed == 0
    assert _load_cache_entry(tmp_path, key) is not None


def test_cache_prune_nonexistent_dir(tmp_path):
    assert prune_cache(tmp_path / "no-such-dir") == 0


def test_cache_key_is_deterministic():
    k1 = _cache_key("https://github.com/org/repo", "sha1", "ops.secrets", "1.0")
    k2 = _cache_key("https://github.com/org/repo", "sha1", "ops.secrets", "1.0")
    assert k1 == k2


def test_cache_key_differs_by_feature():
    k1 = _cache_key("https://github.com/org/repo", "sha1", "ops.secrets", "1.0")
    k2 = _cache_key("https://github.com/org/repo", "sha1", "pebble.checks", "1.0")
    assert k1 != k2


# ── score_worth_considering — happy path ──────────────────────────────────────


def test_score_wc_escalates_to_clear_gap(tmp_path):
    scored = _make_scored("canonical/test", "ops.secrets", "worth-considering")
    client = FakeLLMClient([_clear_gap_response("Config key read directly, no Secrets API.")])
    result = score_worth_considering(scored, client, tmp_path)
    rec = result["canonical/test"]["features"]["ops.secrets"]
    assert rec["score"] == "clear-gap"
    assert rec.get("ai_escalated") is True
    assert len(client.calls) == 1


def test_score_wc_keeps_worth_considering_on_llm_hold(tmp_path):
    scored = _make_scored("canonical/test", "ops.secrets", "worth-considering")
    client = FakeLLMClient([_wc_response()])
    result = score_worth_considering(scored, client, tmp_path)
    rec = result["canonical/test"]["features"]["ops.secrets"]
    assert rec["score"] == "worth-considering"
    assert not rec.get("ai_escalated")


def test_score_wc_skips_non_eligible_feature(tmp_path):
    scored = _make_scored("canonical/test", "ops.collect-status", "worth-considering")
    client = FakeLLMClient()
    result = score_worth_considering(scored, client, tmp_path)
    assert len(client.calls) == 0
    rec = result["canonical/test"]["features"]["ops.collect-status"]
    assert rec["score"] == "worth-considering"


def test_score_wc_skips_clear_gap_records(tmp_path):
    scored = _make_scored("canonical/test", "ops.secrets", "clear-gap")
    client = FakeLLMClient()
    score_worth_considering(scored, client, tmp_path)
    assert len(client.calls) == 0


def test_score_wc_skips_not_applicable_records(tmp_path):
    scored = _make_scored("canonical/test", "pebble.checks", "not-applicable")
    client = FakeLLMClient()
    score_worth_considering(scored, client, tmp_path)
    assert len(client.calls) == 0


def test_score_wc_skips_present_records(tmp_path):
    scored = _make_scored("canonical/test", "ops.secrets", "worth-considering")
    scored["canonical/test"]["features"]["ops.secrets"]["present"] = True
    client = FakeLLMClient()
    score_worth_considering(scored, client, tmp_path)
    assert len(client.calls) == 0


def test_score_wc_returns_copy_not_mutation(tmp_path):
    scored = _make_scored("canonical/test", "ops.secrets", "worth-considering")
    client = FakeLLMClient([_clear_gap_response("Gap confirmed.")])
    result = score_worth_considering(scored, client, tmp_path)
    # Original must not be mutated.
    assert scored["canonical/test"]["features"]["ops.secrets"]["score"] == "worth-considering"
    assert result["canonical/test"]["features"]["ops.secrets"]["score"] == "clear-gap"


def test_score_wc_empty_scored(tmp_path):
    result = score_worth_considering({}, FakeLLMClient(), tmp_path)
    assert result == {}


def test_score_wc_skips_meta_keys(tmp_path):
    scored = {"__skipped__": {"some/charm": "clone failed"}}
    client = FakeLLMClient()
    result = score_worth_considering(scored, client, tmp_path)
    assert len(client.calls) == 0
    assert result == scored


# ── score_worth_considering — shim-charm rule ────────────────────────────────


def test_score_wc_shim_charm_skipped_for_secrets(tmp_path):
    scored = _make_scored("canonical/test", "ops.secrets", "worth-considering", architecture=["shim"])
    client = FakeLLMClient([_clear_gap_response("Would be clear gap.")])
    result = score_worth_considering(scored, client, tmp_path)
    # LLM must NOT be called for shim + ops.secrets.
    assert len(client.calls) == 0
    assert result["canonical/test"]["features"]["ops.secrets"]["score"] == "worth-considering"


def test_score_wc_shim_charm_still_processed_for_other_features(tmp_path):
    scored = _make_scored("canonical/test", "pebble.checks", "worth-considering", architecture=["shim"])
    client = FakeLLMClient([_clear_gap_response("No checks found.")])
    result = score_worth_considering(scored, client, tmp_path)
    assert len(client.calls) == 1
    assert result["canonical/test"]["features"]["pebble.checks"]["score"] == "clear-gap"


# ── score_worth_considering — call-cap and budget ────────────────────────────


def test_score_wc_stops_at_max_calls(tmp_path):
    # Two separate charms, both with WC secrets records.
    scored = {}
    scored.update(_make_scored("canonical/charm-a", "ops.secrets", "worth-considering"))
    scored.update(_make_scored("canonical/charm-b", "ops.secrets", "worth-considering"))
    client = FakeLLMClient([_wc_response(), _wc_response()])
    score_worth_considering(scored, client, tmp_path, max_calls=1)
    # Only one call should be made; second record gets budget-stop in disagreement log.
    assert len(client.calls) == 1


def test_score_wc_stops_when_budget_exhausted(tmp_path):
    scored = _make_scored("canonical/test", "ops.secrets", "worth-considering")
    client = FakeLLMClient(exhausted=True)
    result = score_worth_considering(scored, client, tmp_path)
    assert len(client.calls) == 0
    rec = result["canonical/test"]["features"]["ops.secrets"]
    assert rec["score"] == "worth-considering"


# ── score_worth_considering — cache integration ───────────────────────────────


def test_score_wc_cache_hit_skips_llm(tmp_path):
    repo_url = "https://github.com/canonical/test-charm"
    feature_id = "ops.secrets"
    key = _cache_key(repo_url, "unknown", feature_id, "")
    cache_entry = {
        "key": key,
        "verdict": "clear-gap",
        "rationale": "Cached gap.",
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ttl_days": 30,
    }
    _save_cache_entry(tmp_path, key, cache_entry)

    scored = _make_scored("canonical/test", feature_id, "worth-considering", repo_url=repo_url)
    client = FakeLLMClient()
    result = score_worth_considering(scored, client, tmp_path)
    assert len(client.calls) == 0
    assert result["canonical/test"]["features"][feature_id]["score"] == "clear-gap"
    assert result["canonical/test"]["features"][feature_id].get("ai_escalated") is True


def test_score_wc_result_saved_to_cache(tmp_path):
    scored = _make_scored("canonical/test", "pebble.checks", "worth-considering")
    client = FakeLLMClient([_clear_gap_response("No checks in pebble layer.")])
    score_worth_considering(scored, client, tmp_path)
    repo_url = "https://github.com/canonical/test-charm"
    key = _cache_key(repo_url, "unknown", "pebble.checks", "")
    cached = _load_cache_entry(tmp_path, key)
    assert cached is not None
    assert cached["verdict"] == "clear-gap"


# ── Disagreement log ──────────────────────────────────────────────────────────


def test_disagreement_log_escalated_entry(tmp_path):
    scored = _make_scored("canonical/test", "ops.secrets", "worth-considering")
    client = FakeLLMClient([_clear_gap_response("Confirmed gap.")])
    score_worth_considering(scored, client, tmp_path)

    log = (tmp_path / "llm-disagreements.jsonl").read_text()
    entries = [json.loads(line) for line in log.strip().splitlines()]
    escalated = [e for e in entries if e.get("outcome") == "escalated"]
    assert len(escalated) == 1
    assert escalated[0]["charm_slug"] == "canonical/test"
    assert escalated[0]["feature_id"] == "ops.secrets"
    assert escalated[0]["llm_verdict"] == "clear-gap"


def test_disagreement_log_parse_error_entry(tmp_path):
    scored = _make_scored("canonical/test", "pebble.checks", "worth-considering")
    client = FakeLLMClient(["this is not json"])
    score_worth_considering(scored, client, tmp_path)

    log = (tmp_path / "llm-disagreements.jsonl").read_text()
    entries = [json.loads(line) for line in log.strip().splitlines()]
    errors = [e for e in entries if e.get("outcome") == "parse-error"]
    assert len(errors) == 1
    assert errors[0]["charm_slug"] == "canonical/test"


def test_disagreement_log_invalid_path_entry(tmp_path):
    raw = json.dumps({"verdict": "clear-gap", "rationale": "Found it.", "evidence_path": "invented/file.py"})
    scored = _make_scored("canonical/test", "jubilant.integration-tests", "worth-considering")
    client = FakeLLMClient([raw])
    score_worth_considering(scored, client, tmp_path)

    log = (tmp_path / "llm-disagreements.jsonl").read_text()
    entries = [json.loads(line) for line in log.strip().splitlines()]
    invalid = [e for e in entries if e.get("outcome") == "invalid-path"]
    assert len(invalid) == 1


def test_disagreement_log_budget_stop_entry(tmp_path):
    scored = _make_scored("canonical/test", "ops.secrets", "worth-considering")
    client = FakeLLMClient(exhausted=True)
    score_worth_considering(scored, client, tmp_path)

    log = (tmp_path / "llm-disagreements.jsonl").read_text()
    entries = [json.loads(line) for line in log.strip().splitlines()]
    stops = [e for e in entries if e.get("outcome") == "budget-stop"]
    assert len(stops) == 1


def test_disagreement_log_is_append_only(tmp_path):
    scored = _make_scored("canonical/test", "ops.secrets", "worth-considering")
    client = FakeLLMClient([_clear_gap_response("First run.")])
    score_worth_considering(scored, client, tmp_path)

    # Manually add a pre-existing entry.
    log_path = tmp_path / "llm-disagreements.jsonl"
    prior_count = len(log_path.read_text().strip().splitlines())

    scored2 = _make_scored("canonical/test2", "pebble.checks", "worth-considering")
    client2 = FakeLLMClient([_clear_gap_response("Second run.")])
    score_worth_considering(scored2, client2, tmp_path)

    lines = log_path.read_text().strip().splitlines()
    assert len(lines) > prior_count, "Log should have grown, not been overwritten"


# ── run_preflight_calibration ─────────────────────────────────────────────────


def test_calibration_passes_at_90_percent(tmp_path):
    scored = {}
    for i in range(10):
        scored.update(_make_scored(f"canonical/charm-{i}", "ops.secrets", "worth-considering"))

    # LLM always returns worth-considering; 9 human labels agree, 1 disagrees.
    ground_truth = [
        {"charm_slug": f"canonical/charm-{i}", "feature_id": "ops.secrets", "human_verdict": "worth-considering"}
        for i in range(9)
    ] + [{"charm_slug": "canonical/charm-9", "feature_id": "ops.secrets", "human_verdict": "worth-considering"}]

    client = FakeLLMClient()  # default always returns worth-considering
    result = run_preflight_calibration(scored, client, tmp_path, ground_truth)
    assert result["passed"] is True
    assert result["agreement"] >= 0.9


def test_calibration_fails_below_90_percent(tmp_path):
    scored = {}
    for i in range(10):
        scored.update(_make_scored(f"canonical/charm-{i}", "ops.secrets", "worth-considering"))

    # LLM returns WC but human says clear-gap for 5 of them → 50% agreement.
    ground_truth = [
        {"charm_slug": f"canonical/charm-{i}", "feature_id": "ops.secrets", "human_verdict": "clear-gap"}
        for i in range(5)
    ] + [
        {"charm_slug": f"canonical/charm-{i}", "feature_id": "ops.secrets", "human_verdict": "worth-considering"}
        for i in range(5, 10)
    ]

    client = FakeLLMClient()  # default always returns worth-considering
    result = run_preflight_calibration(scored, client, tmp_path, ground_truth)
    assert result["passed"] is False
    assert result["agreement"] == 0.5


def test_calibration_empty_ground_truth(tmp_path):
    result = run_preflight_calibration({}, FakeLLMClient(), tmp_path, [])
    assert result["passed"] is True
    assert result["total"] == 0
    assert result["agreed"] == 0


def test_calibration_returns_correct_fields(tmp_path):
    scored = _make_scored("canonical/test", "ops.secrets", "worth-considering")
    gt = [{"charm_slug": "canonical/test", "feature_id": "ops.secrets", "human_verdict": "worth-considering"}]
    client = FakeLLMClient()  # returns worth-considering by default
    result = run_preflight_calibration(scored, client, tmp_path, gt)
    assert set(result.keys()) == {"agreement", "total", "agreed", "passed"}
    assert result["total"] == 1
    assert result["agreed"] == 1


# ── LLM_ELIGIBLE_FEATURES content ────────────────────────────────────────────


def test_llm_eligible_features_contains_top5():
    expected = {
        "ops.relation-app-data",
        "jubilant.integration-tests",
        "ops.secrets",
        "pebble.checks",
        "ops.pebble-custom-notice",
    }
    assert expected == LLM_ELIGIBLE_FEATURES


def test_non_eligible_features_not_processed(tmp_path):
    non_eligible = [
        "ops.collect-status",
        "ops.stored-state",
        "ops.pebble-ready",
        "ops.leader-events",
        "charmlibs.data-platform",
    ]
    client = FakeLLMClient()
    for feat in non_eligible:
        scored = _make_scored("canonical/test", feat, "worth-considering")
        score_worth_considering(scored, client, tmp_path)
    assert len(client.calls) == 0


# ── count_worth_considering (--dry-run helper) ────────────────────────────────


def test_count_wc_counts_eligible_only(tmp_path):
    scored = {}
    scored.update(_make_scored("canonical/a", "ops.secrets", "worth-considering"))
    scored.update(_make_scored("canonical/b", "pebble.checks", "worth-considering"))
    scored.update(_make_scored("canonical/c", "ops.collect-status", "worth-considering"))
    counts = count_worth_considering(scored)
    assert counts["ops.secrets"] == 1
    assert counts["pebble.checks"] == 1
    assert "ops.collect-status" not in counts


def test_count_wc_excludes_non_wc_scores():
    scored = {}
    scored.update(_make_scored("canonical/a", "ops.secrets", "clear-gap"))
    scored.update(_make_scored("canonical/b", "ops.secrets", "not-applicable"))
    counts = count_worth_considering(scored)
    assert counts["ops.secrets"] == 0


# ── _get_source_excerpts ──────────────────────────────────────────────────────


def test_get_source_excerpts_no_workdir():
    excerpts = _get_source_excerpts("canonical/test", "", "ops.secrets", {}, None)
    assert excerpts == []


def test_get_source_excerpts_missing_dir(tmp_path):
    excerpts = _get_source_excerpts("canonical/nonexistent", "", "ops.secrets", {}, tmp_path)
    assert excerpts == []


def test_get_source_excerpts_reads_lines(tmp_path):
    slug = "canonical/test-charm"
    charm_root = tmp_path / slug
    charm_root.mkdir(parents=True)
    src_dir = charm_root / "src"
    src_dir.mkdir()
    charm_file = src_dir / "charm.py"
    charm_file.write_text("\n".join(f"line {i}" for i in range(50)))

    features_dict = {
        "ops.secrets": {
            "present": False,
            "evidence": [{"file": "src/charm.py", "line": 10, "detector_kind": "regex", "snippet": "secret-like"}],
        }
    }
    excerpts = _get_source_excerpts(slug, "", "ops.secrets", features_dict, tmp_path)
    assert len(excerpts) == 1
    assert excerpts[0]["path"] == "src/charm.py"
    assert "line 9" in excerpts[0]["content"]


# ── CLI wiring ────────────────────────────────────────────────────────────────


def test_cli_llm_score_subcommand_exists():
    from ..cli import main

    # --help should list llm-score; no error on parsing the subcommand name.
    with pytest.raises(SystemExit) as exc_info:
        main(["llm-score", "--help"])
    assert exc_info.value.code == 0


def test_cli_llm_calibrate_subcommand_exists():
    from ..cli import main

    with pytest.raises(SystemExit) as exc_info:
        main(["llm-calibrate", "--help"])
    assert exc_info.value.code == 0


def test_cli_llm_score_dry_run(tmp_path):
    scored = _make_scored("canonical/test", "ops.secrets", "worth-considering")
    scored_path = tmp_path / "scored.json"
    scored_path.write_text(json.dumps(scored))

    from ..cli import main

    rc = main(["llm-score", str(scored_path), "--dry-run", "--cache-dir", str(tmp_path / "cache")])
    assert rc == 0


def test_cli_llm_score_prune_cache(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    scored_path = tmp_path / "scored.json"
    scored_path.write_text("{}")

    from ..cli import main

    rc = main(["llm-score", str(scored_path), "--prune-cache", "--cache-dir", str(cache_dir)])
    assert rc == 0


def test_cli_llm_score_writes_output(tmp_path, monkeypatch):
    """llm-score writes the scored JSON when no LLM key is needed (no WC records)."""
    scored = _make_scored("canonical/test", "ops.secrets", "clear-gap")
    scored_path = tmp_path / "scored.json"
    scored_path.write_text(json.dumps(scored))
    out_path = tmp_path / "llm-scored.json"

    from ..cli import main

    rc = main([
        "llm-score",
        str(scored_path),
        "--out",
        str(out_path),
        "--cache-dir",
        str(tmp_path / "cache"),
    ])
    assert rc == 0
    assert out_path.is_file()
    result = json.loads(out_path.read_text())
    assert "canonical/test" in result
