"""Trend-over-time: read `snapshots/scored-*.json` (+ the live `scored.json`)
and compute adoption series, per-charm timelines, and a diff list between two
dates.

Data model guards (PLAN.md §"Trend over time" → "Data model considerations"),
all enforced here rather than left to callers:

  - Corpus drift: a (charm, feature) comparison only happens when the charm
    exists in both snapshots being compared. A charm missing from an older
    snapshot is "not yet in corpus", not a regression.
  - Feature catalogue drift: each snapshot carries its own feature set (read
    from the data, not the live catalogue). A feature absent from a
    snapshot's set is "not yet scanned" for that date, not absent.
  - Slug stability: a charm that disappears from one snapshot and reappears
    under a different slug is surfaced as a possible rename (matched via
    `repo_url`), not silently dropped or reported as a regression.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from pathlib import Path

_SNAPSHOT_RE = re.compile(r"scored-(\d{4}-\d{2}-\d{2})\.json$")


@dataclass(frozen=True)
class Snapshot:
    date: str  # ISO YYYY-MM-DD
    charms: dict[str, dict]  # slug -> charm record (features dict, repo_url, ...), __skipped__ excluded
    feature_names: frozenset[str]  # the feature catalogue as scanned at this date, from the data itself


def _feature_names_of(charm: dict) -> set[str]:
    return {k for k in charm.get("features", {}) if k != "__meta__"}


def _load_one(path: Path, date: str) -> Snapshot:
    raw: dict = json.loads(path.read_text())
    charms = {k: v for k, v in raw.items() if not k.startswith("__")}
    feature_names: set[str] = set()
    for charm in charms.values():
        feature_names |= _feature_names_of(charm)
    return Snapshot(date=date, charms=charms, feature_names=frozenset(feature_names))


def load_snapshots(
    snapshot_dir: Path,
    live_path: Path | None = None,
    *,
    today: dt.date | None = None,
) -> list[Snapshot]:
    """Load all dated snapshots plus, optionally, the live `scored.json`.

    Snapshots are discovered by globbing ``scored-*.json`` in ``snapshot_dir``
    and sorting by the ISO date embedded in the filename. If ``live_path``
    exists, it is included as one more snapshot dated ``today`` — unless a
    snapshot for that date already exists (the common case right after a CI
    run has just written both files), in which case the on-disk dated
    snapshot wins and the live copy is skipped as a duplicate.
    """
    dated: list[tuple[str, Path]] = []
    for path in snapshot_dir.glob("scored-*.json"):
        m = _SNAPSHOT_RE.search(path.name)
        if m:
            dated.append((m.group(1), path))
    dated.sort(key=lambda pair: pair[0])

    snapshots = [_load_one(path, date) for date, path in dated]

    if live_path is not None and live_path.is_file():
        live_date = (today or dt.date.today()).isoformat()
        if not any(s.date == live_date for s in snapshots):
            snapshots.append(_load_one(live_path, live_date))
            snapshots.sort(key=lambda s: s.date)

    return snapshots


def select_range(snapshots: list[Snapshot], since: str | None) -> list[Snapshot]:
    """Snapshots dated on/after `since` (all of them if `since` is None)."""
    if since is None:
        return snapshots
    return [s for s in snapshots if s.date >= since]


def compute_adoption(snapshots: list[Snapshot], feature: str | None = None) -> dict[str, list[dict]]:
    """Per-feature percent-present series, one point per snapshot date.

    Only counts a snapshot's charms toward a feature's denominator when that
    feature was actually scanned in that snapshot (feature-catalogue drift
    guard) — a feature added later has no data point for earlier dates.
    """
    all_features: set[str] = set()
    for s in snapshots:
        all_features |= s.feature_names
    wanted = {feature} if feature else all_features

    series: dict[str, list[dict]] = {f: [] for f in sorted(wanted)}
    for s in snapshots:
        for f in sorted(wanted):
            if f not in s.feature_names:
                continue
            total = 0
            present = 0
            for charm in s.charms.values():
                rec = charm.get("features", {}).get(f)
                if rec is None:
                    continue
                total += 1
                if rec.get("present"):
                    present += 1
            if total == 0:
                continue
            series[f].append({
                "date": s.date,
                "present": present,
                "total": total,
                "percent": round(100 * present / total, 1),
            })
    return series


_STATE_NOT_IN_CORPUS = "not-in-corpus"
_STATE_NOT_SCANNED = "not-scanned"


def _cell_state(snapshot: Snapshot, slug: str, feature: str) -> str:
    charm = snapshot.charms.get(slug)
    if charm is None:
        return _STATE_NOT_IN_CORPUS
    if feature not in snapshot.feature_names:
        return _STATE_NOT_SCANNED
    rec = charm.get("features", {}).get(feature)
    if rec is None:
        return _STATE_NOT_SCANNED
    if rec.get("present"):
        return "present"
    return rec.get("score", "not-applicable")


def compute_timeline(
    snapshots: list[Snapshot],
    *,
    feature: str | None = None,
    charm: str | None = None,
) -> list[dict]:
    """Per (charm, feature) row with one cell per snapshot date.

    Cell state is one of: present / clear-gap / worth-considering /
    not-applicable / not-in-corpus (corpus-drift guard) / not-scanned
    (feature-catalogue-drift guard).
    """
    if not snapshots:
        return []

    all_features: set[str] = set()
    all_slugs: set[str] = set()
    for s in snapshots:
        all_features |= s.feature_names
        all_slugs |= set(s.charms)

    features = {feature} if feature else all_features
    slugs = {charm} if charm else all_slugs

    rows: list[dict] = []
    for slug in sorted(slugs):
        for fname in sorted(features):
            cells = [{"date": s.date, "state": _cell_state(s, slug, fname)} for s in snapshots]
            if all(c["state"] in (_STATE_NOT_IN_CORPUS, _STATE_NOT_SCANNED) for c in cells):
                continue
            rows.append({"charm": slug, "feature": fname, "cells": cells})
    return rows


def _detect_renames(base: Snapshot, latest: Snapshot) -> list[dict]:
    """Match slugs that vanished from `base` against slugs that newly
    appeared in `latest`, via identical `repo_url`. Ambiguous matches (more
    than one candidate sharing a repo_url on either side) are left unmatched
    — better to miss a rename than to report a wrong one.
    """
    disappeared = set(base.charms) - set(latest.charms)
    appeared = set(latest.charms) - set(base.charms)
    if not disappeared or not appeared:
        return []

    by_repo_disappeared: dict[str, list[str]] = {}
    for slug in disappeared:
        repo = base.charms[slug].get("repo_url", "")
        by_repo_disappeared.setdefault(repo, []).append(slug)

    by_repo_appeared: dict[str, list[str]] = {}
    for slug in appeared:
        repo = latest.charms[slug].get("repo_url", "")
        by_repo_appeared.setdefault(repo, []).append(slug)

    renames: list[dict] = []
    for repo, old_slugs in by_repo_disappeared.items():
        new_slugs = by_repo_appeared.get(repo)
        if not new_slugs or len(old_slugs) != 1 or len(new_slugs) != 1:
            continue  # ambiguous — more than one candidate on either side
        renames.append({"old_slug": old_slugs[0], "new_slug": new_slugs[0], "repo_url": repo})
    return renames


def compute_diff(base: Snapshot, latest: Snapshot) -> dict:
    """(charm, feature) pairs whose `present` flipped between two snapshots.

    Applies the corpus-drift guard (only compares charms present in both)
    and the feature-drift guard (only compares features scanned in both),
    and separately surfaces possible renames so a vanished slug isn't
    reported as a pile of regressions.
    """
    common_slugs = set(base.charms) & set(latest.charms)
    common_features = base.feature_names & latest.feature_names

    flips: list[dict] = []
    for slug in sorted(common_slugs):
        base_charm = base.charms[slug]
        latest_charm = latest.charms[slug]
        for fname in sorted(common_features):
            base_rec = base_charm.get("features", {}).get(fname)
            latest_rec = latest_charm.get("features", {}).get(fname)
            if base_rec is None or latest_rec is None:
                continue
            was = bool(base_rec.get("present"))
            now = bool(latest_rec.get("present"))
            if was == now:
                continue
            flips.append({
                "charm": slug,
                "feature": fname,
                "from": was,
                "to": now,
                "kind": "regression" if was and not now else "adoption",
            })

    return {
        "base_date": base.date,
        "latest_date": latest.date,
        "flips": flips,
        "possible_renames": _detect_renames(base, latest),
    }


def select_base(snapshots: list[Snapshot], since: str | None) -> Snapshot | None:
    """Pick the base snapshot for a diff: the earliest by default, or the
    earliest snapshot dated on/after `since` when given."""
    candidates = select_range(snapshots, since)
    if not candidates:
        return None
    return candidates[0]
