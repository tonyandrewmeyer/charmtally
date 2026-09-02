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
    ops_requirement       — the `ops` dependency as the charm asks for it
                            (">=2.15", "==2.4.1", "" for an unpinned `ops`),
                            or None when nothing declares one. Empty string
                            and None are different findings: the first is a
                            charm that takes whatever `ops` it gets, the
                            second is a charm we could not read a dependency
                            out of at all
    ops_requirement_source— which file it was read from (`requirements.txt`,
                            `pyproject.toml`, `uv.lock`, ...), or None
    ops_min_version       — the lower bound implied by that requirement, so a
                            feature row can ask "could this charm even have
                            this API?"; None for an unpinned or upper-bound-
                            only requirement, which asserts no minimum
    charm_user            — `charm-user:` from charmcraft.yaml: root / sudoer
                            / non-root, or None when unset (Juju's default,
                            i.e. root). Only affects Kubernetes charms on
                            Juju 3.6+, so read it against has_containers
    library_count         — distinct lib/charms/<libname>/ subdirs the charm
                            USES: its own published library (the subdir named
                            after the charm) is excluded, so publishing a lib
                            never reads as consuming one
    library_names         — the same set, by name (used by pair detection
                            to spot k8s/machine pairs sharing a charmlib)
    charmlibs_count       — distinct `charmlibs` namespace packages used
    charmlibs_names       — the same set, by name (`pathops`, `apt`,
                            `interfaces.tls`, ...). The PyPI charmlibs, not
                            the vendored Charmhub libs above; the adoption
                            dashboard divides one by the other.
    provides_own_library  — true if lib/charms/<charm_name>/ exists; this is
                            where a published library is reported, since
                            library_names excludes it
    has_terraform_module  — true if terraform/ dir or .tf files at root
    tooling               — subset of ["tox","make","just"] based on
                            tox.ini / Makefile / justfile presence
    repo_sha              — commit the charm was scanned at, or None when the
                            charm root isn't a git checkout

`CharmMeta.to_dict` / `CharmMeta.from_dict` are the round-trip pair used to
write and re-read the per-charm ``__meta__`` block in results.json.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import yaml

from ._toml import tomllib

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_SECRETY = re.compile(r"(password|token|secret|api[-_]?key)$", re.IGNORECASE)
# Matches "juju >= 3.4", "juju>=3.4", "juju 3.4", etc. Captures the version.
_JUJU_VERSION = re.compile(r"juju\s*[>=]*\s*(\d+(?:\.\d+)*)", re.IGNORECASE)
# `pebble.Layer(...)` or `pebble.LayerDict(...)` construction in src/ —
# strong signal that the charm drives a workload via pebble.
_PEBBLE_LAYER_CALL = re.compile(r"\bpebble\.Layer(Dict)?\s*\(")
# Imports of the `charmlibs` namespace package — the libraries charm-tech
# publishes to PyPI (`charmlibs-pathops`, `charmlibs-apt`,
# `charmlibs-interfaces-*`, ...). Deliberately NOT the same thing as the
# `charmlibs.*` features in features.yaml, which pre-date the namespace and
# match vendored Charmhub libs under `lib/charms/`.
_CHARMLIBS_FROM = re.compile(r"\bfrom\s+charmlibs(\.[\w.]+)?\s+import\s+(\([^)]*\)|[^\n]+)")
_CHARMLIBS_IMPORT = re.compile(r"^\s*import\s+charmlibs\.([\w.]+)", re.MULTILINE)
# `charmlibs-pathops`, `charmlibs_interfaces_tls_certificates`, ... as spelled
# in a requirements file or a pyproject dependency list.
_CHARMLIBS_REQUIREMENT = re.compile(r"\bcharmlibs[-_]([A-Za-z0-9][\w.-]*)")
# `ops` as spelled in a requirements file or a PEP 508 dependency string:
# the name, optional extras, then the specifier. The specifier group has to
# start with an operator (or be empty) so `ops-scenario>=7` and `opslib-foo`
# don't read as an `ops` requirement with a nonsense version.
_OPS_REQUIREMENT = re.compile(r"^ops\s*(?:\[[^\]]*\])?\s*(([<>=!~^@].*)?)$", re.IGNORECASE)
# Lower bounds inside a specifier set. `>` and `<` are deliberately absent:
# `>2.14` asserts a minimum somewhere above 2.14, and recording 2.14 would
# be a version the charm has excluded.
_VERSION_LOWER_BOUND = re.compile(r"(?:>=|===|==|~=|\^)\s*v?(\d+(?:\.\d+)*)")
# Directories a source walk must not descend into when looking for imports.
_SKIP_WALK_DIRS = {".git", ".tox", ".venv", "venv", "node_modules", "build", "dist", "lib"}


@dataclass(frozen=True)
class Relation:
    """A relation declared in a charm's metadata."""

    name: str
    role: str  # provides | requires | peers
    interface: str


@dataclass(frozen=True)
class CharmMeta:
    """Facts read from a charm's metadata and source, used for scoring."""

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
    ops_requirement: str | None = None
    ops_requirement_source: str | None = None
    ops_min_version: str | None = None
    charm_user: str | None = None
    library_count: int = 0
    library_names: tuple[str, ...] = ()
    charmlibs_count: int = 0
    charmlibs_names: tuple[str, ...] = ()
    provides_own_library: bool = False
    has_terraform_module: bool = False
    tooling: tuple[str, ...] = ()
    # Commit the charm was scanned at (``git rev-parse HEAD`` of the clone),
    # or None for a working tree that isn't a git checkout. Used to build
    # permalinks in the dashboard and to key the LLM verdict cache.
    repo_sha: str | None = None

    def to_dict(self) -> dict:
        """Serialise to the ``__meta__`` block written into results.json.

        Paired with :meth:`from_dict`; keep the two in sync — the scan and
        score commands round-trip through this pair, and every consumer of
        ``__meta__`` (dashboard, llm_score, pairs) reads these key names.
        """
        return {
            "has_containers": self.has_containers,
            "relations": [
                {"name": r.name, "role": r.role, "interface": r.interface} for r in self.relations
            ],
            "config_keys": list(self.config_keys),
            "secret_like_config": list(self.secret_like_config),
            "secret_typed_config": list(self.secret_typed_config),
            "has_integration_tests": self.has_integration_tests,
            "is_reactive": self.is_reactive,
            "is_legacy_classic": self.is_legacy_classic,
            "is_subordinate": self.is_subordinate,
            "is_workload_less": self.is_workload_less,
            "charm_name": self.charm_name,
            "charmcraft_plugins": list(self.charmcraft_plugins),
            "bases": list(self.bases),
            "min_juju_version": self.min_juju_version,
            "ops_requirement": self.ops_requirement,
            "ops_requirement_source": self.ops_requirement_source,
            "ops_min_version": self.ops_min_version,
            "charm_user": self.charm_user,
            "library_count": self.library_count,
            "library_names": list(self.library_names),
            "charmlibs_count": self.charmlibs_count,
            "charmlibs_names": list(self.charmlibs_names),
            "provides_own_library": self.provides_own_library,
            "has_terraform_module": self.has_terraform_module,
            "tooling": list(self.tooling),
            "repo_sha": self.repo_sha,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> CharmMeta:
        """Rebuild a CharmMeta from a ``__meta__`` block.

        Tolerant of missing keys so snapshots written by older scans still
        load — every field falls back to the dataclass default.
        """
        return cls(
            has_containers=bool(raw.get("has_containers")),
            relations=tuple(
                Relation(r["name"], r["role"], r.get("interface", ""))
                for r in raw.get("relations") or []
            ),
            config_keys=tuple(raw.get("config_keys") or []),
            secret_like_config=tuple(raw.get("secret_like_config") or []),
            secret_typed_config=tuple(raw.get("secret_typed_config") or []),
            has_integration_tests=bool(raw.get("has_integration_tests")),
            is_reactive=bool(raw.get("is_reactive")),
            is_legacy_classic=bool(raw.get("is_legacy_classic")),
            is_subordinate=bool(raw.get("is_subordinate")),
            is_workload_less=bool(raw.get("is_workload_less")),
            charm_name=raw.get("charm_name"),
            charmcraft_plugins=tuple(raw.get("charmcraft_plugins") or []),
            bases=tuple(raw.get("bases") or []),
            min_juju_version=raw.get("min_juju_version"),
            ops_requirement=raw.get("ops_requirement"),
            ops_requirement_source=raw.get("ops_requirement_source"),
            ops_min_version=raw.get("ops_min_version"),
            charm_user=raw.get("charm_user"),
            library_count=int(raw.get("library_count", 0)),
            library_names=tuple(raw.get("library_names") or []),
            charmlibs_count=int(raw.get("charmlibs_count", 0)),
            charmlibs_names=tuple(raw.get("charmlibs_names") or []),
            provides_own_library=bool(raw.get("provides_own_library")),
            has_terraform_module=bool(raw.get("has_terraform_module")),
            tooling=tuple(raw.get("tooling") or []),
            repo_sha=raw.get("repo_sha"),
        )


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


def declared_name(charm_root: Path) -> str | None:
    """Return the charm's declared `name:`, or None if no metadata declares one.

    Same first-non-empty-wins rule as :func:`read`, minus the rest of the
    metadata scan — `detectors` needs only the name, to tell the charm that
    *provides* a charm library from the many that merely vendor one.
    """
    for meta_path in _find_metadata_files(charm_root):
        data = _load_yaml(meta_path)
        if not data:
            continue
        name = data.get("name")
        if isinstance(name, str):
            return name
    return None


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
    """Return true if the charm constructs a pebble layer in src/ or ships a layer YAML.

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


def _charmlibs_names(charm_root: Path) -> list[str]:
    """Return the distinct `charmlibs` packages this charm uses, sorted.

    Two signals, unioned:
      * an import of the `charmlibs` namespace package in any Python file
        under the charm root (`from charmlibs import pathops`,
        `from charmlibs.interfaces.tls import ...`, `import charmlibs.apt`);
      * a `charmlibs-*` / `charmlibs_*` requirement in a requirements file,
        `pyproject.toml`, or `charmcraft.yaml`.

    Names are normalised to the dotted sub-path under `charmlibs`, with the
    `interfaces` namespace keeping its second segment (`interfaces.tls`) so
    two different interface libs don't collapse into one. A requirement
    spelling is normalised the same way: `charmlibs-interfaces-tls` →
    `interfaces.tls`.

    Text-matched rather than AST-walked: this runs inside `read()`, before
    the scan builds its shared `CharmSource`, and a second full parse of
    every charm just to count imports is not worth the seconds.
    """
    names: set[str] = set()

    for py in _walk_python(charm_root):
        try:
            text = py.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "charmlibs" not in text:
            continue  # cheap pre-filter: the regexes are the expensive part
        for sub, imported in _CHARMLIBS_FROM.findall(text):
            if sub:
                names.add(_normalise_charmlib(sub.lstrip(".")))
                continue
            # `from charmlibs import pathops, apt as _apt` — every name in
            # the import list is itself a charmlibs package.
            for part in imported.strip("()").split(","):
                symbol = part.strip().split(" as ")[0].strip()
                if symbol and symbol.isidentifier():
                    names.add(symbol)
        for sub in _CHARMLIBS_IMPORT.findall(text):
            names.add(_normalise_charmlib(sub))

    for rel in ("pyproject.toml", "charmcraft.yaml", "requirements.txt"):
        path = charm_root / rel
        if path.is_file():
            names.update(_charmlibs_requirements(path))
    for req in charm_root.glob("requirements*.txt"):
        names.update(_charmlibs_requirements(req))

    return sorted(names)


def _normalise_charmlib(dotted: str) -> str:
    """Collapse a dotted sub-path to the package it identifies.

    `pathops.core` → `pathops`; `interfaces.tls.v1` → `interfaces.tls`. The
    `interfaces` namespace keeps two segments because its first segment is
    shared by every interface library.
    """
    parts = [p for p in dotted.split(".") if p]
    if not parts:
        return ""
    if parts[0] == "interfaces" and len(parts) > 1:
        return f"interfaces.{parts[1]}"
    return parts[0]


def _charmlibs_requirements(path: Path) -> set[str]:
    """Extract `charmlibs-*` distribution names declared in a dependency file."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    if "charmlibs" not in text:
        return set()
    out: set[str] = set()
    for raw in _CHARMLIBS_REQUIREMENT.findall(text):
        # Strip any version specifier the regex's trailing class swallowed,
        # then read the distribution suffix as a dotted package path.
        dist = re.split(r"[<>=!~\[]", raw)[0].strip().rstrip("-_.")
        normalised = _normalise_charmlib(dist.replace("-", ".").replace("_", "."))
        if normalised:
            out.add(normalised)
    return out


def _parse_ops_requirement(requirement: str) -> str | None:
    """Return the specifier from a single `ops` requirement, or None if it isn't one.

    An `ops` with no specifier returns `""` — the charm asks for `ops` and
    takes whatever it gets, which is a finding rather than a missing value.
    """
    stripped = requirement.split(";", 1)[0].strip()  # drop any environment marker
    m = _OPS_REQUIREMENT.match(stripped)
    if not m:
        return None
    return m.group(1).strip()


def _ops_from_requirements_text(text: str) -> str | None:
    """Find an `ops` requirement in a pip requirements file's contents."""
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        spec = _parse_ops_requirement(line)
        if spec is not None:
            return spec
    return None


def _ops_from_pyproject(data: dict) -> str | None:
    """Find an `ops` requirement in a parsed pyproject.toml.

    Covers both the PEP 621 `project.dependencies` list and poetry's
    `tool.poetry.dependencies` table, where the version is the value rather
    than part of the string.
    """
    project = data.get("project")
    if isinstance(project, dict):
        deps = project.get("dependencies")
        if isinstance(deps, list):
            for dep in deps:
                if isinstance(dep, str):
                    spec = _parse_ops_requirement(dep)
                    if spec is not None:
                        return spec
    poetry_deps = ((data.get("tool") or {}).get("poetry") or {}).get("dependencies")
    if isinstance(poetry_deps, dict):
        for name, body in poetry_deps.items():
            if name.lower() != "ops":
                continue
            if isinstance(body, str):
                return body.strip()
            if isinstance(body, dict) and isinstance(body.get("version"), str):
                return body["version"].strip()
            return ""
    return None


def _ops_from_uv_lock(data: dict) -> str | None:
    """Find the resolved `ops` version in a parsed uv.lock.

    Returned as `==<version>` so a lockfile entry parses for a minimum the
    same way a declared requirement does. It is what the charm *gets*, not
    what it asks for, which is why it is only consulted when nothing
    declares an `ops` dependency and why the source is recorded alongside.
    """
    packages = data.get("package")
    if not isinstance(packages, list):
        return None
    for pkg in packages:
        if not isinstance(pkg, dict) or str(pkg.get("name", "")).lower() != "ops":
            continue
        version = pkg.get("version")
        return f"=={version}" if isinstance(version, str) and version else ""
    return None


def _read_ops_requirement(charm_root: Path) -> tuple[str, str] | None:
    """Return (specifier, filename) for the charm's `ops` dependency, or None.

    Declared requirements win over the lockfile: the question is what the
    charm asks for, and `uv.lock` answers a different one. `requirements.txt`
    is checked before any other `requirements*.txt` because the variants are
    usually test/dev sets.
    """
    req_files = [charm_root / "requirements.txt"]
    req_files += sorted(p for p in charm_root.glob("requirements*.txt") if p not in req_files)
    for path in req_files:
        text = _read_text(path)
        if text is None:
            continue
        spec = _ops_from_requirements_text(text)
        if spec is not None:
            return spec, path.name

    for name, extract in (("pyproject.toml", _ops_from_pyproject), ("uv.lock", _ops_from_uv_lock)):
        path = charm_root / name
        text = _read_text(path)
        if text is None:
            continue
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            continue
        spec = extract(data)
        if spec is not None:
            return spec, name
    return None


def _min_version(specifier: str) -> str | None:
    """Return the highest lower bound asserted by a specifier set, or None.

    None means the requirement asserts no minimum — it is unpinned, or has
    only an upper bound — not that it could not be read.
    """
    bounds = _VERSION_LOWER_BOUND.findall(specifier)
    if not bounds:
        return None
    return max(bounds, key=lambda v: tuple(int(p) for p in v.split(".")))


def _read_text(path: Path) -> str | None:
    """Return a file's contents, or None if it isn't a readable file."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _walk_python(charm_root: Path) -> Iterator[Path]:
    """Yield the charm's own Python files, skipping vendored / build trees.

    `lib/` is skipped along with the usual build dirs: vendored Charmhub
    libraries are other people's code, and their imports say nothing about
    what this charm adopted.
    """
    for path in charm_root.rglob("*.py"):
        rel = path.relative_to(charm_root).parts
        if any(part in _SKIP_WALK_DIRS for part in rel[:-1]):
            continue
        yield path


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
    """Read the metadata facts for the charm rooted at charm_root."""
    has_containers = False
    is_subordinate = False
    relations: list[Relation] = []
    config_keys: list[str] = []
    secret_typed: list[str] = []
    charm_name: str | None = None
    plugins: list[str] = []
    bases: list[str] = []
    min_juju: str | None = None
    charm_user: str | None = None

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
        if charm_user is None:
            # charmcraft validates this against a literal enum, so anything
            # else in the file is a typo; record it as written rather than
            # normalising, so a typo shows up as its own bucket instead of
            # being counted as one of the real values.
            cu = data.get("charm-user")
            if isinstance(cu, str) and cu.strip():
                charm_user = cu.strip()

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
    # A third variant nests the indicator file itself under src/ alongside
    # the handlers (charmed-kubernetes' layer-* family, some openstack-
    # charmers charms): src/layer.yaml or src/osci.yaml, with no root-level
    # indicator at all. CALIBRATION #38/#39 follow-up #15.
    #
    # Either layout: rule out ops.* / pebble.* / charmlibs.* features; the
    # scoring layer short-circuits to not-applicable.
    has_reactive_indicator = (
        (charm_root / "layer.yaml").is_file()
        or (charm_root / "osci.yaml").is_file()
        or (charm_root / "src" / "layer.yaml").is_file()
        or (charm_root / "src" / "osci.yaml").is_file()
    )
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
    has_legacy_hooks_dir = (charm_root / "hooks").is_dir() and any(
        (charm_root / "hooks").iterdir()
    )
    has_modern_entry = (charm_root / "src" / "charm.py").is_file()
    is_legacy_classic = has_legacy_hooks_dir and not has_modern_entry and not is_reactive

    # Charmhub-hosted libraries vendored under lib/charms/<libname>/...
    #
    # A charm that PUBLISHES a library hosts it at `lib/charms/<own_name>/`,
    # the same place `charmcraft fetch-lib` drops a CONSUMED one. Counting the
    # charm's own package would make publishing look like consumption: it
    # inflates the library count, and it drags the charmlibs share down for
    # exactly the charms doing the most charm-tech work. Publication is
    # already reported separately as `provides_own_library`, so
    # `library_names` is the *used* set.
    lib_root = charm_root / "lib" / "charms"
    own_lib = charm_name.replace("-", "_") if charm_name else None
    library_names: list[str] = []
    if lib_root.is_dir():
        library_names.extend(
            entry.name for entry in lib_root.iterdir() if entry.is_dir() and entry.name != own_lib
        )
    library_count = len(library_names)

    # PyPI `charmlibs` namespace packages — the replacement for the vendored
    # libs above, and the numerator of the adoption dashboard's charmlibs share.
    charmlibs_names = _charmlibs_names(charm_root)

    provides_own_library = bool(own_lib and (lib_root / own_lib).is_dir())

    # Workload-less classification: a principal charm that drives
    # no processes. All three negatives must hold:
    #   1. No `containers:` block (would imply k8s workload).
    #   2. No pebble layer evidence (call in src/ or *layer*.yaml).
    #   3. No juju-info requires binding
    # Known miss: machine charms managing processes via systemd / apt /
    # subprocess — left as a future refinement, not chased in v1.
    has_juju_info_requires = any(
        r.role == "requires" and r.interface == "juju-info" for r in deduped_rel
    )
    is_workload_less = (
        not has_containers
        and not _has_pebble_layer_evidence(charm_root)
        and not has_juju_info_requires
    )

    # Terraform module convention: a `terraform/` directory at root holding
    # the charm's published TF module. Some charms put .tf at root instead.
    has_terraform_module = (charm_root / "terraform").is_dir() or any(charm_root.glob("*.tf"))

    # The `ops` the charm asks for. Feature rows that need a minimum `ops`
    # can read `ops_min_version` to tell a charm that declined an API from
    # one that is pinned below the release that has it.
    ops_req = _read_ops_requirement(charm_root)
    ops_requirement, ops_source = ops_req if ops_req else (None, None)
    ops_min = _min_version(ops_requirement) if ops_requirement else None

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
        ops_requirement=ops_requirement,
        ops_requirement_source=ops_source,
        ops_min_version=ops_min,
        charm_user=charm_user,
        library_count=library_count,
        library_names=tuple(library_names),
        charmlibs_count=len(charmlibs_names),
        charmlibs_names=tuple(charmlibs_names),
        provides_own_library=provides_own_library,
        has_terraform_module=has_terraform_module,
        tooling=tuple(tooling),
    )
