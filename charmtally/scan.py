"""Scan one or more checked-out charm directories against the feature catalogue."""

from __future__ import annotations

import subprocess
from dataclasses import asdict
from pathlib import Path

import yaml

from . import metadata, scoring
from .catalogue import Feature, Pattern
from .corpus import CharmRef
from .detectors import detect_feature

# Directory names a fan-out walk should never descend into when looking for
# sub-charms. Vendored charm libs ship `charmcraft.yaml` files for the lib
# itself; test fixtures sometimes do too. Both would inflate the fan-out.
_FAN_OUT_SKIP_DIRS = {
    "lib",
    "libs",
    "tests",
    "test",
    "node_modules",
    ".git",
    ".tox",
    ".venv",
    "venv",
    "build",
    "dist",
    "vendor",
}


def scan_charm(
    charm_root: Path,
    features: list[Feature],
    patterns: list[Pattern] | None = None,
) -> dict:
    """Return a dict of feature.name → {present, evidence, score, rationale}.

    Architecture patterns (if provided) are detected per-charm and surfaced
    in the per-charm ``__meta__`` block as ``architecture: [name, ...]``.
    The scoring layer uses these as inputs to per-feature gap rules.
    """
    meta = metadata.read(charm_root)
    architecture = _detect_architecture(charm_root, patterns or [])
    out: dict[str, dict] = {}
    # First pass: detection. Scoring needs the full features dict (rules can
    # reference cross-feature state), so we score in a second pass.
    for feat in features:
        ev = detect_feature(charm_root, feat)
        out[feat.name] = {
            "present": bool(ev),
            "evidence": [asdict(e) for e in ev],
        }
    for feat in features:
        rec = out[feat.name]
        if rec["present"]:
            note = scoring.annotate_present(feat.name, meta)
            if note:
                rec["score"] = note.label
                rec["rationale"] = note.rationale
        else:
            s = scoring.score_absent(feat.name, out, meta, architecture)
            rec["score"] = s.label
            rec["rationale"] = s.rationale
    out["__meta__"] = {
        "has_containers": meta.has_containers,
        "relations": [{"name": r.name, "role": r.role, "interface": r.interface} for r in meta.relations],
        "config_keys": list(meta.config_keys),
        "secret_like_config": list(meta.secret_like_config),
        "secret_typed_config": list(meta.secret_typed_config),
        "has_integration_tests": meta.has_integration_tests,
        "is_reactive": meta.is_reactive,
        "is_legacy_classic": meta.is_legacy_classic,
        "architecture": architecture,
        "charm_name": meta.charm_name,
        "charmcraft_plugins": list(meta.charmcraft_plugins),
        "bases": list(meta.bases),
        "min_juju_version": meta.min_juju_version,
        "library_count": meta.library_count,
        "provides_own_library": meta.provides_own_library,
        "has_terraform_module": meta.has_terraform_module,
        "tooling": list(meta.tooling),
    }
    return out


def _detect_architecture(charm_root: Path, patterns: list[Pattern]) -> list[str]:
    """Return the names of architecture patterns that match this charm.

    Pattern detection re-uses the per-feature detector machinery. A pattern
    matches if any of its detectors yields evidence — same `any` semantics
    as features.
    """
    matched: list[str] = []
    for pat in patterns:
        ev = detect_feature(charm_root, pat)  # duck-typed: needs scope + detectors
        if ev:
            matched.append(pat.name)
    return matched


def _is_bundle_charmcraft(path: Path) -> bool:
    """True if `path` is a charmcraft.yaml with ``type: bundle``.

    Bundles are deploy-time recipes, not charms — they have no src/, no
    handlers, no features to scan. A repo whose root charmcraft.yaml is a
    bundle typically ships its real charms under ``charms/`` (e.g.
    argo-operators, kfp-operators, istio-operators). Without this check
    we'd treat the bundle root as the only charm and miss every sub-charm.
    """
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))
    except (yaml.YAMLError, OSError):
        return False
    return isinstance(data, dict) and data.get("type") == "bundle"


def find_charm_roots(repo_root: Path) -> list[Path]:
    """Return the charm-root directories inside ``repo_root``.

    - Single-charm repo (charmcraft.yaml or metadata.yaml at the root):
      returns ``[repo_root]``.
    - Bundle-only root (root charmcraft.yaml has ``type: bundle`` and no
      root metadata.yaml): treated as a monorepo — descend into
      sub-directories to find the real charms.
    - Monorepo (no root charm file but sub-directories contain charm files):
      returns one path per sub-charm, deepest match wins so nested layer
      charms aren't double-counted by an ancestor's charmcraft.yaml.
    - No charm files anywhere: returns ``[]``. The caller treats this as
      a not-a-charm skip.

    Sub-charm detection prefers ``charmcraft.yaml`` over ``metadata.yaml``;
    a directory containing both counts once. Directories listed in
    ``_FAN_OUT_SKIP_DIRS`` are not descended into.
    """
    root_charmcraft = repo_root / "charmcraft.yaml"
    root_metadata = repo_root / "metadata.yaml"
    root_is_bundle = root_charmcraft.is_file() and _is_bundle_charmcraft(root_charmcraft)
    root_has_charm = (root_charmcraft.is_file() and not root_is_bundle) or root_metadata.is_file()
    if root_has_charm:
        return [repo_root]

    sub_roots: list[Path] = []

    def _walk(path: Path) -> None:
        try:
            entries = list(path.iterdir())
        except (OSError, PermissionError):
            return
        cc = path / "charmcraft.yaml"
        md = path / "metadata.yaml"
        # Same bundle-vs-charm logic at each level — a nested bundle (rare,
        # but valid) shouldn't terminate the walk.
        is_bundle = cc.is_file() and _is_bundle_charmcraft(cc)
        has_charm = (cc.is_file() and not is_bundle) or md.is_file()
        if has_charm:
            sub_roots.append(path)
            # Do not descend further: a layer charm with nested testing
            # fixtures shouldn't add more sub-roots under itself.
            return
        for e in entries:
            if not e.is_dir():
                continue
            if e.name in _FAN_OUT_SKIP_DIRS or e.name.startswith("."):
                continue
            _walk(e)

    _walk(repo_root)
    return sub_roots


def ensure_clone(ref: CharmRef, workdir: Path) -> Path | None:
    """Shallow-clone `ref.repo_url` into workdir/<slug>, returning the path.

    Returns None on clone failure. Uses HTTPS (no SSH key needed).
    """
    dest = workdir / ref.slug
    if dest.exists():
        return dest
    workdir.mkdir(parents=True, exist_ok=True)
    cmd = ["git", "clone", "--depth", "1"]
    if ref.branch:
        cmd += ["--branch", ref.branch]
    cmd += [ref.repo_url, str(dest)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return dest
