"""Render scan results (results.json) to a static HTML dashboard.

Two tables (PLAN.md §D):
  - Feature view: row per feature, with counts + linkable exemplars.
  - Charm view:   row per charm, with totals + the list of clear-gap features.

Each table has a filter bar. The template's JS is generic — it reads the
axes off `data-` attributes on the rows and builds the controls from the
`facets` mapping rendered here — so anything that should be filterable has
to be emitted as a row attribute (see `dashboard.html.j2`), and the filter
state round-trips through the query string so a view can be linked.

Evidence-to-GitHub links are permalinks: the corpus `repo_url`, the commit the
scan ran at (`__meta__.repo_sha`), and the evidence path re-based from the
charm root onto the repo root via the record's `subpath`. Both parts matter —
against `main` the line numbers drift as upstream moves, and without `subpath`
every monorepo sub-charm link points at a path that doesn't exist. `ref` is the
fallback for records with no `repo_sha` (a charm root that isn't a git
checkout, i.e. `charmtally local`).
"""

from __future__ import annotations

import datetime as dt
import posixpath
from pathlib import Path
from typing import TYPE_CHECKING, Any

import jinja2

from . import adoption as _adoption

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


def _environment() -> jinja2.Environment:
    """Jinja environment for the dashboard and trend templates.

    autoescape is set unconditionally rather than via select_autoescape:
    the templates are named `*.html.j2`, and select_autoescape looks only at
    the final extension, so `j2` was tested against the enabled list and
    escaping was off. Every page interpolates data derived from third-party
    charm repositories — names, repo URLs, rationale strings — and is
    published to GitHub Pages.
    """
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(_TEMPLATE_DIR),
        autoescape=True,
    )


def _gh_blob(repo_url: str, ref: str, file_path: str, line: int) -> str:
    """Build a GitHub blob URL for `file_path` at `ref`, anchored on `line`.

    Line 0 means "this file, no particular line" — the file-independent
    detectors (`yaml-key`, `requires-interface`, `relation-count`) report a
    structural match with no located line. Emitting `#L0` for those made
    GitHub scroll to nothing, so the anchor is dropped instead.
    """
    base = repo_url.rstrip("/").removesuffix(".git")
    url = f"{base}/blob/{ref}/{file_path}"
    return f"{url}#L{line}" if line > 0 else url


def _evidence_ref(charm: dict, fallback: str) -> str:
    """Return the commit to link evidence against: the scanned SHA, else `fallback`."""
    return charm.get("features", {}).get("__meta__", {}).get("repo_sha") or fallback


def _repo_path(charm: dict, file_path: str) -> str:
    """Re-base a charm-root-relative evidence path onto the repo root.

    Monorepo records carry the sub-charm directory as `subpath`; evidence
    paths are relative to that directory, but the blob URL is relative to the
    repo. Single-charm records have no `subpath` and pass through unchanged.

    `repo-file` evidence sits *above* the charm root and says so with `..`
    segments, so the join is normalised rather than concatenated: a sub-charm
    at `charms/foo` reporting `../../.github/workflows/ci.yaml` links to
    `.github/workflows/ci.yaml`, which is where the file actually is.
    """
    subpath = (charm.get("subpath") or "").strip("/")
    joined = f"{subpath}/{file_path}" if subpath else file_path
    return posixpath.normpath(joined) if ".." in joined else joined


def _exemplar(charm: dict, ref: str, evidence: list[dict]) -> dict:
    """Pick the first concrete evidence line for an exemplar link."""
    if evidence:
        e = evidence[0]
        return {
            "charm": charm["name"],
            "url": _gh_blob(
                charm["repo_url"],
                _evidence_ref(charm, ref),
                _repo_path(charm, e["file"]),
                e["line"],
            ),
        }
    return {"charm": charm["name"], "url": charm["repo_url"]}


# Below this many present-counts across the full corpus a feature is flagged
# as low-confidence in the feature view — the detector probably needs a
# re-check. Suppress per-feature with `expected_rare: true` in features.yaml.
# 5 hits is well under 1% of the current ~750-charm corpus.
_PRECISION_FLOOR = 5

_ARCH_PRIORITY = (
    "reactive",  # short-circuits all feature scoring; tracked separately
    "legacy-classic",  # pre-ops hooks/ layout; also N/A for the ops catalogue
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
    if meta.get("is_legacy_classic"):
        return "legacy-classic"
    archs = meta.get("architecture") or []
    for a in _ARCH_PRIORITY:
        if a == "delta":
            continue
        if a in archs:
            return a
    return "delta"


# Charms whose feature scores short-circuit to not-applicable, and so say
# nothing about whether an `ops` pin was what held a feature back.
_SCORING_NA_ARCH = ("reactive", "legacy-classic")

# Where an unreadable pin and an unpinned one sort, after the numbered
# majors. Two buckets rather than one: a charm that asks for bare `ops`
# resolves to the newest release and could hold any feature in the
# catalogue, whereas one we could not read a requirement for is a charm we
# know nothing about. Collapsing them would report the first as ignorance.
_OPS_UNPINNED = "unpinned"
_OPS_UNKNOWN = "unknown"


def _ops_cohort(meta: dict) -> str:
    """Bucket a charm by the `ops` major version its dependencies ask for.

    The major is the boundary because that is where `ops` itself breaks API;
    any finer split would be a number we picked rather than one the library
    draws. `ops_min_version` is a lower bound, so a cohort reads as "this
    charm will not resolve an `ops` older than major N" — one pinned
    `>=2.17` may well be running 3.x, and that is the point: it is allowed
    to, so a feature it lacks is a feature it declined rather than one its
    pin withheld.
    """
    requirement = meta.get("ops_requirement")
    if requirement is None:
        return _OPS_UNKNOWN
    if not requirement:
        return _OPS_UNPINNED
    floor = meta.get("ops_min_version")
    if not floor:
        # A specifier with no lower bound at all (`<4`, `!=2.9`): the charm
        # asked for something, but for nothing that names an oldest release.
        return _OPS_UNKNOWN
    return f"ops {floor.split('.')[0]}"


def _ops_cohort_order(cohorts: Iterable[str]) -> list[str]:
    """Display order for the cohorts present: majors ascending, then the other two.

    Derived from the data rather than hardcoded, so the day a charm pins
    `ops>=4` the dashboard grows a cohort instead of filing it under
    unknown.
    """
    seen = set(cohorts)
    majors = sorted(
        (c for c in seen if c not in (_OPS_UNPINNED, _OPS_UNKNOWN)),
        key=lambda c: int(c.split()[1]),
    )
    return [*majors, *(c for c in (_OPS_UNPINNED, _OPS_UNKNOWN) if c in seen)]


def _juju_assertion(meta: dict) -> str | None:
    """Render a charm's `assumes:` Juju bounds as one specifier.

    ">=3.4", "<4.0.0", ">=3.4,<4.0.0", or None when the charm asserts
    neither bound. One cell rather than two because the floor and the
    ceiling answer the same question, and the ceiling is the half that
    decides whether the charm can be deployed on Juju 4 at all.
    """
    lo = meta.get("min_juju_version")
    hi = meta.get("max_juju_version")
    return ",".join(b for b in (f">={lo}" if lo else "", f"<{hi}" if hi else "") if b) or None


def render(results: dict, features: list, ref: str = "main", *, pairs: list | None = None) -> str:
    """Render the survey results as a standalone HTML dashboard."""
    charms = [v for k, v in results.items() if not k.startswith("__")]
    feat_meta = {f.name: f for f in features}
    feat_names = [f.name for f in features]

    # Pre-bucket each charm into its primary architecture (single pick).
    arch_of_charm = {c["name"]: _primary_arch(c["features"].get("__meta__", {})) for c in charms}
    # And by the `ops` major its dependencies ask for (#74). Reactive and
    # legacy-classic charms are left out rather than bucketed: their feature
    # scores are not-applicable by construction, so counting them would put
    # a floor under every cohort they land in that has nothing to do with
    # anyone's pin.
    ops_cohort_of_charm = {
        c["name"]: _ops_cohort(c["features"].get("__meta__", {}))
        for c in charms
        if arch_of_charm[c["name"]] not in _SCORING_NA_ARCH
    }
    ops_cohort_names = _ops_cohort_order(ops_cohort_of_charm.values())

    # Feature view rows.
    feature_rows: list[dict[str, Any]] = []
    for fname in feat_names:
        present = 0
        clear_gap = 0
        clear_gap_ai = 0
        worth = 0
        na = 0
        exemplars: list[dict] = []
        # Per-architecture adoption counts. Tracks present/total per bucket so
        # we can show "delta 60% (123/204)" alongside the corpus-wide totals.
        # Reactive charms are kept in their own bucket and reported as N/A
        # because every feature score short-circuits to not-applicable for them.
        by_arch: dict[str, dict[str, int]] = {
            a: {"present": 0, "total": 0} for a in _ARCH_PRIORITY
        }
        by_ops: dict[str, dict[str, int]] = {
            c: {"present": 0, "total": 0} for c in ops_cohort_names
        }
        for c in charms:
            rec = c["features"].get(fname, {})
            arch = arch_of_charm[c["name"]]
            by_arch[arch]["total"] += 1
            cohort = ops_cohort_of_charm.get(c["name"])
            if cohort is not None:
                by_ops[cohort]["total"] += 1
            if rec.get("present"):
                present += 1
                by_arch[arch]["present"] += 1
                if cohort is not None:
                    by_ops[cohort]["present"] += 1
                if len(exemplars) < 5:
                    exemplars.append(_exemplar(c, ref, rec.get("evidence", [])))
            else:
                score = rec.get("score", "not-applicable")
                if score == "clear-gap":
                    clear_gap += 1
                    if rec.get("ai_escalated"):
                        clear_gap_ai += 1
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
            elif a == "legacy-classic":
                arch_adoption.append({
                    "arch": a,
                    "label": "n/a",
                    "tooltip": f"{stats['total']} legacy-classic charms — feature scoring N/A",
                    "is_na": True,
                    "pct": None,
                })
            else:
                pct = 100 * stats["present"] // stats["total"]
                arch_adoption.append({
                    "arch": a,
                    "label": f"{pct}%",
                    "tooltip": (
                        f"{stats['present']} / {stats['total']} {a} charms have this feature"
                    ),
                    "is_na": False,
                    "pct": pct,
                })
        # The same cells again, over the `ops` pin instead of the architecture
        # (#74). A row that is low everywhere says the API went unused; one
        # that climbs with the major says the charms without it are on an
        # `ops` that predates it, which is a dependency bump rather than a
        # decision anyone made about the API.
        ops_adoption = []
        for cohort in ops_cohort_names:
            stats = by_ops[cohort]
            if stats["total"] == 0:
                continue
            pct = 100 * stats["present"] // stats["total"]
            ops_adoption.append({
                "cohort": cohort,
                "label": f"{pct}%",
                "tooltip": (
                    f"{stats['present']} / {stats['total']} charms "
                    f"in the {cohort} cohort have this feature"
                ),
                "pct": pct,
            })
        # Low-count flag: a feature with fewer than PRECISION_FLOOR positive
        # hits across the whole corpus is suspicious — usually it means the
        # detector is too strict. Suppress for features explicitly marked
        # `expected_rare: true` in features.yaml.
        low_count = present < _PRECISION_FLOOR and not feat_meta[fname].expected_rare
        feature_rows.append({
            "name": fname,
            "library": feat_meta[fname].library,
            "present": present,
            "clear_gap": clear_gap,
            "clear_gap_ai": clear_gap_ai,
            "worth": worth,
            "na": na,
            "exemplars": exemplars,
            "arch_adoption": arch_adoption,
            "ops_adoption": ops_adoption,
            "low_count": low_count,
        })

    # Charm view rows. Pulls the descriptive __meta__ facts forward so each
    # row carries its architecture label, stack (plugin/base/juju/libs/tooling),
    # and k8s/reactive/lib-provider/terraform flags — surfaced as chips +
    # a compact stack cell in the rendered HTML.
    charm_rows: list[dict[str, Any]] = []
    for c in charms:
        present = clear_gap = clear_gap_ai = worth = 0
        gaps = []
        present_features: list[str] = []
        for fname in feat_names:
            rec = c["features"].get(fname, {})
            if rec.get("present"):
                present += 1
                present_features.append(fname)
            elif rec.get("score") == "clear-gap":
                clear_gap += 1
                if rec.get("ai_escalated"):
                    clear_gap_ai += 1
                gaps.append({
                    "feature": fname,
                    "rationale": rec.get("rationale", ""),
                    "ai_escalated": bool(rec.get("ai_escalated")),
                })
            elif rec.get("score") == "worth-considering":
                worth += 1
        m = c["features"].get("__meta__", {})
        architecture = list(m.get("architecture") or [])
        # Boolean facts as filter-bar slugs. The dashboard turns these into
        # AND-able toggle buttons and makes them searchable as `flag:`.
        flags = [
            slug
            for slug, on in (
                ("lib-provider", m.get("provides_own_library")),
                ("subordinate", m.get("is_subordinate")),
                ("workload-less", m.get("is_workload_less")),
                ("terraform", m.get("has_terraform_module")),
            )
            if on
        ]
        if m.get("is_reactive"):
            arch_labels = ["reactive"]
        elif m.get("is_legacy_classic"):
            arch_labels = ["legacy-classic"]
        elif architecture:
            arch_labels = architecture
        else:
            arch_labels = ["delta"]  # implicit default
        plugins = list(m.get("charmcraft_plugins") or [])
        bases = list(m.get("bases") or [])
        tooling = list(m.get("tooling") or [])
        juju_assertion = _juju_assertion(m)
        # Free-text blob behind the search box's `stack:` field.
        stack_text = " ".join([
            "k8s" if m.get("has_containers") else "machine",
            *plugins,
            *bases,
            *tooling,
            f"juju {juju_assertion}" if juju_assertion else "",
            # `ops` with no specifier is a finding, so it gets a searchable
            # word rather than falling through the falsy branch as unknown.
            f"ops {m['ops_requirement'] or 'unpinned'}"
            if m.get("ops_requirement") is not None
            else "",
            "terraform" if m.get("has_terraform_module") else "",
        ]).strip()
        charm_rows.append({
            "name": c["name"],
            "team": c.get("team", ""),
            "repo_url": c["repo_url"],
            "present": present,
            "clear_gap": clear_gap,
            "clear_gap_ai": clear_gap_ai,
            "worth": worth,
            "gaps": gaps,
            "present_features": present_features,
            "gap_features": [g["feature"] for g in gaps],
            "architecture": arch_labels,
            "flags": flags,
            "k8s": m.get("has_containers", False),
            "is_reactive": m.get("is_reactive", False),
            "is_subordinate": m.get("is_subordinate", False),
            "is_workload_less": m.get("is_workload_less", False),
            "is_legacy_classic": m.get("is_legacy_classic", False),
            "provides_own_library": m.get("provides_own_library", False),
            "has_terraform_module": m.get("has_terraform_module", False),
            "library_count": m.get("library_count", 0),
            "plugins": plugins,
            "bases": bases,
            "juju_assertion": juju_assertion,
            "ops_requirement": m.get("ops_requirement"),
            "tooling": tooling,
            "stack_text": stack_text,
        })

    # Team rollup: one row per team aggregating per-charm stats. Mirrors the
    # questions "which team has the most gaps to migrate?" and "what's team
    # X's architectural footprint?" Counts are at the (charm × feature) level
    # for present/clear-gap/worth, and at the charm level for architecture.
    team_acc: dict[str, dict[str, Any]] = {}
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
        elif r["is_legacy_classic"]:
            primary = "legacy-classic"
        elif r["architecture"] and r["architecture"][0] != "delta":
            primary = r["architecture"][0]
        else:
            primary = "delta"
        bucket["architecture"][primary] = bucket["architecture"].get(primary, 0) + 1
        for g in r["gaps"]:
            bucket["gap_features"][g["feature"]] = bucket["gap_features"].get(g["feature"], 0) + 1

    team_rows: list[dict[str, Any]] = [
        {
            "team": bucket["team"],
            "charms": bucket["charms"],
            "present": bucket["present"],
            "clear_gap": bucket["clear_gap"],
            "worth": bucket["worth"],
            "avg_gap": round(bucket["clear_gap"] / bucket["charms"], 1) if bucket["charms"] else 0,
            "architecture": sorted(bucket["architecture"].items(), key=lambda kv: -kv[1]),
            "top_gaps": sorted(bucket["gap_features"].items(), key=lambda kv: -kv[1])[:5],
        }
        for bucket in team_acc.values()
    ]
    team_rows.sort(key=lambda r: -r["clear_gap"])

    # Top-of-page summary: distributions over the corpus that don't fit
    # cleanly into either of the two main tables.
    arch_dist: dict[str, int] = {}
    tooling_dist: dict[str, int] = {}
    plugin_dist: dict[str, int] = {}
    base_dist: dict[str, int] = {}
    k8s_count = 0
    reactive_count = 0
    legacy_classic_count = 0
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
        if r["is_legacy_classic"]:
            legacy_classic_count += 1
        if r["provides_own_library"]:
            own_lib_count += 1
        if r["has_terraform_module"]:
            tf_count += 1
    summary = {
        "total": len(charm_rows),
        "k8s": k8s_count,
        "machine": len(charm_rows) - k8s_count,
        "reactive": reactive_count,
        "legacy_classic": legacy_classic_count,
        "own_library": own_lib_count,
        "terraform": tf_count,
        "architecture": sorted(arch_dist.items(), key=lambda kv: -kv[1]),
        "tooling": sorted(tooling_dist.items(), key=lambda kv: -kv[1]),
        "plugins": sorted(plugin_dist.items(), key=lambda kv: -kv[1]),
        "bases": sorted(base_dist.items(), key=lambda kv: -kv[1])[:6],
    }

    # Facet values for the filter bars. Derived here rather than with
    # map/unique chains in the template so the option lists are sorted and
    # deduplicated once, in code that can be tested.
    facets = {
        "teams": sorted({r["team"] or "(no team)" for r in charm_rows}, key=str.lower),
        "libraries": sorted({r["library"] for r in feature_rows}),
        "tooling": sorted({t for r in charm_rows for t in r["tooling"]}),
        # The full taxonomy, not just the buckets that are currently
        # populated — the filter bar should stay stable week to week, and a
        # bucket with 0 charms is itself worth being able to check for.
        "architectures": [(a, arch_dist.get(a, 0)) for a in _ARCH_PRIORITY],
    }

    # Evidence log (all clear-gap findings, flattened).
    evidence_log: list[dict[str, Any]] = []
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

    env = _environment()
    tmpl = env.get_template("dashboard.html.j2")
    return tmpl.render(
        charms=charms,
        features=features,
        feature_rows=feature_rows,
        charm_rows=charm_rows,
        team_rows=team_rows,
        evidence_log=evidence_log,
        summary=summary,
        facets=facets,
        pairs=pairs or [],
        generated_at=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    )


def render_trend(
    diff: dict,
    adoption: dict[str, list[dict]],
    timeline: list[dict],
    *,
    feature_filter: str | None = None,
    timeline_url: str = "trend.timeline.json",
) -> str:
    """Render the `trend` subcommand's output — the History page.

    with a diff list (default view), an adoption-over-time chart, and a
    per-(charm, feature) timeline. See trend.py for how these are computed
    and the corpus/feature-drift guards applied along the way.

    The page is no longer standalone: the timeline browser fetches
    `timeline_url` (relative to the page) the first time its tab is opened,
    rather than carrying the whole (charms x features x snapshots) matrix
    inline. The caller is responsible for writing that file —
    `trend.encode_timeline` produces its contents. Everything below the fetch
    degrades to the static tables, which is also what happens when the page
    is opened over `file://`, where fetch is blocked.
    """
    env = _environment()
    tmpl = env.get_template("trend.html.j2")

    regressions = [f for f in diff["flips"] if f["kind"] == "regression"]
    adoptions = [f for f in diff["flips"] if f["kind"] == "adoption"]

    # The static fallback is keyed on the pairs that actually flipped, not on
    # every feature of any charm that flipped something: the latter multiplied
    # one flip into a full row of the catalogue, and at 30-odd snapshots it
    # was the single largest thing on the page.
    flipped_pairs = {(f["charm"], f["feature"]) for f in diff["flips"]}
    if feature_filter:
        static_timeline_rows = timeline
    else:
        static_timeline_rows = [
            row for row in timeline if (row["charm"], row["feature"]) in flipped_pairs
        ]

    adoption_dates: list[str] = sorted({
        point["date"] for series in adoption.values() for point in series
    })
    adoption_table = {
        fname: {point["date"]: point["percent"] for point in series}
        for fname, series in adoption.items()
    }

    # The feature dropdown has to be populated before the payload arrives, so
    # the names — 65 strings, not the 50k-row matrix behind them — stay inline.
    timeline_features = sorted({row["feature"] for row in timeline})

    return tmpl.render(
        diff=diff,
        regressions=regressions,
        adoptions=adoptions,
        adoption=adoption,
        adoption_table=adoption_table,
        adoption_dates=adoption_dates,
        static_timeline_rows=static_timeline_rows,
        timeline_features=timeline_features,
        timeline_url=timeline_url,
        feature_filter=feature_filter,
        generated_at=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    )


def render_adoption(
    metrics: Sequence[_adoption.Metric],
    series: dict[str, list[dict]],
) -> str:
    """Render the `adoption` subcommand's output — the charm-tech scorecard.

    `metrics` is `adoption.METRICS` (or a filtered subset) and `series` the
    matching output of `adoption.compute_series`. A metric with no series —
    either not yet defined, or defined but with no snapshot old enough to
    carry its inputs — still gets a card, so the page shows the full
    scorecard rather than silently dropping a metric.

    The template renders `metric.detail`, `metric.denominator_note` and each
    `metric.caveats` entry with `|safe`: those strings are authored in
    `adoption.py` and carry deliberate `<code>` markup. Nothing else on this
    page comes from a charm repository — the metrics are aggregates, so
    unlike the dashboard there is no third-party text to escape — but keep
    any new field that *does* carry charm data escaped.
    """
    env = _environment()
    tmpl = env.get_template("adoption.html.j2")

    cards: list[dict[str, Any]] = []
    for metric in metrics:
        points = series.get(metric.key, [])
        latest, delta = _adoption.latest_and_delta(points)
        cards.append({
            "metric": metric,
            "series": points,
            "latest": latest,
            "delta": delta,
            "pending": metric.pending,
        })

    # Built from the Metric objects rather than by reaching back into `cards`,
    # whose values are a union the type checker can't attribute-access.
    chart_data = [
        {
            "key": metric.key,
            "title": metric.title,
            "breakdown_keys": list(metric.breakdown_keys),
            "series": series.get(metric.key, []),
        }
        for metric in metrics
    ]

    return tmpl.render(
        cards=cards,
        chart_data=chart_data,
        generated_at=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    )
