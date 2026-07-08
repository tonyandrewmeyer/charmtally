"""Parse the charm corpus CSV.

The canonical source is **canonical/hyrum's `charm-list/charms.csv`**, kept up
to date by the hyrum team. ``HYRUM_CHARMS_CSV_URL`` is the raw URL; the
``scan`` and ``spike`` subcommands fetch it on demand and cache under the
workdir. Pass ``--corpus <local.csv>`` to override (offline / pinned scans).

The columns hyrum ships:
    Team, Charm Name, Repository, Branch (if not the default), Source

Older snapshots also carried ``Key Charm for this Team`` / ``Notes`` columns;
both are optional and default to ``False`` / ``""`` when absent.

`load_overrides` loads the companion YAML file (`corpus-overrides.yaml`) that
records URLs to skip and per-URL branch overrides.
"""

from __future__ import annotations

import csv
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import yaml

HYRUM_CHARMS_CSV_URL = "https://raw.githubusercontent.com/canonical/hyrum/main/charm-list/charms.csv"


@dataclass(frozen=True)
class CharmRef:
    team: str
    name: str
    repo_url: str
    key_charm: bool
    branch: str | None
    notes: str

    @property
    def slug(self) -> str:
        """A filesystem-safe id derived from the repo URL."""
        return self.repo_url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")


@dataclass(frozen=True)
class CorpusOverrides:
    """Exclusions and branch overrides keyed by repo URL.

    `exclude` maps url → reason (recorded in __skipped__). `branch_overrides`
    maps url → branch name (clone that branch instead of the CSV-provided one
    or the repo's default). `sub_charm_excludes` maps url → {sub_path → reason}
    and is applied AFTER monorepo fan-out, to drop specific sub-charms (e.g.
    test fixtures shipped alongside a real example charm).
    """

    exclude: dict[str, str]
    branch_overrides: dict[str, str]
    sub_charm_excludes: dict[str, dict[str, str]] = field(default_factory=dict)
    feature_excludes: dict[tuple[str, str], dict[str, str]] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> CorpusOverrides:
        return cls(exclude={}, branch_overrides={}, sub_charm_excludes={}, feature_excludes={})

    def apply(self, ref: CharmRef) -> tuple[CharmRef | None, str | None]:
        """Return ``(adjusted_ref, skip_reason)``.

        - If the URL is excluded, returns ``(None, reason)``.
        - If a branch override applies, returns ``(ref_with_branch, None)``.
        - Otherwise returns ``(ref, None)``.
        """
        if ref.repo_url in self.exclude:
            return None, self.exclude[ref.repo_url]
        branch = self.branch_overrides.get(ref.repo_url)
        if branch and branch != ref.branch:
            adjusted = CharmRef(
                team=ref.team,
                name=ref.name,
                repo_url=ref.repo_url,
                key_charm=ref.key_charm,
                branch=branch,
                notes=ref.notes,
            )
            return adjusted, None
        return ref, None

    def sub_charm_skip_reason(self, repo_url: str, sub_path: str) -> str | None:
        """Return the override reason if this sub-charm is excluded, else None.

        ``sub_path`` is the path relative to the repo root, e.g. "charms/foo"
        or just "noticestest". Match is exact (string equality).
        """
        return self.sub_charm_excludes.get(repo_url, {}).get(sub_path)

    def feature_skip_reason(self, repo_url: str, sub_path: str, feature_name: str) -> str | None:
        """Return the override reason for a specific (charm, feature) pair.

        Use ``sub_path=""`` for single-charm repos. Match is exact.
        """
        return self.feature_excludes.get((repo_url, sub_path), {}).get(feature_name)


def load(path: Path) -> list[CharmRef]:
    out: list[CharmRef] = []
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            repo = (row.get("Repository") or "").strip()
            if not repo:
                continue
            name = (row.get("Charm Name") or "").strip()
            if not name:
                name = repo.rstrip("/").removesuffix(".git").rsplit("/", 1)[-1]
            out.append(
                CharmRef(
                    team=(row.get("Team") or "").strip(),
                    name=name,
                    repo_url=repo,
                    key_charm=(row.get("Key Charm for this Team") or "").strip().upper() == "TRUE",
                    branch=(row.get("Branch (if not the default)") or "").strip() or None,
                    notes=(row.get("Notes") or "").strip(),
                )
            )
    return out


def fetch_to(url: str, dest: Path) -> Path:
    """Download ``url`` to ``dest`` (creating parents). Returns ``dest``.

    Stdlib-only so the scanner has no extra network dep. Use this to materialise
    the hyrum CSV under a workdir before calling :func:`load`.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310
        body = resp.read()
    dest.write_bytes(body)
    return dest


def load_overrides(path: Path) -> CorpusOverrides:
    """Load corpus-overrides.yaml. Returns an empty override set if path missing."""
    if not path.exists():
        return CorpusOverrides.empty()
    data = yaml.safe_load(path.read_text()) or {}
    excludes: dict[str, str] = {}
    for entry in data.get("exclude") or []:
        url = entry.get("url")
        reason = entry.get("reason", "excluded by corpus-overrides.yaml")
        if url:
            excludes[url] = reason
    branches: dict[str, str] = {}
    for entry in data.get("branch_overrides") or []:
        url = entry.get("url")
        branch = entry.get("branch")
        if url and branch:
            branches[url] = branch
    sub_excludes: dict[str, dict[str, str]] = {}
    for entry in data.get("sub_charm_excludes") or []:
        url = entry.get("url")
        sub_paths = entry.get("sub_paths") or []
        reason = entry.get("reason", "excluded by corpus-overrides.yaml")
        if not url or not sub_paths:
            continue
        bucket = sub_excludes.setdefault(url, {})
        for sp in sub_paths:
            bucket[sp] = reason
    feat_excludes: dict[tuple[str, str], dict[str, str]] = {}
    for entry in data.get("feature_excludes") or []:
        url = entry.get("url")
        sub_path = entry.get("sub_path", "") or ""
        features = entry.get("features") or []
        reason = entry.get("reason", "excluded by corpus-overrides.yaml")
        if not url or not features:
            continue
        bucket = feat_excludes.setdefault((url, sub_path), {})
        for fn in features:
            bucket[fn] = reason
    return CorpusOverrides(
        exclude=excludes,
        branch_overrides=branches,
        sub_charm_excludes=sub_excludes,
        feature_excludes=feat_excludes,
    )
