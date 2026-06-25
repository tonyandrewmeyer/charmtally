"""Read charm metadata (charmcraft.yaml or metadata.yaml) into a normalised dict.

Two kinds of fact are surfaced:

Per-charm signals used by scoring rules:
    has_containers        — K8s workload charm (has `containers:` block)
    relations             — list of {name, role, interface} from provides/requires/peers
    config_keys           — list of config option names
    secret_like_config    — names matching *-password / *-token / *-secret / *-key
    secret_typed_config   — names of config options declared `type: secret`
                            (Juju 3.3+ — Juju resolves the secret URI before
                            handing the value to the charm)
    has_integration_tests — tests/integration/ contains at least one .py file
    is_reactive           — charm uses the charms.reactive framework
    is_legacy_classic     — charm uses the pre-ops hooks-driven layout
                            (hooks/ directory present, no src/charm.py).
                            Pre-dates the ops framework; ops.* / pebble.* /
                            charmlibs.* features don't apply, same as
                            is_reactive.
    is_subordinate        — `subordinate: true` at root of charmcraft.yaml
                            or metadata.yaml. Surfaces as a dashboard chip;
                            not a scoring suppressor in v1.
    is_workload_less      — principal charm that manages no processes:
                            no `containers:`, no pebble layer usage (call
                            or *layer*.yaml), and no juju-info requires
                            binding (which would mark a subordinate).
                            Descriptive chip; not a scoring suppressor.
                            Misses machine charms that drive processes
                            via systemd/apt/subprocess.

Descriptive facts surfaced for the dashboard (no scoring rules attached):
    charm_name            — declared name from charmcraft.yaml / metadata.yaml
    charmcraft_plugins    — distinct plugins under parts.*.plugin (uv, python,
                            charm, poetry, ...) — modern-stack signal
    bases                 — base/bases entries (e.g. ubuntu@22.04)
    min_juju_version      — Juju version asserted in `assumes:` (or None)
    library_count         — distinct lib/charms/<libname>/ subdirs imported
    library_names         — the same set, by name (used by pair detection
                            to spot k8s/machine pairs sharing a charmlib)
    provides_own_library  — true if lib/charms/<charm_name>/ exists
    has_terraform_module  — true if terraform/ dir or .tf files at root
    tooling               — subset of ["tox","make","just"] based on
                            tox.ini / Makefile / justfile presence
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

_SECRETY = re.compile(r"(password|token|secret|api[-_]?key)$", re.IGNORECASE)
# Matches "juju >= 3.4", "juju>=3.4", "juju 3.4", etc. Captures the version.
_JUJU_VERSION = re.compile(r"juju\s*[>=]*\s*(\d+(?:\.\d+)*)", re.IGNORECASE)
# `pebble.Layer(...)` or `pebble.LayerDict(...)` construction in src/ —
# strong signal that the charm drives a workload via pebble.
_PEBBLE_LAYER_CALL = re.compile(r"\bpebble\.Layer(Dict)?\s*\(")


@dataclass(frozen=True)
class Relation:
    name: str
    role: str  # provides | requires | peers
    interface: str


@dataclass(frozen=True)
class CharmMeta:
    has_containers: bool
    relations: tuple[Relation, ...]
    config_keys: tuple[str, ...]
    secret_like_config: tuple[str, ...]
    secret_typed_config: tuple[str, ...]
    has_integration_tests: bool
    is_reactive: bool
    is_legacy_classic: bool = False
    is_subordinate: bool = False
    is_workload_less: bool = False
    # Descriptive facts (no scoring rules) — surfaced for the dashboard
    # so users can cluster/filter by stack and tooling choices.
    charm_name: str | None = None
    charmcraft_plugins: tuple[str, ...] = ()
    bases: tuple[str, ...] = ()
    min_juju_version: str | None = None
    library_count: int = 0
    library_names: tuple[str, ...] = ()
    provides_own_library: bool = False
    has_terraform_module: bool = False
    tooling: tuple[str, ...] = ()


def _load_yaml(path: Path) -> dict | None:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))
    except (yaml.YAMLError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _find_metadata_files(charm_root: Path) -> list[Path]:
    """Return any charmcraft.yaml or metadata.yaml files found in the tree.

    Excludes vendored libs and test fixtures so we don't pick up nested charms.
    """
    out: list[Path] = []
    for name in ("charmcraft.yaml", "metadata.yaml"):
        for p in charm_root.rglob(name):
            parts = p.relative_to(charm_root).parts
            if "lib" in parts and "charms" in parts:
                continue
            if any(d in parts for d in ("tests", "test")):
                continue
            out.append(p)
    return out


def _extract_charmcraft_plugins(data: dict) -> list[str]:
    """Distinct part plugins declared in charmcraft.yaml `parts:`."""
    parts = data.get("parts") or {}
    if not isinstance(parts, dict):
        return []
    plugins: list[str] = []
    for body in parts.values():
        if isinstance(body, dict):
            plugin = body.get("plugin")
            if isinstance(plugin, str) and plugin not in plugins:
                plugins.append(plugin)
    return plugins


def _extract_bases(data: dict) -> list[str]:
    """Normalise charmcraft bases across v1 (bases:) and v2 (base:) layouts."""
    out: list[str] = []
    # charmcraft v2 single base form: `base: ubuntu@22.04` or `base: ubuntu@24.04`
    base = data.get("base")
    if isinstance(base, str):
        out.append(base)
    # charmcraft v2 `build-base:` for non-LTS dev bases.
    build_base = data.get("build-base")
    if isinstance(build_base, str) and build_base not in out:
        out.append(build_base)
    # charmcraft v1 bases list.
    bases = data.get("bases") or []
    if isinstance(bases, list):
        for entry in bases:
            if isinstance(entry, dict):
                name = entry.get("name") or ""
                channel = entry.get("channel") or ""
                if name and channel:
                    s = f"{name}@{channel}"
                    if s not in out:
                        out.append(s)
            elif isinstance(entry, str) and entry not in out:
                out.append(entry)
    return out


def _extract_min_juju_version(data: dict) -> str | None:
    """Strip a Juju version assertion out of charmcraft.yaml `assumes:`.

    Returns the earliest version mentioned, or None if no `juju ...`
    expression is found. Doesn't try to combine clauses across nested
    any-of / all-of blocks — just scans the flattened textual content.
    """
    assumes = data.get("assumes")
    if not assumes:
        return None
    # Flatten to a string and regex-extract. Works for the common shapes:
    #   assumes: ["juju >= 3.4"]
    #   assumes: [juju >= 3.4, k8s-api]
    #   assumes: [{all-of: ["juju >= 3.4"]}]
    text = yaml.safe_dump(assumes)
    matches = _JUJU_VERSION.findall(text)
    if not matches:
        return None

    # Pick the lowest version mentioned (the asserted minimum).
    def _key(v: str) -> tuple[int, ...]:
        try:
            return tuple(int(p) for p in v.split("."))
        except ValueError:
            return (0,)

    return min(matches, key=_key)


def _has_pebble_layer_evidence(charm_root: Path) -> bool:
    """True if the charm constructs a pebble layer in src/ or ships a layer YAML.

    Two signals (either is sufficient):
      * `pebble.Layer(` / `pebble.LayerDict(` call in any `src/**/*.py` file.
      * A `*layer*.yaml` file under the charm root or `src/` (case-insensitive,
        excluding `tests/`, `lib/`, `.git/`).
    """
    src = charm_root / "src"
    if src.is_dir():
        for py in src.rglob("*.py"):
            try:
                text = py.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if _PEBBLE_LAYER_CALL.search(text):
                return True
    skip_dirs = {"tests", "test", "lib", ".git"}
    for yml in charm_root.rglob("*.yaml"):
        rel_parts = yml.relative_to(charm_root).parts
        if any(p in skip_dirs for p in rel_parts):
            continue
        if "layer" in yml.name.lower():
            return True
    return False


def _extract_relations(data: dict) -> list[Relation]:
    out: list[Relation] = []
    for role in ("provides", "requires", "peers"):
        block = data.get(role) or {}
        if not isinstance(block, dict):
            continue
        for name, info in block.items():
            iface = (info or {}).get("interface", "") if isinstance(info, dict) else ""
            out.append(Relation(name=name, role=role, interface=iface))
    return out


def read(charm_root: Path) -> CharmMeta:
    has_containers = False
    is_subordinate = False
    relations: list[Relation] = []
    config_keys: list[str] = []
    secret_typed: list[str] = []
    charm_name: str | None = None
    plugins: list[str] = []
    bases: list[str] = []
    min_juju: str | None = None

    for meta_path in _find_metadata_files(charm_root):
        data = _load_yaml(meta_path)
        if not data:
            continue
        if isinstance(data.get("containers"), dict) and data["containers"]:
            has_containers = True
        # YAML loads `subordinate: true` as bool True; tolerate the string
        # form for hand-edited metadata.yaml files.
        sub = data.get("subordinate")
        if sub is True or (isinstance(sub, str) and sub.strip().lower() == "true"):
            is_subordinate = True
        relations.extend(_extract_relations(data))
        cfg = data.get("config") or {}
        opts = cfg.get("options") if isinstance(cfg, dict) else None
        if isinstance(opts, dict):
            config_keys.extend(opts.keys())
            for name, body in opts.items():
                if isinstance(body, dict) and body.get("type") == "secret":
                    secret_typed.append(name)
        # Descriptive facts — first non-empty value wins (charmcraft.yaml
        # takes precedence over metadata.yaml in iteration order).
        if charm_name is None:
            n = data.get("name")
            if isinstance(n, str):
                charm_name = n
        for pl in _extract_charmcraft_plugins(data):
            if pl not in plugins:
                plugins.append(pl)
        for b in _extract_bases(data):
            if b not in bases:
                bases.append(b)
        if min_juju is None:
            min_juju = _extract_min_juju_version(data)

    # De-duplicate (a charm can have both charmcraft.yaml and metadata.yaml
    # listing the same relation/config).
    seen_rel: set[tuple[str, str]] = set()
    deduped_rel: list[Relation] = []
    for r in relations:
        key = (r.name, r.role)
        if key not in seen_rel:
            seen_rel.add(key)
            deduped_rel.append(r)
    config_keys = list(dict.fromkeys(config_keys))
    secret_typed = list(dict.fromkeys(secret_typed))

    integration_dir = charm_root / "tests" / "integration"
    has_integration_tests = integration_dir.exists() and any(integration_dir.rglob("*.py"))

    # Reactive charms (charms.reactive framework) come in two layouts:
    #
    #   canonical: layer.yaml at root + reactive/<handlers>.py
    #     (or a Python package: reactive/<name>/<handlers>.py — fix #12)
    #
    #   OpenStack: osci.yaml at root + src/reactive/<handlers>.py
    #     (the opendev.org/openstack/charm-* family doesn't ship layer.yaml
    #     and nests handlers under src/. CALIBRATION #14.)
    #
    # Either layout: rule out ops.* / pebble.* / charmlibs.* features; the
    # scoring layer short-circuits to not-applicable.
    has_reactive_indicator = (charm_root / "layer.yaml").is_file() or (charm_root / "osci.yaml").is_file()
    reactive_dirs = (
        charm_root / "reactive",
        charm_root / "src" / "reactive",
    )
    has_reactive_handlers = any(d.is_dir() and any(d.rglob("*.py")) for d in reactive_dirs)
    is_reactive = has_reactive_indicator and has_reactive_handlers

    # Legacy classic-charm layout (pre-ops): `hooks/` directory at charm root
    # with at least one executable hook script, and no `src/charm.py` modern
    # entry point. Surfaced by CALIBRATION #15: openstack-charmers
    # `charm-helpers` charms (charm-ceilometer, charm-cinder, etc.) and a
    # handful of IS team charms (container-log-archive, ubuntu-mirror) share
    # this shape. None of the ops.* / pebble.* / charmlibs.* features apply.
    has_legacy_hooks_dir = (charm_root / "hooks").is_dir() and any((charm_root / "hooks").iterdir())
    has_modern_entry = (charm_root / "src" / "charm.py").is_file()
    is_legacy_classic = has_legacy_hooks_dir and not has_modern_entry and not is_reactive

    # Charmhub-hosted libraries vendored under lib/charms/<libname>/...
    lib_root = charm_root / "lib" / "charms"
    library_names: list[str] = []
    if lib_root.is_dir():
        for entry in lib_root.iterdir():
            if entry.is_dir():
                library_names.append(entry.name)
    library_count = len(library_names)
    provides_own_library = bool(charm_name and (lib_root / charm_name.replace("-", "_")).is_dir())

    # Workload-less classification: a principal charm that drives
    # no processes. All three negatives must hold:
    #   1. No `containers:` block (would imply k8s workload).
    #   2. No pebble layer evidence (call in src/ or *layer*.yaml).
    #   3. No juju-info requires binding
    # Known miss: machine charms managing processes via systemd / apt /
    # subprocess — left as a future refinement, not chased in v1.
    has_juju_info_requires = any(r.role == "requires" and r.interface == "juju-info" for r in deduped_rel)
    is_workload_less = not has_containers and not _has_pebble_layer_evidence(charm_root) and not has_juju_info_requires

    # Terraform module convention: a `terraform/` directory at root holding
    # the charm's published TF module. Some charms put .tf at root instead.
    has_terraform_module = (charm_root / "terraform").is_dir() or any(charm_root.glob("*.tf"))

    tooling: list[str] = []
    if (charm_root / "tox.ini").is_file():
        tooling.append("tox")
    if (charm_root / "Makefile").is_file() or (charm_root / "makefile").is_file():
        tooling.append("make")
    if (charm_root / "justfile").is_file() or (charm_root / "Justfile").is_file():
        tooling.append("just")

    return CharmMeta(
        has_containers=has_containers,
        relations=tuple(deduped_rel),
        config_keys=tuple(config_keys),
        secret_like_config=tuple(k for k in config_keys if _SECRETY.search(k)),
        secret_typed_config=tuple(secret_typed),
        has_integration_tests=has_integration_tests,
        is_reactive=is_reactive,
        is_legacy_classic=is_legacy_classic,
        is_subordinate=is_subordinate,
        is_workload_less=is_workload_less,
        charm_name=charm_name,
        charmcraft_plugins=tuple(plugins),
        bases=tuple(bases),
        min_juju_version=min_juju,
        library_count=library_count,
        library_names=tuple(library_names),
        provides_own_library=provides_own_library,
        has_terraform_module=has_terraform_module,
        tooling=tuple(tooling),
    )
