"""Render scan results (results.json) to a static HTML dashboard.

Two tables (PLAN.md §D):
  - Feature view: row per feature, with counts + linkable exemplars.
  - Charm view:   row per charm, with totals + the list of clear-gap features.

Evidence-to-GitHub links are derived from the corpus `repo_url` plus the
charm-relative file path captured at scan time. We default to `main` as the
ref since the spike doesn't capture commit SHAs yet (a v1+ improvement; see
PLAN.md §9).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import jinja2

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


def _gh_blob(repo_url: str, ref: str, file_path: str, line: int) -> str:
    base = repo_url.rstrip("/").removesuffix(".git")
    return f"{base}/blob/{ref}/{file_path}#L{line}"


def _exemplar(charm: dict, ref: str, evidence: list[dict]) -> dict:
    """Pick the first concrete evidence line for an exemplar link."""
    if evidence:
        e = evidence[0]
        return {
            "charm": charm["name"],
            "url": _gh_blob(charm["repo_url"], ref, e["file"], e["line"]),
        }
    return {"charm": charm["name"], "url": charm["repo_url"]}


_ARCH_PRIORITY = (
    "reactive",  # short-circuits all feature scoring; tracked separately
    "component-graph",
    "reconcile-all",
    "reconcile",
    "unconditional-init",  # holistic-in-init
    "part-reconcile",  # delta outside, holistic inside
    "delta",  # implicit default
)


def _primary_arch(meta: dict) -> str:
    """Return the single most specific architecture label for a charm.

    A charm can match multiple patterns (e.g. mysql-router matches both
    reconcile-all and reconcile). Use a fixed priority order so each charm
    lands in exactly one bucket, mirroring the chip-row UI: reactive wins
    over everything; otherwise component-graph > reconcile-all > reconcile;
    delta is the implicit default for charms with no pattern matches.
    """
    if meta.get("is_reactive"):
        return "reactive"
    archs = meta.get("architecture") or []
    for a in _ARCH_PRIORITY:
        if a == "delta":
            continue
        if a in archs:
            return a
    return "delta"


def render(results: dict, features: list, ref: str = "main") -> str:
    charms = [v for k, v in results.items() if not k.startswith("__")]
    feat_meta = {f.name: f for f in features}
    feat_names = [f.name for f in features]

    # Pre-bucket each charm into its primary architecture (single pick).
    arch_of_charm = {c["name"]: _primary_arch(c["features"].get("__meta__", {})) for c in charms}

    # Feature view rows.
    feature_rows = []
    for fname in feat_names:
        present = 0
        clear_gap = 0
        worth = 0
        na = 0
        exemplars: list[dict] = []
        # Per-architecture adoption counts. Tracks present/total per bucket so
        # we can show "delta 60% (123/204)" alongside the corpus-wide totals.
        # Reactive charms are kept in their own bucket and reported as N/A
        # because every feature score short-circuits to not-applicable for them.
        by_arch: dict[str, dict[str, int]] = {a: {"present": 0, "total": 0} for a in _ARCH_PRIORITY}
        for c in charms:
            rec = c["features"].get(fname, {})
            arch = arch_of_charm[c["name"]]
            by_arch[arch]["total"] += 1
            if rec.get("present"):
                present += 1
                by_arch[arch]["present"] += 1
                if len(exemplars) < 5:
                    exemplars.append(_exemplar(c, ref, rec.get("evidence", [])))
            else:
                score = rec.get("score", "not-applicable")
                if score == "clear-gap":
                    clear_gap += 1
                elif score == "worth-considering":
                    worth += 1
                else:
                    na += 1
        # Compact list for the template — only buckets with at-least-1 charm,
        # reactive flagged as N/A, others as percent + raw counts.
        arch_adoption = []
        for a in _ARCH_PRIORITY:
            stats = by_arch[a]
            if stats["total"] == 0:
                continue
            if a == "reactive":
                arch_adoption.append({
                    "arch": a,
                    "label": "n/a",
                    "tooltip": f"{stats['total']} reactive charms — feature scoring N/A",
                    "is_na": True,
                    "pct": None,
                })
            else:
                pct = 100 * stats["present"] // stats["total"]
                arch_adoption.append({
                    "arch": a,
                    "label": f"{pct}%",
                    "tooltip": f"{stats['present']} / {stats['total']} {a} charms have this feature",
                    "is_na": False,
                    "pct": pct,
                })
        feature_rows.append({
            "name": fname,
            "library": feat_meta[fname].library,
            "present": present,
            "clear_gap": clear_gap,
            "worth": worth,
            "na": na,
            "exemplars": exemplars,
            "arch_adoption": arch_adoption,
        })

    # Charm view rows. Pulls the descriptive __meta__ facts forward so each
    # row carries its architecture label, stack (plugin/base/juju/libs/tooling),
    # and k8s/reactive/lib-provider/terraform flags — surfaced as chips +
    # a compact stack cell in the rendered HTML.
    charm_rows = []
    for c in charms:
        present = clear_gap = worth = 0
        gaps = []
        for fname in feat_names:
            rec = c["features"].get(fname, {})
            if rec.get("present"):
                present += 1
            elif rec.get("score") == "clear-gap":
                clear_gap += 1
                gaps.append({"feature": fname, "rationale": rec.get("rationale", "")})
            elif rec.get("score") == "worth-considering":
                worth += 1
        m = c["features"].get("__meta__", {})
        architecture = list(m.get("architecture") or [])
        if m.get("is_reactive"):
            arch_labels = ["reactive"]
        elif architecture:
            arch_labels = architecture
        else:
            arch_labels = ["delta"]  # implicit default
        charm_rows.append({
            "name": c["name"],
            "team": c.get("team", ""),
            "repo_url": c["repo_url"],
            "present": present,
            "clear_gap": clear_gap,
            "worth": worth,
            "gaps": gaps,
            "architecture": arch_labels,
            "k8s": m.get("has_containers", False),
            "is_reactive": m.get("is_reactive", False),
            "is_subordinate": m.get("is_subordinate", False),
            "is_workload_less": m.get("is_workload_less", False),
            "provides_own_library": m.get("provides_own_library", False),
            "has_terraform_module": m.get("has_terraform_module", False),
            "library_count": m.get("library_count", 0),
            "plugins": list(m.get("charmcraft_plugins") or []),
            "bases": list(m.get("bases") or []),
            "min_juju": m.get("min_juju_version"),
            "tooling": list(m.get("tooling") or []),
        })

    # Team rollup: one row per team aggregating per-charm stats. Mirrors the
    # questions "which team has the most gaps to migrate?" and "what's team
    # X's architectural footprint?" Counts are at the (charm × feature) level
    # for present/clear-gap/worth, and at the charm level for architecture.
    team_acc: dict[str, dict] = {}
    for r in charm_rows:
        team = r["team"] or "(no team)"
        bucket = team_acc.setdefault(
            team,
            {
                "team": team,
                "charms": 0,
                "present": 0,
                "clear_gap": 0,
                "worth": 0,
                "architecture": {},
                "gap_features": {},
            },
        )
        bucket["charms"] += 1
        bucket["present"] += r["present"]
        bucket["clear_gap"] += r["clear_gap"]
        bucket["worth"] += r["worth"]
        # primary arch (single pick, same priority as the chip)
        if r["is_reactive"]:
            primary = "reactive"
        elif r["architecture"] and r["architecture"][0] != "delta":
            primary = r["architecture"][0]
        else:
            primary = "delta"
        bucket["architecture"][primary] = bucket["architecture"].get(primary, 0) + 1
        for g in r["gaps"]:
            bucket["gap_features"][g["feature"]] = bucket["gap_features"].get(g["feature"], 0) + 1

    team_rows = []
    for bucket in team_acc.values():
        team_rows.append({
            "team": bucket["team"],
            "charms": bucket["charms"],
            "present": bucket["present"],
            "clear_gap": bucket["clear_gap"],
            "worth": bucket["worth"],
            "avg_gap": round(bucket["clear_gap"] / bucket["charms"], 1) if bucket["charms"] else 0,
            "architecture": sorted(bucket["architecture"].items(), key=lambda kv: -kv[1]),
            "top_gaps": sorted(bucket["gap_features"].items(), key=lambda kv: -kv[1])[:5],
        })
    team_rows.sort(key=lambda r: -r["clear_gap"])

    # Top-of-page summary: distributions over the corpus that don't fit
    # cleanly into either of the two main tables.
    arch_dist: dict[str, int] = {}
    tooling_dist: dict[str, int] = {}
    plugin_dist: dict[str, int] = {}
    base_dist: dict[str, int] = {}
    k8s_count = 0
    reactive_count = 0
    own_lib_count = 0
    tf_count = 0
    for r in charm_rows:
        for a in r["architecture"]:
            arch_dist[a] = arch_dist.get(a, 0) + 1
        for t in r["tooling"]:
            tooling_dist[t] = tooling_dist.get(t, 0) + 1
        for p in r["plugins"]:
            plugin_dist[p] = plugin_dist.get(p, 0) + 1
        for b in r["bases"]:
            base_dist[b] = base_dist.get(b, 0) + 1
        if r["k8s"]:
            k8s_count += 1
        if r["is_reactive"]:
            reactive_count += 1
        if r["provides_own_library"]:
            own_lib_count += 1
        if r["has_terraform_module"]:
            tf_count += 1
    summary = {
        "total": len(charm_rows),
        "k8s": k8s_count,
        "machine": len(charm_rows) - k8s_count,
        "reactive": reactive_count,
        "own_library": own_lib_count,
        "terraform": tf_count,
        "architecture": sorted(arch_dist.items(), key=lambda kv: -kv[1]),
        "tooling": sorted(tooling_dist.items(), key=lambda kv: -kv[1]),
        "plugins": sorted(plugin_dist.items(), key=lambda kv: -kv[1]),
        "bases": sorted(base_dist.items(), key=lambda kv: -kv[1])[:6],
    }

    # Evidence log (all clear-gap findings, flattened).
    evidence_log = []
    for c in charms:
        for fname in feat_names:
            rec = c["features"].get(fname, {})
            if rec.get("score") == "clear-gap":
                evidence_log.append({
                    "charm": c["name"],
                    "feature": fname,
                    "library": feat_meta[fname].library,
                    "rationale": rec.get("rationale", ""),
                })

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(_TEMPLATE_DIR),
        autoescape=jinja2.select_autoescape(["html"]),
    )
    tmpl = env.get_template("dashboard.html.j2")
    return tmpl.render(
        charms=charms,
        features=features,
        feature_rows=feature_rows,
        charm_rows=charm_rows,
        team_rows=team_rows,
        evidence_log=evidence_log,
        summary=summary,
        generated_at=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    )
