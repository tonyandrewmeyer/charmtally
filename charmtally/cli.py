r"""Command-line entry point.

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

    charmtally pairs scored.json [--out pairs.json]
        Detect k8s/machine charm pairs; feeds the dashboard's Pairs view.

    charmtally dashboard results.json [--pairs pairs.json]
                                      [--out dashboard.html]
        Render results.json → dashboard.html (sortable tables + Pairs view).

    charmtally trend [--snapshots-dir snapshots] [--live scored.json]
                     [--since DATE] [--feature F] [--out trend.html]
        Adoption trend, per-charm timeline and diff list across the dated
        snapshots → the standalone History page.

    charmtally adoption [--snapshots-dir snapshots] [--live scored.json]
                        [--since DATE] [--metric KEY] [--out adoption.html]
        Charm-tech adoption scorecard: a handful of headline metrics
        (typed relation data, jubilant, charmlibs share, ...) over the same
        dated snapshots the trend page reads → the standalone Adoption page.

    charmtally scan-rocks [--rocks rocks.csv] [--into scored.json]
                          [--out rocks.json]
        Fetch each rock's rockcraft.yaml from raw.githubusercontent and record
        its facts. `--into` parks them in scored.json's `__rocks__` block so
        the weekly snapshot carries them; `--out` writes them standalone.

    charmtally snapshot scored.json [--snapshots-dir snapshots] [--date DATE]
                                    [--out PATH]
        Write the dated snapshot the trend and adoption pages read, thinned
        to the readings (evidence and rationale are dropped — see
        charmtally/snapshot.py).

    charmtally llm-score scored.json [--dry-run] [--out llm-scored.json]
        Optional LLM pass over `worth-considering` records. Needs
        OPENROUTER_API_KEY; capped by --max-llm-calls and a spend budget.

    charmtally llm-calibrate scored.json ground-truth.json
        Compare LLM verdicts against human labels; exits non-zero if
        agreement falls below the threshold.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from dataclasses import asdict
from pathlib import Path

from . import __version__, adoption, catalogue, corpus, dashboard, scan
from . import llm_score as _llm_score
from . import metadata as _metadata
from . import pairs as _pairs
from . import rocks as _rocks
from . import scoring as _scoring
from . import snapshot as _snapshot
from . import trend as _trend

DEFAULT_CATALOGUE = catalogue.default_path()


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
    """Scan a single local charm directory and print the result as JSON."""
    feats = _filter(catalogue.load(args.features), args.only)
    pats = catalogue.load_patterns(args.features)
    result = scan.scan_charm(args.charm_dir, feats, pats)
    json.dump({args.charm_dir.name: result}, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def cmd_spike(args: argparse.Namespace) -> int:
    """Scan the whole corpus and write the combined scan results."""
    feats = _filter(catalogue.load(args.features), args.only)
    pats = catalogue.load_patterns(args.features)
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
            "features": scan.scan_charm(path, feats, pats),
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
    overrides = (
        corpus.load_overrides(args.overrides) if args.overrides else corpus.CorpusOverrides.empty()
    )

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
            skipped[ref.slug] = exclude_reason or "excluded by corpus-overrides.yaml"
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
    overrides = (
        corpus.load_overrides(args.overrides) if args.overrides else corpus.CorpusOverrides.empty()
    )
    results: dict = json.loads(args.results.read_text())

    for slug, charm_data in results.items():
        if slug.startswith("__"):
            continue
        features_dict = charm_data.get("features", {})
        meta_raw = features_dict.get("__meta__", {})
        meta = _metadata.CharmMeta.from_dict(meta_raw)
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


def cmd_scan_rocks(args: argparse.Namespace) -> int:
    """Scan the rocks corpus → `__rocks__` in scored.json (and/or a standalone file).

    Writing into scored.json is the path the weekly workflow takes: the rocks
    half then rides along into the dated snapshot, and the adoption scorecard
    gets history for the rootless metric without a second snapshot series to
    keep in step.
    """
    refs = _rocks.load_csv(args.rocks)
    if not refs:
        print(f"no scannable rows in {args.rocks}", file=sys.stderr)
        return 1
    print(f"… fetching rockcraft.yaml for {len(refs)} rocks", file=sys.stderr)
    records = _rocks.scan_rocks(refs, workers=args.workers)
    readable = sum(1 for r in records.values() if r.get("readable"))
    print(f"read {readable} of {len(records)} rockcraft.yaml files", file=sys.stderr)

    block = {"scanned": len(records), "readable": readable, "rocks": records}
    if args.out is not None:
        args.out.write_text(json.dumps(block, indent=2) + "\n")
        print(f"wrote {args.out}", file=sys.stderr)
    if args.into is not None:
        scored: dict = json.loads(args.into.read_text())
        scored[_trend.ROCKS_KEY] = block
        args.into.write_text(json.dumps(scored, indent=2) + "\n")
        print(f"wrote {_trend.ROCKS_KEY} into {args.into}", file=sys.stderr)
    if args.out is None and args.into is None:
        print("nothing written: pass --out and/or --into", file=sys.stderr)
        return 2
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    """Write a dated, thinned snapshot from scored.json.

    The dated snapshots are the entire history and cannot be regenerated, so
    this writes only the readings, not the evidence behind them; see
    `charmtally/snapshot.py` for what is dropped and why.
    """
    scored: dict = json.loads(args.scored.read_text())
    out = args.out
    if out is None:
        date = args.date or dt.datetime.now(dt.timezone.utc).date().isoformat()
        args.snapshots_dir.mkdir(parents=True, exist_ok=True)
        out = args.snapshots_dir / f"scored-{date}.json"
    thinned = _snapshot.thin(scored)
    out.write_text(json.dumps(thinned, indent=2) + "\n")
    records = sum(1 for k in thinned if not k.startswith("__"))
    print(f"wrote {out} ({records} records, thinned)", file=sys.stderr)
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    """Render the HTML dashboard from a scan results file."""
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
            "warning: OPENROUTER_API_KEY not set; LLM calls will fail. Use --dry-run to skip or "
            "set the env var.",
            file=sys.stderr,
        )

    client = _llm_score.OpenRouterClient(api_key=api_key)
    workdir = args.workdir

    result = _llm_score.score_worth_considering(
        scored,
        client,
        cache_dir,
        max_calls=args.max_llm_calls,
        scanner_version=__version__,
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
        scanner_version=__version__,
        workdir=args.workdir,
    )
    print(
        f"calibration: {cal['agreed']}/{cal['total']} agreed "
        f"({cal['agreement']:.0%}) — {'PASSED' if cal['passed'] else 'FAILED'}"
    )
    return 0 if cal["passed"] else 1


def cmd_pairs(args: argparse.Namespace) -> int:
    """Find K8s/machine charm pairs in the results and write them as JSON."""
    results = json.loads(args.results.read_text())
    pairs = _pairs.find_pairs(results)
    payload = {"pairs": [asdict(p) for p in pairs]}
    args.out.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"wrote {len(pairs)} pairs to {args.out}", file=sys.stderr)
    return 0


def cmd_trend(args: argparse.Namespace) -> int:
    """Adoption trend, per-charm timeline, and diff list across CI snapshots.

    Reads `snapshots/scored-*.json` (plus the live scored.json, unless a
    snapshot for today already exists) and writes a standalone History page.
    See trend.py for the corpus-drift / feature-drift / rename guards.
    """
    snapshots = _trend.load_snapshots(args.snapshots_dir, args.live)
    if not snapshots:
        print(f"no snapshots found under {args.snapshots_dir}", file=sys.stderr)
        return 1

    ranged = _trend.select_range(snapshots, args.since)
    if not ranged:
        print(f"no snapshots on/after --since {args.since}", file=sys.stderr)
        return 1

    base = ranged[0]
    latest = snapshots[-1]
    diff = _trend.compute_diff(base, latest)
    if args.feature:
        diff["flips"] = [f for f in diff["flips"] if f["feature"] == args.feature]

    adoption = _trend.compute_adoption(ranged, feature=args.feature)
    timeline = _trend.compute_timeline(ranged, feature=args.feature)

    # The timeline matrix rides in its own document next to the page, fetched
    # on demand — inline it was tens of MB and grew with every snapshot. The
    # name is derived from --out so the two travel together, and the URL is
    # relative because both are published to Pages from the same directory.
    timeline_path = args.out.with_suffix(".timeline.json")
    timeline_path.write_text(json.dumps(_trend.encode_timeline(timeline)) + "\n")
    print(f"wrote {timeline_path}", file=sys.stderr)

    html = dashboard.render_trend(
        diff,
        adoption,
        timeline,
        feature_filter=args.feature,
        timeline_url=timeline_path.name,
    )
    args.out.write_text(html)
    print(f"wrote {args.out}", file=sys.stderr)

    if args.emit_json:
        json_out = args.out.with_suffix(".json")
        payload = {"diff": diff, "adoption": adoption, "timeline": timeline}
        json_out.write_text(json.dumps(payload, indent=2) + "\n")
        print(f"wrote {json_out}", file=sys.stderr)

    return 0


def cmd_adoption(args: argparse.Namespace) -> int:
    """Charm-tech adoption scorecard across CI snapshots → adoption.html.

    Same inputs as `trend`, a much narrower question: are the few things
    charm tech ships actually being adopted? See adoption.py for the metric
    definitions and the eligibility / feature-drift guards.
    """
    snapshots = _trend.load_snapshots(args.snapshots_dir, args.live)
    if not snapshots:
        print(f"no snapshots found under {args.snapshots_dir}", file=sys.stderr)
        return 1

    ranged = _trend.select_range(snapshots, args.since)
    if not ranged:
        print(f"no snapshots on/after --since {args.since}", file=sys.stderr)
        return 1

    if args.metric and adoption.metric_by_key(args.metric) is None:
        known = ", ".join(m.key for m in adoption.METRICS)
        print(f"unknown metric {args.metric!r}; known metrics: {known}", file=sys.stderr)
        return 2

    metrics = [m for m in adoption.METRICS if not args.metric or m.key == args.metric]
    series = adoption.compute_series(ranged, only=args.metric)

    html = dashboard.render_adoption(metrics, series)
    args.out.write_text(html)
    print(f"wrote {args.out}", file=sys.stderr)

    if args.emit_json:
        json_out = args.out.with_suffix(".json")
        payload = {
            "metrics": [
                {
                    "key": m.key,
                    "title": m.title,
                    "question": m.question,
                    "unit": m.unit,
                    "scope": m.scope,
                    "pending": m.pending,
                    "series": series.get(m.key, []),
                }
                for m in metrics
            ]
        }
        json_out.write_text(json.dumps(payload, indent=2) + "\n")
        print(f"wrote {json_out}", file=sys.stderr)

    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the charmtally command-line interface."""
    p = argparse.ArgumentParser(prog="charmtally")
    p.add_argument(
        "--features",
        type=Path,
        default=DEFAULT_CATALOGUE,
        help="Path to features.yaml (default: the catalogue shipped in the package)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_local = sub.add_parser("local", help="Scan a checked-out charm dir.")
    p_local.add_argument("charm_dir", type=Path)
    p_local.add_argument("--only", nargs="+", help="Limit to these feature names.")
    p_local.set_defaults(func=cmd_local)

    p_spike = sub.add_parser("spike", help="Clone+scan a slice of the corpus.")
    p_spike.add_argument(
        "--corpus",
        type=Path,
        default=None,
        help="Path to a local CSV. Default: fetch --corpus-url.",
    )
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
    p_spike.add_argument(
        "--key-only", action="store_true", help="Only scan rows marked 'Key Charm for this Team'."
    )
    p_spike.add_argument(
        "--only",
        nargs="+",
        help="Restrict to these feature names (default: all in features.yaml).",
    )
    p_spike.set_defaults(func=cmd_spike)

    p_scan = sub.add_parser("scan", help="Full corpus scan → results.json.")
    p_scan.add_argument(
        "--corpus",
        type=Path,
        default=None,
        help="Path to a local CSV. Default: fetch --corpus-url.",
    )
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
        help=(
            "Include charms from these teams (case-insensitive). Combined with --key-only via OR."
        ),
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
        help=(
            "Path to corpus-overrides.yaml; applies the same feature_excludes "
            "the scan command would apply."
        ),
    )
    p_score.set_defaults(func=cmd_score)

    p_rocks = sub.add_parser(
        "scan-rocks", help="Scan rocks.csv's rockcraft.yaml files → __rocks__ / rocks.json."
    )
    p_rocks.add_argument(
        "--rocks",
        type=Path,
        default=Path("rocks.csv"),
        help="Rocks corpus CSV, as maintained by tools/rockfind.py (default: rocks.csv).",
    )
    p_rocks.add_argument(
        "--into",
        type=Path,
        default=None,
        help="Scored JSON to write the `__rocks__` block into, in place.",
    )
    p_rocks.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write the block to this standalone JSON file as well.",
    )
    p_rocks.add_argument(
        "--workers",
        type=int,
        default=_rocks.DEFAULT_WORKERS,
        help=f"Parallel fetches (default: {_rocks.DEFAULT_WORKERS}).",
    )
    p_rocks.set_defaults(func=cmd_scan_rocks)

    p_snap = sub.add_parser(
        "snapshot", help="Write the dated, thinned snapshot the trend pages read."
    )
    p_snap.add_argument("scored", type=Path, help="Path to scored.json.")
    p_snap.add_argument(
        "--snapshots-dir",
        type=Path,
        default=Path("snapshots"),
        dest="snapshots_dir",
        help="Directory to write scored-YYYY-MM-DD.json into (default: snapshots/).",
    )
    p_snap.add_argument(
        "--date",
        default=None,
        help="ISO date for the snapshot filename (default: today, UTC).",
    )
    p_snap.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write to this exact path instead, ignoring --snapshots-dir/--date.",
    )
    p_snap.set_defaults(func=cmd_snapshot)

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

    p_llm_score = sub.add_parser(
        "llm-score", help="LLM scoring pass over worth-considering records → llm-scored.json."
    )
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

    p_llm_cal = sub.add_parser(
        "llm-calibrate", help="Pre-flight calibration: compare LLM to human ground truth."
    )
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

    p_trend = sub.add_parser(
        "trend", help="Adoption trend + diff list across CI snapshots → trend.html."
    )
    p_trend.add_argument(
        "--snapshots-dir",
        type=Path,
        default=Path("snapshots"),
        dest="snapshots_dir",
        help="Directory of scored-YYYY-MM-DD.json snapshots (default: snapshots/).",
    )
    p_trend.add_argument(
        "--live",
        type=Path,
        default=Path("scored.json"),
        help=(
            "Live scored.json, included as today's snapshot unless one already "
            "exists (default: scored.json)."
        ),
    )
    p_trend.add_argument(
        "--feature", default=None, help="Restrict adoption/timeline/diff to this feature."
    )
    p_trend.add_argument(
        "--since",
        default=None,
        metavar="DATE",
        help=(
            "ISO date (YYYY-MM-DD). Overrides the diff base snapshot and trims "
            "the adoption/timeline range."
        ),
    )
    p_trend.add_argument("--out", type=Path, default=Path("trend.html"))
    p_trend.add_argument(
        "--json",
        action="store_true",
        dest="emit_json",
        help="Also write the computed trend data as JSON alongside the HTML (<out>.json).",
    )
    p_trend.set_defaults(func=cmd_trend)

    p_adoption = sub.add_parser(
        "adoption",
        help="Charm-tech adoption scorecard across CI snapshots → adoption.html.",
    )
    p_adoption.add_argument(
        "--snapshots-dir",
        type=Path,
        default=Path("snapshots"),
        dest="snapshots_dir",
        help="Directory of scored-YYYY-MM-DD.json snapshots (default: snapshots/).",
    )
    p_adoption.add_argument(
        "--live",
        type=Path,
        default=Path("scored.json"),
        help=(
            "Live scored.json, included as today's snapshot unless one already "
            "exists (default: scored.json)."
        ),
    )
    p_adoption.add_argument(
        "--metric",
        default=None,
        help="Restrict the page to one metric key (default: the whole scorecard).",
    )
    p_adoption.add_argument(
        "--since",
        default=None,
        metavar="DATE",
        help="ISO date (YYYY-MM-DD). Trims every series to snapshots on/after it.",
    )
    p_adoption.add_argument("--out", type=Path, default=Path("adoption.html"))
    p_adoption.add_argument(
        "--json",
        action="store_true",
        dest="emit_json",
        help="Also write the computed metrics as JSON alongside the HTML (<out>.json).",
    )
    p_adoption.set_defaults(func=cmd_adoption)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
