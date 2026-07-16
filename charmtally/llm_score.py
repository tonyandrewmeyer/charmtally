"""LLM scoring pass — reclassify worth-considering records using an LLM.

The rule-based scanner assigns one of three labels per (charm, feature) pair.
This module runs a bounded reclassification over ``worth-considering`` records
for the five v1-eligible features, potentially escalating them to ``clear-gap``
when the LLM agrees the feature is genuinely absent.

Design contract: AI-DESIGN.md (canonical-work-queue non-roadmap/feature-dashboard/).
Eligible features: AI-SCORING-PRIORITISATION.md §3.1.

Public API:
    score_worth_considering(scored, client, cache_dir, ...) -> dict
    prune_cache(cache_dir, ttl_days) -> int
    run_preflight_calibration(scored, client, cache_dir, ground_truth, ...) -> dict

Types:
    LLMVerdict
    LLMClient (Protocol)
    OpenRouterClient
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

# The five features eligible for LLM refinement in v1 (AI-SCORING-PRIORITISATION §3.1).
LLM_ELIGIBLE_FEATURES: frozenset[str] = frozenset({
    "ops.relation-app-data",
    "jubilant.integration-tests",
    "ops.secrets",
    "pebble.checks",
    "ops.pebble-custom-notice",
})

# Hard cap on spend per run (AI-DESIGN.md §5.3).
_BUDGET_HARD_LIMIT_USD = 5.0

# Haiku 4.5 pricing (AI-DESIGN.md §5.2).
_INPUT_COST_PER_TOKEN = 0.80 / 1_000_000  # $0.80/MTok
_OUTPUT_COST_PER_TOKEN = 4.00 / 1_000_000  # $4.00/MTok

# Default model for the LLM scoring pass.
_OPENROUTER_MODEL = "anthropic/claude-haiku-4-5"
_OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"

_DISAGREEMENT_LOG_NAME = "llm-disagreements.jsonl"
_CACHE_TTL_DAYS = 30

# System message with few-shot examples (AI-DESIGN.md §4.1 and §4.2).
_SYSTEM_PROMPT = """\
You are a code-quality analyst reviewing Canonical charm repositories.
A charm is a software operator (written in Python with the ops framework)
that automates a piece of infrastructure on Ubuntu/Kubernetes.

Your task is to reclassify a single (charm, feature) pair that the
rule-based scanner has tagged "worth-considering" — meaning it found
weak indirect signals but could not confirm the feature is absent.

You must decide:
  - "worth-considering": keep as is; not enough evidence to escalate.
  - "clear-gap": escalate; the feature is clearly absent and the charm
    should be using it.

Respond with JSON only — no prose outside the JSON block:

  {
    "verdict":       "worth-considering" | "clear-gap",
    "rationale":     "<plain prose, max 200 characters>",
    "evidence_path": "<path>" | null
  }

Rules:
1. evidence_path, when non-null, MUST be one of the paths listed in the
   source_excerpts below. Do not construct or guess a path.
2. If you are not confident, return "worth-considering". The scanner is
   the primary detector; you are a conservative triage filter.
3. Rationale must be plain text — no markdown, no bullet points.
4. If src/charm.py appears to be a thin delegation layer — it subclasses
   an external non-ops, non-charms.* module and has fewer than ~80 lines
   of Python — do not escalate; return "worth-considering" and note
   delegation in the rationale.

--- FEW-SHOT EXAMPLES ---

Example 1 — worth-considering confirmed (integration-test feature)

REQUEST:
{
  "charm_slug": "canonical/hardware-observer-operator",
  "charm_name": "hardware-observer",
  "feature_id": "jubilant.integration-tests",
  "feature_summary": "Integration tests use jubilant rather than pytest-operator",
  "rule_rationale": "has tests/integration/ — likely uses an older harness",
  "charm_meta": {"has_containers": false, "relations_count": 2, "architecture": "delta"},
  "source_excerpts": [
    {
      "path": "tests/integration/test_charm.py",
      "content": "from pytest_operator.plugin import OpsTest\\nasync def test_build_and_deploy(ops_test: OpsTest): ..."
    }
  ]
}
RESPONSE:
{
  "verdict": "worth-considering",
  "rationale": "Tests use pytest-operator, not jubilant. No jubilant import visible.",
  "evidence_path": "tests/integration/test_charm.py"
}

Example 2 — escalated to clear-gap (secrets feature)

REQUEST:
{
  "charm_slug": "canonical/alertmanager-k8s-operator",
  "charm_name": "alertmanager-k8s",
  "feature_id": "ops.secrets",
  "feature_summary": "Charm uses the Juju Secrets API rather than plain config options",
  "rule_rationale": "2 config keys match *-password pattern — likely plain config",
  "charm_meta": {
    "has_containers": true,
    "relations_count": 4,
    "secret_like_config": ["smtp-password", "webhook-password"],
    "architecture": "delta"
  },
  "source_excerpts": [
    {
      "path": "src/charm.py",
      "content": "smtp_pass = self.config.get('smtp-password', '')\\nif not smtp_pass: return"
    }
  ]
}
RESPONSE:
{
  "verdict": "clear-gap",
  "rationale": "Config key smtp-password read from self.config; no Secret.get call visible.",
  "evidence_path": "src/charm.py"
}\
"""

# Per-feature one-sentence summaries for the LLM input record
# (AI-SCORING-PRIORITISATION §5.1–§5.5).
_FEATURE_SUMMARIES: dict[str, str] = {
    "ops.relation-app-data": (
        "Charm reads or writes relation data at app scope (relation.data[self.app] or relation.data[event.app]),"
        " allowing the remote application to receive data shared across units."
    ),
    "jubilant.integration-tests": (
        "Charm's integration tests use jubilant (the current Canonical standard for ops integration testing)"
        " rather than pytest-operator."
    ),
    "ops.secrets": (
        "Charm uses the Juju Secrets API (model.get_secret / unit.add_secret / Secret.get_content)"
        " to handle credentials rather than storing them in plain-text config options or relation databags."
    ),
    "pebble.checks": (
        "Charm defines pebble health checks in its workload layer (pebble.CheckDict or a checks: section"
        " in a layer YAML), allowing Juju to detect when the workload is unhealthy."
    ),
    "ops.pebble-custom-notice": (
        "Charm observes pebble custom notice events (PebbleCustomNoticeEvent) to react to asynchronous"
        " signals from its workload process (e.g., certificate rotation, config reload, health transitions)."
    ),
}


# ── Public types ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LLMVerdict:
    charm_slug: str
    feature_id: str
    verdict: str  # "worth-considering" | "clear-gap"
    rationale: str
    evidence_path: str | None  # validated: must be in input source_excerpts or None
    from_cache: bool


class LLMClient(Protocol):
    def complete(self, prompt: str, system: str, max_tokens: int) -> str: ...
    def budget_exhausted(self) -> bool: ...


# ── OpenRouter HTTP client ────────────────────────────────────────────────────


class OpenRouterClient:
    """Minimal HTTP client for the OpenRouter API (OpenAI-compatible format)."""

    def __init__(
        self,
        api_key: str | None = None,
        budget_limit_usd: float = _BUDGET_HARD_LIMIT_USD,
        model: str = _OPENROUTER_MODEL,
    ) -> None:
        self._api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self._budget_limit = budget_limit_usd
        self._model = model
        self._spend = 0.0

    def complete(self, prompt: str, system: str, max_tokens: int) -> str:
        if not self._api_key:
            raise RuntimeError("OPENROUTER_API_KEY is not set")

        payload = json.dumps({
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": max_tokens,
        }).encode()

        req = urllib.request.Request(
            _OPENROUTER_API_URL,
            data=payload,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"OpenRouter HTTP error {exc.code}: {exc.reason}") from exc
        except (urllib.error.URLError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"OpenRouter API error: {exc}") from exc

        usage = data.get("usage", {})
        input_tokens = int(usage.get("prompt_tokens", 950))
        output_tokens = int(usage.get("completion_tokens", 80))
        self._spend += (input_tokens * _INPUT_COST_PER_TOKEN) + (output_tokens * _OUTPUT_COST_PER_TOKEN)

        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError("OpenRouter returned no choices")
        return choices[0]["message"]["content"]

    def budget_exhausted(self) -> bool:
        return self._spend >= self._budget_limit

    @property
    def spend_usd(self) -> float:
        return self._spend


# ── Cache helpers ─────────────────────────────────────────────────────────────


def _cache_key(charm_url: str, charm_sha: str, feature_id: str, scanner_version: str) -> str:
    """Stable SHA-256 cache key for a (charm, feature) pair (AI-DESIGN.md §7.1)."""
    raw = f"{charm_url}@{charm_sha}:{feature_id}:{scanner_version}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _load_cache_entry(cache_dir: Path, key: str) -> dict | None:
    path = cache_dir / f"{key}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _save_cache_entry(cache_dir: Path, key: str, entry: dict) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f"{key}.json").write_text(json.dumps(entry, indent=2), encoding="utf-8")


def prune_cache(cache_dir: Path, ttl_days: int = _CACHE_TTL_DAYS) -> int:
    """Remove cache entries older than *ttl_days* (AI-DESIGN.md §7.3).

    Returns the count of entries removed. Entries with unparsable timestamps
    are left intact.
    """
    if not cache_dir.is_dir():
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=ttl_days)
    removed = 0
    for p in cache_dir.glob("*.json"):
        if p.name == _DISAGREEMENT_LOG_NAME:
            continue
        try:
            entry = json.loads(p.read_text(encoding="utf-8"))
            ts_str = entry.get("timestamp", "")
            if not ts_str:
                continue
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if ts < cutoff:
                p.unlink()
                removed += 1
        except (json.JSONDecodeError, OSError, ValueError):
            continue
    return removed


# ── Validation (AI-DESIGN.md §2.3 + §3) ─────────────────────────────────────


def _validate_output(raw: str, allowed_paths: set[str]) -> dict | None:
    """Parse and validate the LLM's JSON output.

    Returns the validated dict on success, or None on any validation failure.
    The caller inspects ``None`` to determine the failure mode (parse vs path).
    Hard rule: ``evidence_path`` may only cite paths from ``allowed_paths``.
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    verdict = data.get("verdict")
    if verdict not in ("worth-considering", "clear-gap"):
        return None
    rationale = data.get("rationale", "")
    if not isinstance(rationale, str) or not rationale or len(rationale) > 200:
        return None
    evidence_path = data.get("evidence_path")
    if evidence_path is not None and (not isinstance(evidence_path, str) or evidence_path not in allowed_paths):
        return None
    return data


# ── Input-record builders ─────────────────────────────────────────────────────


def _build_charm_meta(meta_raw: dict) -> dict:
    """Extract the charm_meta subset the LLM needs (AI-DESIGN.md §2.1)."""
    relations = meta_raw.get("relations", [])
    non_peer = [r for r in relations if r.get("role") != "peers"]
    archs = meta_raw.get("architecture") or []
    return {
        "has_containers": meta_raw.get("has_containers", False),
        "relations_count": len(non_peer),
        "secret_like_config": list(meta_raw.get("secret_like_config") or []),
        "architecture": archs[0] if archs else "delta",
    }


def _get_source_excerpts(
    charm_slug: str,
    subpath: str,
    feature_id: str,
    features_dict: dict,
    workdir: Path | None,
) -> list[dict]:
    """Return source excerpt dicts for the evidence lines in *features_dict*.

    Reads ~20 lines around each evidence hit from the on-disk clone.
    Returns an empty list when *workdir* is None or files cannot be read.
    """
    if workdir is None:
        return []

    if subpath:
        repo_slug = charm_slug.removesuffix("/" + subpath)
        charm_root = workdir / repo_slug / subpath
    else:
        charm_root = workdir / charm_slug

    if not charm_root.is_dir():
        return []

    rec = features_dict.get(feature_id, {})
    evidence = rec.get("evidence") or []

    seen: set[str] = set()
    excerpts: list[dict] = []
    for ev in evidence[:5]:
        file_path = ev.get("file", "")
        line_num = int(ev.get("line", 1))
        if not file_path or file_path in seen:
            continue
        seen.add(file_path)
        full_path = charm_root / file_path
        try:
            lines = full_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        start = max(0, line_num - 11)
        end = min(len(lines), line_num + 10)
        content = "\n".join(lines[start:end])
        excerpts.append({"path": file_path, "content": content})
    return excerpts


def _build_input_record(
    charm_slug: str,
    charm_name: str,
    repo_url: str,
    feature_id: str,
    rule_rationale: str,
    charm_meta: dict,
    source_excerpts: list[dict],
) -> dict:
    return {
        "charm_slug": charm_slug,
        "charm_name": charm_name,
        "repo_url": repo_url,
        "feature_id": feature_id,
        "feature_summary": _FEATURE_SUMMARIES.get(feature_id, ""),
        "rule_rationale": rule_rationale,
        "charm_meta": charm_meta,
        "source_excerpts": source_excerpts,
    }


# ── Disagreement log (AI-DESIGN.md §8) ───────────────────────────────────────


def _write_disagreement_log(cache_dir: Path, entry: dict) -> None:
    """Append one JSON-lines entry to the disagreement log."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    log_path = cache_dir / _DISAGREEMENT_LOG_NAME
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def _detect_parse_failure_kind(raw: str, allowed_paths: set[str]) -> str:
    """Classify a validation failure as parse-error or invalid-path."""
    try:
        parsed = json.loads(raw)
        ev_path = parsed.get("evidence_path") if isinstance(parsed, dict) else None
        if isinstance(ev_path, str) and ev_path and ev_path not in allowed_paths:
            return "invalid-path"
    except (json.JSONDecodeError, ValueError):
        pass
    return "parse-error"


# ── Main scoring pass (AI-DESIGN.md §10.2) ───────────────────────────────────


def score_worth_considering(
    scored: dict,
    client: LLMClient,
    cache_dir: Path,
    max_calls: int = 200,
    scanner_version: str = "",
    workdir: Path | None = None,
) -> dict:
    """Run the LLM pass over all worth-considering records for eligible features.

    Takes a scored.json dict, runs the LLM over ``worth-considering`` records
    for the five v1-eligible features, and returns a new dict with escalated
    ``clear-gap`` verdicts where the LLM agrees. Does not mutate the input.

    Skips ``not-applicable``, ``clear-gap``, and ``present`` records entirely.
    Prunes expired cache entries before processing.
    Stops issuing LLM calls when *max_calls* is reached or
    ``client.budget_exhausted()`` returns True; remaining records keep
    ``worth-considering``.
    """
    result = copy.deepcopy(scored)
    prune_cache(cache_dir)

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    calls_made = 0
    hits = 0
    escalated = 0

    for slug, charm_data in result.items():
        if slug.startswith("__") or not isinstance(charm_data, dict):
            continue

        features_dict = charm_data.get("features", {})
        meta_raw = features_dict.get("__meta__", {})
        charm_meta = _build_charm_meta(meta_raw)
        charm_name = charm_data.get("name", slug)
        repo_url = charm_data.get("repo_url", "")
        subpath = charm_data.get("subpath", "")
        charm_sha = meta_raw.get("repo_sha", "unknown")
        architecture = list(meta_raw.get("architecture") or [])

        for feature_id in LLM_ELIGIBLE_FEATURES:
            rec = features_dict.get(feature_id)
            if rec is None:
                continue
            if rec.get("present") or rec.get("score") != "worth-considering":
                continue

            # Shim-charm rule for ops.secrets (AI-SCORING-PRIORITISATION §5.3):
            # shim charms may delegate secret handling to an external base class
            # that the LLM cannot see from the excerpts; skip to avoid false gaps.
            if feature_id == "ops.secrets" and "shim" in architecture:
                continue

            # Check call cap and budget before any work.
            if calls_made >= max_calls or client.budget_exhausted():
                _write_disagreement_log(
                    cache_dir,
                    {
                        "charm_slug": slug,
                        "feature_id": feature_id,
                        "scanner_verdict": "worth-considering",
                        "llm_verdict": None,
                        "outcome": "budget-stop",
                        "timestamp": now_str,
                    },
                )
                continue

            # Cache lookup.
            key = _cache_key(repo_url, charm_sha, feature_id, scanner_version)
            cached = _load_cache_entry(cache_dir, key)
            if cached is not None:
                verdict = cached.get("verdict", "worth-considering")
                if verdict == "clear-gap":
                    rec["score"] = "clear-gap"
                    rec["rationale"] = cached.get("rationale", rec.get("rationale", ""))
                    rec["ai_escalated"] = True
                    escalated += 1
                hits += 1
                continue

            # Build input and call LLM.
            source_excerpts = _get_source_excerpts(slug, subpath, feature_id, features_dict, workdir)
            rule_rationale = rec.get("rationale", "")
            input_record = _build_input_record(
                slug,
                charm_name,
                repo_url,
                feature_id,
                rule_rationale,
                charm_meta,
                source_excerpts,
            )
            user_msg = f"--- LIVE REQUEST ---\n{json.dumps(input_record, indent=2)}"
            allowed_paths = {e["path"] for e in source_excerpts}

            try:
                raw_output = client.complete(user_msg, _SYSTEM_PROMPT, max_tokens=200)
            except RuntimeError as exc:
                print(f"  LLM error for {slug}/{feature_id}: {exc}", file=sys.stderr)
                raw_output = ""
            calls_made += 1

            validated = _validate_output(raw_output, allowed_paths)
            if validated is None:
                outcome = _detect_parse_failure_kind(raw_output, allowed_paths)
                _write_disagreement_log(
                    cache_dir,
                    {
                        "charm_slug": slug,
                        "feature_id": feature_id,
                        "scanner_verdict": "worth-considering",
                        "llm_verdict": None,
                        "outcome": outcome,
                        "raw_output": raw_output[:500],
                        "timestamp": now_str,
                    },
                )
                continue

            llm_verdict = validated["verdict"]
            llm_rationale = validated["rationale"]
            llm_evidence_path = validated.get("evidence_path")

            # Persist to cache.
            _save_cache_entry(
                cache_dir,
                key,
                {
                    "key": key,
                    "charm_slug": slug,
                    "feature_id": feature_id,
                    "verdict": llm_verdict,
                    "rationale": llm_rationale,
                    "evidence_path": llm_evidence_path,
                    "model": _OPENROUTER_MODEL,
                    "timestamp": now_str,
                    "ttl_days": _CACHE_TTL_DAYS,
                },
            )

            if llm_verdict == "clear-gap":
                rec["score"] = "clear-gap"
                rec["rationale"] = llm_rationale
                rec["ai_escalated"] = True
                escalated += 1
                _write_disagreement_log(
                    cache_dir,
                    {
                        "charm_slug": slug,
                        "feature_id": feature_id,
                        "scanner_verdict": "worth-considering",
                        "llm_verdict": "clear-gap",
                        "llm_rationale": llm_rationale,
                        "evidence_path": llm_evidence_path,
                        "timestamp": now_str,
                        "outcome": "escalated",
                    },
                )
            # "worth-considering" — no change; confirmed agreement not logged.

    print(
        f"LLM pass complete: {calls_made} calls, {hits} cache hits, {escalated} escalated",
        file=sys.stderr,
    )
    return result


# ── Pre-flight calibration (AI-SCORING-PRIORITISATION §6.1) ──────────────────


def run_preflight_calibration(
    scored: dict,
    client: LLMClient,
    cache_dir: Path,
    ground_truth: list[dict],
    max_calls: int = 200,
    scanner_version: str = "",
    workdir: Path | None = None,
) -> dict:
    """Run the LLM pass and compare verdicts against human ground-truth labels.

    *ground_truth* is a list of dicts with keys:
      ``charm_slug``, ``feature_id``, ``human_verdict``.

    Pass criterion: ≥90% agreement between LLM and human verdicts
    (AI-SCORING-PRIORITISATION §6.1). Returns::

        {"agreement": float, "total": int, "agreed": int, "passed": bool}
    """
    llm_result = score_worth_considering(
        scored,
        client,
        cache_dir,
        max_calls=max_calls,
        scanner_version=scanner_version,
        workdir=workdir,
    )

    agreed = 0
    evaluated = 0
    for gt in ground_truth:
        slug = gt.get("charm_slug", "")
        feature_id = gt.get("feature_id", "")
        human_verdict = gt.get("human_verdict", "")
        charm_data = llm_result.get(slug, {})
        rec = charm_data.get("features", {}).get(feature_id, {})
        llm_verdict = rec.get("score", "worth-considering")
        if llm_verdict == human_verdict:
            agreed += 1
        evaluated += 1

    pct = agreed / evaluated if evaluated > 0 else 1.0
    return {
        "agreement": pct,
        "total": evaluated,
        "agreed": agreed,
        "passed": pct >= 0.9,
    }


# ── CLI helpers ───────────────────────────────────────────────────────────────


def count_worth_considering(scored: dict) -> dict[str, int]:
    """Count worth-considering records per eligible feature (for --dry-run)."""
    counts: dict[str, int] = {f: 0 for f in LLM_ELIGIBLE_FEATURES}
    for slug, charm_data in scored.items():
        if slug.startswith("__") or not isinstance(charm_data, dict):
            continue
        features_dict = charm_data.get("features", {})
        for feature_id in LLM_ELIGIBLE_FEATURES:
            rec = features_dict.get(feature_id)
            if rec and not rec.get("present") and rec.get("score") == "worth-considering":
                counts[feature_id] += 1
    return counts
