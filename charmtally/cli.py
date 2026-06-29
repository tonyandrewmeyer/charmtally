"""Command-line entry point.

The corpus CSV is pulled from canonical/hyrum on each run by default
(``--corpus-url``, see ``corpus.HYRUM_CHARMS_CSV_URL``). Override with
``--corpus <local.csv>`` for offline / pinned runs.

Usage:
    charmtally local <charm-dir> [--features features.yaml]
        Scan a single already-checked-out charm directory.

    charmtally spike --workdir /tmp/charms \\
                             [--limit 5] [--only ops.collect-status,...]
        Clone (or reuse) a handful of charms from the corpus and scan them.
        Calibration tool: limited set, output to stdout.

    charmtally scan --workdir /tmp/charms \\
                            [--team charm-tech] [--key-only] --out results.json
        Full corpus scan: clones charms, detects + scores features, writes
        results.json. Skipped slugs (clone failure / archived) recorded in
        results["__skipped__"].

    charmtally score results.json [--out scored.json]
        Re-apply rule-based scoring over an existing results.json.
        Useful for tweaking scoring rules without re-cloning.

    charmtally dashboard results.json [--out dashboard.html]
        Render results.json → dashboard.html (two sortable tables).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from . import catalogue, corpus, dashboard, scan
from . import llm_score as _llm_score
from . import metadata as _metadata
from . import pairs as _pairs
from . import scoring as _scoring

DEFAULT_CATALOGUE = Path(__file__).resolve().parent.parent / "features.yaml"


def _apply_feature_excludes(
    features_dict: dict,
    overrides: corpus.CorpusOverrides,
    repo_url: str,
    sub_path: str,
) -> None:
    """Force specific (charm, feature) pairs to not-applicable per overrides.

    Used to silence shim-charm FPs etc. where the detector can't see through
    the abstraction. Mutates ``features_dict`` in place.
    """
    for feat_name, rec in features_dict.items():
        if feat_name.startswith("__") or not isinstance(rec, dict):
            continue
        reason = overrides.feature_skip_reason(repo_url, sub_path, feat_name)
        if reason:
            rec["score"] = "not-applicable"
            rec["rationale"] = reason


def _filter(features, names):
    if not names:
        return features
    wanted = set(names)
    return [f for f in features if f.name in wanted]


def _resolve_corpus_path(args: argparse.Namespace) -> Path:
    """Return the CSV path to load — local if --corpus, else fetch --corpus-url.

    Pulls into ``<workdir>/corpus.csv``; the fetch is idempotent (just
    re-downloads). Caller pins ``--corpus`` to override (offline runs).
    """
    if args.corpus is not None:
        return args.corpus
    dest = args.workdir / "corpus.csv"
    print(f"… fetching corpus from {args.corpus_url}", file=sys.stderr)
    corpus.fetch_to(args.corpus_url, dest)
    return dest


def cmd_local(args: argparse.Namespace) -> int:
    feats = _filter(catalogue.load(args.features), args.only)
    result = scan.scan_charm(args.charm_dir, feats)
    json.dump({args.charm_dir.name: result}, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def cmd_spike(args: argparse.Namespace) -> int:
    feats = _filter(catalogue.load(args.features), args.only)
    refs = corpus.load(_resolve_corpus_path(args))
    if args.key_only:
        refs = [r for r in refs if r.key_charm]
    if args.limit:
        refs = refs[: args.limit]

    results: dict[str, dict] = {}
    for ref in refs:
        print(f"… {ref.name} ({ref.repo_url})", file=sys.stderr)
        path = scan.ensure_clone(ref, args.workdir)
        if path is None:
            print("  clone failed; skipping", file=sys.stderr)
            continue
        results[ref.slug] = {
            "name": ref.name,
            "team": ref.team,
            "repo_url": ref.repo_url,
            "features": scan.scan_charm(path, feats),
        }

    json.dump(results, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    """Full corpus scan — charm-tech slice + key_charm rows (or any --team filter).

    Monorepos fan out: one result per sub-charm, keyed ``<repo-slug>/<dir>``.
    Overrides (exclusions + branch swaps) loaded from
    ``--overrides corpus-overrides.yaml`` and applied per-row before clone.
    Skipped rows recorded in ``results["__skipped__"]`` as ``{slug: reason}``.
    """
    feats = _filter(catalogue.load(args.features), args.only)
    pats = catalogue.load_patterns(args.features)
    refs = corpus.load(_resolve_corpus_path(args))
    overrides = corpus.load_overrides(args.overrides) if args.overrides else corpus.CorpusOverrides.empty()

    # Filter: union of team match and key_charm flag.
    # --team X --key-only → include rows in team X OR rows with key_charm=True.
    teams = {t.lower() for t in args.team} if args.team else set()
    filtered: list[corpus.CharmRef] = []
    for r in refs:
        in_team = bool(teams) and r.team.lower() in teams
        is_key = args.key_only and r.key_charm
        if not teams and not args.key_only:
            filtered.append(r)  # no filter at all
        elif in_team or is_key:
            filtered.append(r)
    # Remove duplicates (same repo can appear multiple times).
    seen_urls: set[str] = set()
    unique_refs: list[corpus.CharmRef] = []
    for r in filtered:
        if r.repo_url not in seen_urls:
            seen_urls.add(r.repo_url)
            unique_refs.append(r)

    results: dict[str, object] = {}
    skipped: dict[str, str] = {}
    for ref in unique_refs:
        adjusted, exclude_reason = overrides.apply(ref)
        if adjusted is None:
            print(f"… {ref.name} ({ref.repo_url}) — excluded: {exclude_reason}", file=sys.stderr)
            skipped[ref.slug] = exclude_reason
            continue
        ref = adjusted

        print(
            f"… {ref.name} ({ref.repo_url})" + (f" [branch={ref.branch}]" if ref.branch else ""),
            file=sys.stderr,
        )
        path = scan.ensure_clone(ref, args.workdir)
        if path is None:
            print("  clone failed; skipping", file=sys.stderr)
            skipped[ref.slug] = "clone failed"
            continue

        charm_roots = scan.find_charm_roots(path)
        if not charm_roots:
            print("  no charm files found; skipping", file=sys.stderr)
            skipped[ref.slug] = "no charmcraft.yaml or metadata.yaml found"
            continue

        if len(charm_roots) == 1 and charm_roots[0] == path:
            charm_features = scan.scan_charm(path, feats, pats)
            _apply_feature_excludes(charm_features, overrides, ref.repo_url, "")
            results[ref.slug] = {
                "name": ref.name,
                "team": ref.team,
                "repo_url": ref.repo_url,
                "features": charm_features,
            }
        else:
            # Monorepo fan-out: one entry per sub-charm.
            print(f"  monorepo: {len(charm_roots)} sub-charms", file=sys.stderr)
            for sub in charm_roots:
                rel = sub.relative_to(path)
                sub_slug = f"{ref.slug}/{rel}"
                skip_sub = overrides.sub_charm_skip_reason(ref.repo_url, str(rel))
                if skip_sub:
                    print(f"  excluding sub-charm {rel}: {skip_sub}", file=sys.stderr)
                    skipped[sub_slug] = skip_sub
                    continue
                charm_features = scan.scan_charm(sub, feats, pats)
                _apply_feature_excludes(charm_features, overrides, ref.repo_url, str(rel))
                results[sub_slug] = {
                    "name": f"{ref.name}/{rel}",
                    "team": ref.team,
                    "repo_url": ref.repo_url,
                    "subpath": str(rel),
                    "features": charm_features,
                }

    if skipped:
        results["__skipped__"] = skipped

    text = json.dumps(results, indent=2) + "\n"
    args.out.write_text(text)
    scanned = sum(1 for k in results if not k.startswith("__"))
    print(f"wrote {args.out} ({scanned} records, {len(skipped)} skipped)", file=sys.stderr)
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    """Re-apply rule-based scoring to an existing results.json → scored.json."""
    feats = catalogue.load(args.features)
    overrides = corpus.load_overrides(args.overrides) if args.overrides else corpus.CorpusOverrides.empty()
    results: dict = json.loads(args.results.read_text())

    for slug, charm_data in results.items():
        if slug.startswith("__"):
            continue
        features_dict = charm_data.get("features", {})
        meta_raw = features_dict.get("__meta__", {})
        meta = _metadata.CharmMeta(
            has_containers=meta_raw.get("has_containers", False),
            relations=tuple(
                _metadata.Relation(r["name"], r["role"], r.get("interface", "")) for r in meta_raw.get("relations", [])
            ),
            config_keys=tuple(meta_raw.get("config_keys", [])),
            secret_like_config=tuple(meta_raw.get("secret_like_config", [])),
            secret_typed_config=tuple(meta_raw.get("secret_typed_config", [])),
            has_integration_tests=meta_raw.get("has_integration_tests", False),
            is_reactive=meta_raw.get("is_reactive", False),
            is_legacy_classic=meta_raw.get("is_legacy_classic", False),
            is_subordinate=meta_raw.get("is_subordinate", False),
            is_workload_less=meta_raw.get("is_workload_less", False),
            charm_name=meta_raw.get("charm_name"),
            charmcraft_plugins=tuple(meta_raw.get("charmcraft_plugins", [])),
            bases=tuple(meta_raw.get("bases", [])),
            min_juju_version=meta_raw.get("min_juju_version"),
            library_count=int(meta_raw.get("library_count", 0)),
            library_names=tuple(meta_raw.get("library_names", [])),
            provides_own_library=bool(meta_raw.get("provides_own_library", False)),
            has_terraform_module=bool(meta_raw.get("has_terraform_module", False)),
            tooling=tuple(meta_raw.get("tooling", [])),
        )
        architecture = list(meta_raw.get("architecture") or [])
        for feat in feats:
            if feat.name not in features_dict:
                continue
            rec = features_dict[feat.name]
            if rec.get("present"):
                note = _scoring.annotate_present(feat.name, meta)
                if note:
                    rec["score"] = note.label
                    rec["rationale"] = note.rationale
                else:
                    rec.pop("score", None)
                    rec.pop("rationale", None)
            else:
                s = _scoring.score_absent(feat.name, features_dict, meta, architecture)
                rec["score"] = s.label
                rec["rationale"] = s.rationale
        _apply_feature_excludes(
            features_dict,
            overrides,
            charm_data.get("repo_url", ""),
            charm_data.get("subpath", ""),
        )

    args.out.write_text(json.dumps(results, indent=2) + "\n")
    print(f"wrote {args.out}", file=sys.stderr)
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    feats = catalogue.load(args.features)
    results = json.loads(args.results.read_text())
    pairs_payload = None
    if args.pairs is not None and args.pairs.is_file():
        pairs_payload = json.loads(args.pairs.read_text()).get("pairs")
    html = dashboard.render(results, feats, pairs=pairs_payload)
    args.out.write_text(html)
    print(f"wrote {args.out}", file=sys.stderr)
    return 0


def cmd_llm_score(args: argparse.Namespace) -> int:
    """Run the LLM scoring pass over worth-considering records → llm-scored.json."""
    scored = json.loads(args.results.read_text())

    cache_dir = args.cache_dir
    if cache_dir is None:
        cache_dir = args.results.parent / ".llm-verdicts"

    out_path = args.out
    if out_path is None:
        out_path = args.results.parent / "llm-scored.json"

    if args.prune_cache:
        removed = _llm_score.prune_cache(cache_dir)
        print(f"pruned {removed} expired cache entries from {cache_dir}", file=sys.stderr)
        return 0

    if args.dry_run:
        counts = _llm_score.count_worth_considering(scored)
        total = sum(counts.values())
        print(f"dry-run: {total} worth-considering records eligible for LLM scoring")
        for feat, n in sorted(counts.items()):
            print(f"  {feat}: {n}")
        return 0

    import os

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        print(
            "warning: OPENROUTER_API_KEY not set; LLM calls will fail. Use --dry-run to skip or set the env var.",
            file=sys.stderr,
        )

    client = _llm_score.OpenRouterClient(api_key=api_key)
    workdir = args.workdir

    result = _llm_score.score_worth_considering(
        scored,
        client,
        cache_dir,
        max_calls=args.max_llm_calls,
        workdir=workdir,
    )
    out_path.write_text(json.dumps(result, indent=2) + "\n")
    print(f"wrote {out_path}", file=sys.stderr)
    return 0


def cmd_llm_calibrate(args: argparse.Namespace) -> int:
    """Run pre-flight calibration comparing LLM verdicts against human ground truth."""
    scored = json.loads(args.results.read_text())
    ground_truth = json.loads(args.ground_truth.read_text())

    cache_dir = args.cache_dir
    if cache_dir is None:
        cache_dir = args.results.parent / ".llm-verdicts"

    import os

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    client = _llm_score.OpenRouterClient(api_key=api_key)

    cal = _llm_score.run_preflight_calibration(
        scored,
        client,
        cache_dir,
        ground_truth,
        max_calls=args.max_llm_calls,
        workdir=args.workdir,
    )
    print(
        f"calibration: {cal['agreed']}/{cal['total']} agreed "
        f"({cal['agreement']:.0%}) — {'PASSED' if cal['passed'] else 'FAILED'}"
    )
    return 0 if cal["passed"] else 1


def cmd_pairs(args: argparse.Namespace) -> int:
    results = json.loads(args.results.read_text())
    pairs = _pairs.find_pairs(results)
    payload = {"pairs": [asdict(p) for p in pairs]}
    args.out.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"wrote {len(pairs)} pairs to {args.out}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="charmtally")
    p.add_argument(
        "--features",
        type=Path,
        default=DEFAULT_CATALOGUE,
        help="Path to features.yaml (default: alongside this package)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_local = sub.add_parser("local", help="Scan a checked-out charm dir.")
    p_local.add_argument("charm_dir", type=Path)
    p_local.add_argument("--only", nargs="+", help="Limit to these feature names.")
    p_local.set_defaults(func=cmd_local)

    p_spike = sub.add_parser("spike", help="Clone+scan a slice of the corpus.")
    p_spike.add_argument("--corpus", type=Path, default=None, help="Path to a local CSV. Default: fetch --corpus-url.")
    p_spike.add_argument(
        "--corpus-url",
        default=corpus.HYRUM_CHARMS_CSV_URL,
        help="URL of the corpus CSV (default: canonical/hyrum charm-list).",
    )
    p_spike.add_argument(
        "--workdir",
        type=Path,
        required=True,
        help="Where to clone charms (reused if already present).",
    )
    p_spike.add_argument("--limit", type=int, default=5)
    p_spike.add_argument("--key-only", action="store_true", help="Only scan rows marked 'Key Charm for this Team'.")
    p_spike.add_argument("--only", nargs="+", help="Restrict to these feature names (default: all in features.yaml).")
    p_spike.set_defaults(func=cmd_spike)

    p_scan = sub.add_parser("scan", help="Full corpus scan → results.json.")
    p_scan.add_argument("--corpus", type=Path, default=None, help="Path to a local CSV. Default: fetch --corpus-url.")
    p_scan.add_argument(
        "--corpus-url",
        default=corpus.HYRUM_CHARMS_CSV_URL,
        help="URL of the corpus CSV (default: canonical/hyrum charm-list).",
    )
    p_scan.add_argument("--workdir", type=Path, required=True, help="Where to clone/cache charms.")
    p_scan.add_argument(
        "--team",
        nargs="+",
        metavar="TEAM",
        help="Include charms from these teams (case-insensitive). Combined with --key-only via OR.",
    )
    p_scan.add_argument("--key-only", action="store_true", help="Include all key_charm=TRUE rows.")
    p_scan.add_argument("--only", nargs="+", help="Restrict to these feature names.")
    p_scan.add_argument(
        "--out",
        type=Path,
        default=Path("results.json"),
        help="Output path (default: results.json).",
    )
    p_scan.add_argument(
        "--overrides",
        type=Path,
        default=None,
        help="Path to corpus-overrides.yaml (exclusions + branch swaps). "
        "Recommended: --overrides ./corpus-overrides.yaml.",
    )
    p_scan.set_defaults(func=cmd_scan)

    p_score = sub.add_parser("score", help="Re-apply scoring to results.json → scored.json.")
    p_score.add_argument("results", type=Path, help="Path to results.json.")
    p_score.add_argument("--out", type=Path, default=Path("scored.json"))
    p_score.add_argument(
        "--overrides",
        type=Path,
        default=None,
        help="Path to corpus-overrides.yaml; applies the same feature_excludes the scan command would apply.",
    )
    p_score.set_defaults(func=cmd_score)

    p_dash = sub.add_parser("dashboard", help="Render results.json/scored.json → dashboard.html.")
    p_dash.add_argument("results", type=Path, help="Path to results/scored JSON.")
    p_dash.add_argument("--out", type=Path, default=Path("dashboard.html"))
    p_dash.add_argument(
        "--pairs",
        type=Path,
        default=None,
        help="Optional pairs.json (from `charmtally pairs`) to render the Pairs view.",
    )
    p_dash.set_defaults(func=cmd_dashboard)

    p_llm_score = sub.add_parser("llm-score", help="LLM scoring pass over worth-considering records → llm-scored.json.")
    p_llm_score.add_argument("results", type=Path, help="Path to scored.json.")
    p_llm_score.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output path (default: llm-scored.json next to RESULTS).",
    )
    p_llm_score.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        dest="cache_dir",
        help="Cache directory for LLM verdicts (default: .llm-verdicts/ next to RESULTS).",
    )
    p_llm_score.add_argument(
        "--workdir",
        type=Path,
        default=None,
        help="Charm clone workdir (enables source-excerpt reading; default: none).",
    )
    p_llm_score.add_argument(
        "--max-llm-calls",
        type=int,
        default=200,
        dest="max_llm_calls",
        help="Hard cap on LLM calls per run (default: 200).",
    )
    p_llm_score.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Print eligible worth-considering counts; exit without calling LLM.",
    )
    p_llm_score.add_argument(
        "--prune-cache",
        action="store_true",
        dest="prune_cache",
        help="Prune expired cache entries and exit.",
    )
    p_llm_score.set_defaults(func=cmd_llm_score)

    p_llm_cal = sub.add_parser("llm-calibrate", help="Pre-flight calibration: compare LLM to human ground truth.")
    p_llm_cal.add_argument("results", type=Path, help="Path to scored.json.")
    p_llm_cal.add_argument(
        "ground_truth",
        type=Path,
        help="JSON file with [{charm_slug, feature_id, human_verdict}, …].",
    )
    p_llm_cal.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        dest="cache_dir",
        help="Cache directory for LLM verdicts.",
    )
    p_llm_cal.add_argument(
        "--workdir",
        type=Path,
        default=None,
        help="Charm clone workdir (enables source-excerpt reading).",
    )
    p_llm_cal.add_argument(
        "--max-llm-calls",
        type=int,
        default=200,
        dest="max_llm_calls",
        help="Hard cap on LLM calls (default: 200).",
    )
    p_llm_cal.set_defaults(func=cmd_llm_calibrate)

    p_pairs = sub.add_parser("pairs", help="Detect k8s/machine charm pairs → pairs.json.")
    p_pairs.add_argument("results", type=Path, help="Path to results.json (or scored.json).")
    p_pairs.add_argument("--out", type=Path, default=Path("pairs.json"))
    p_pairs.set_defaults(func=cmd_pairs)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
