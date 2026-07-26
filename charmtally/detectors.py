"""Run a single feature's detectors against a charm tree.

Four kinds run per Python file in scope:
    import         — AST: matches `import X` and `from X import Y` (with optional names filter)
    call           — AST: matches `*.<attr>(...)` where <attr> is the trailing dotted suffix
    observe-event  — regex: matches `observe(... on.<snake_name>(...)|on['<snake_name>'] ...)`
                     for each given event class (translated CamelCase→snake_case,
                     dropping trailing 'Event')
    regex          — raw multiline regex over file contents

Three more run per Python file and back the architecture axis:
    ast-init-call               — a `self.X(...)` call inside `__init__`
    ast-observe-shared-handler  — one handler bound to N distinct events
    ast-shared-method           — N `_on_*` handlers delegating to one method

Four are file-independent: they read the charm root directly rather than the
Python files `_select_files` returns.
    yaml-key           — a mapping key present in YAML matching a glob
    pytest-config-key  — a pytest setting in pyproject/pytest.ini/setup.cfg/tox.ini
    requires-interface — an interface named in the metadata `requires:` block
    relation-count     — bucket the charm by its requires/provides/peers count
"""

from __future__ import annotations

import ast
import configparser
import re
import warnings
from dataclasses import dataclass

import yaml

# Cross-version shim: tomllib is stdlib from 3.11, tomli is a conditional
# dependency below that. Exactly one of these resolves on any given
# interpreter, so a type checker pinned to either version flags the other.
# Which one is unresolved depends on the interpreter the checker is run
# under — CI's 3.12 env has no tomli, a 3.10 dev env has no tomllib — so one
# of these two suppressions is always the redundant one. `unused-ignore-
# comment` is switched off for this file in pyproject.toml; it can't be done
# inline, because the inline form is itself reported as unused on whichever
# interpreter doesn't need it.
try:
    import tomllib  # ty: ignore[unresolved-import]
except ModuleNotFoundError:  # pragma: no cover — exercised only on 3.10
    import tomli as tomllib  # ty: ignore[unresolved-import]

from typing import TYPE_CHECKING

from . import metadata as _metadata

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from .catalogue import Detectable


@dataclass(frozen=True)
class Evidence:
    """A single detector hit: where it matched and the matching text."""

    file: str  # path relative to charm root
    line: int
    detector_kind: str
    snippet: str


# ── file selection ──────────────────────────────────────────────────────────────


_SRC_DIRS = ("src", "lib")
_TEST_DIRS = ("tests", "test")


def _is_vendored_lib(parts: tuple[str, ...]) -> bool:
    """`lib/charms/<libname>/...` is a vendored charm library, not charm code."""
    return any(parts[i] == "lib" and parts[i + 1] == "charms" for i in range(len(parts) - 1))


def _charm_provides_lib(charm_root: Path, module: str, charm_name: str | None) -> bool:
    """Return true if this charm is the *provider* of the given charms.<pkg> library.

    Prevents flagging the lib-provider charm when it imports its own library
    from src/ — e.g. grafana-agent importing charms.grafana_agent because it
    IS grafana-agent, not because it consumes the COS agent lib.

    The provider is identified by name, not by the presence of
    `lib/charms/<pkg>/`: `charmcraft fetch-lib` vendors a *consumed* library
    into exactly that path, so the directory existing is the normal shape of
    a consumer. Only the charm whose own declared name matches the package
    owns it — charms.grafana_agent belongs to the charm named grafana-agent.
    """
    if not module.startswith("charms."):
        return False
    pkg = module.split(".")[1]
    if not charm_name or charm_name.replace("-", "_") != pkg:
        return False
    return (charm_root / "lib" / "charms" / pkg).is_dir()


def _select_files(charm_root: Path, scope: str) -> list[Path]:
    if not charm_root.exists():
        return []
    py_files = [p for p in charm_root.rglob("*.py") if p.is_file()]
    if scope == "any":
        return [p for p in py_files if not _is_vendored_lib(p.relative_to(charm_root).parts)]
    wanted_dirs = _SRC_DIRS if scope == "src" else _TEST_DIRS
    avoid_dirs = _TEST_DIRS if scope == "src" else ()
    out = []
    for p in py_files:
        parts = p.relative_to(charm_root).parts
        if _is_vendored_lib(parts):
            continue
        if avoid_dirs and any(d in parts for d in avoid_dirs):
            # e.g. a test-fixture charm at `tests/integration/foo/src/charm.py`
            continue
        if any(d in parts for d in wanted_dirs):
            out.append(p)
    return out


# ── AST helpers ─────────────────────────────────────────────────────────────────


def _parse_text(text: str, path: Path) -> ast.Module | None:
    try:
        with warnings.catch_warnings():
            # Scanned charm code frequently contains regex string literals with
            # invalid escape sequences (e.g. "\d"), which ast.parse reports as
            # SyntaxWarning. Those are noise, not scan failures.
            warnings.simplefilter("ignore", SyntaxWarning)
            return ast.parse(text, filename=str(path))
    except (SyntaxError, ValueError):
        return None


def _parse(path: Path) -> ast.Module | None:
    return _parse_text(path.read_text(encoding="utf-8", errors="replace"), path)


# ── per-charm source cache ──────────────────────────────────────────────────────


class SourceFile:
    """One Python file, read and parsed at most once per charm scan."""

    __slots__ = ("_lines", "path", "rel", "text", "tree")

    def __init__(self, path: Path, charm_root: Path) -> None:
        self.path = path
        self.rel = str(path.relative_to(charm_root))
        self.text = path.read_text(encoding="utf-8", errors="replace")
        self.tree = _parse_text(self.text, path)
        self._lines: list[str] | None = None

    def line(self, lineno: int) -> str:
        """Return the 1-indexed source line `lineno`, stripped, or "" if out of range."""
        if self._lines is None:
            self._lines = self.text.splitlines()
        if 1 <= lineno <= len(self._lines):
            return self._lines[lineno - 1].strip()
        return ""


class CharmSource:
    """Per-charm cache of selected files, their text and their parsed AST.

    A charm is matched against every feature and architecture pattern in the
    catalogue — around 70 of them. Reading and re-parsing each file once per
    feature made AST parsing roughly 95% of scan time; parsing once per file
    and reusing the tree across all features cuts that to a single pass.

    Files are cached by path rather than by scope, so a file selected by both
    the "src" and "any" scopes is still only parsed once.
    """

    def __init__(self, charm_root: Path) -> None:
        self.charm_root = charm_root
        self._by_scope: dict[str, list[SourceFile]] = {}
        self._by_path: dict[Path, SourceFile] = {}
        self._charm_name: str | None = None
        self._charm_name_read = False

    @property
    def charm_name(self) -> str | None:
        """The charm's declared name, globbed for at most once per charm."""
        if not self._charm_name_read:
            self._charm_name = _metadata.declared_name(self.charm_root)
            self._charm_name_read = True
        return self._charm_name

    def files(self, scope: str) -> list[SourceFile]:
        cached = self._by_scope.get(scope)
        if cached is None:
            cached = [self._load(p) for p in _select_files(self.charm_root, scope)]
            self._by_scope[scope] = cached
        return cached

    def _load(self, path: Path) -> SourceFile:
        cached = self._by_path.get(path)
        if cached is None:
            cached = SourceFile(path, self.charm_root)
            self._by_path[path] = cached
        return cached


def _attr_chain(node: ast.AST) -> list[str] | None:
    """Return the dotted-name chain of an Attribute expression, or None."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return list(reversed(parts))
    return None


# ── detector kinds ──────────────────────────────────────────────────────────────


def _detect_import(tree: ast.Module, cfg: dict) -> Iterator[ast.Import | ast.ImportFrom]:
    module = cfg["module"]
    wanted_names = set(cfg.get("names") or [])
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            # `names:` filters which symbols are imported FROM a module; it
            # can't be checked on a bare `import X` without symbol resolution.
            if wanted_names:
                continue
            for alias in node.names:
                if alias.name == module or alias.name.startswith(module + "."):
                    yield node
                    break
        elif isinstance(node, ast.ImportFrom):
            if not node.module:
                continue
            if node.module == module or node.module.startswith(module + "."):
                if not wanted_names:
                    yield node
                else:
                    for alias in node.names:
                        if alias.name in wanted_names:
                            yield node
                            break


def _detect_call(tree: ast.Module, cfg: dict) -> Iterator[ast.Call]:
    """Match calls whose attribute chain ends with the configured dotted suffix.

    e.g. attr = "unit.open_port" matches `self.unit.open_port(80)` and
    `charm.unit.open_port(...)`. Leading underscores on attr-chain segments
    are stripped before comparison, so `self._model.get_secret(...)` matches
    a suffix of `model.get_secret` — helper modules commonly cache
    `self._model = charm.model` and call methods on the alias.
    """
    suffix = cfg["attr"].split(".")
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            chain = _attr_chain(func)
            if chain and len(chain) >= len(suffix):
                tail = [seg.lstrip("_") for seg in chain[-len(suffix) :]]
                if tail == suffix:
                    yield node
        elif isinstance(func, ast.Name) and len(suffix) == 1 and func.id.lstrip("_") == suffix[0]:
            yield node


_CAMEL_RE = re.compile(r"(?<!^)(?=[A-Z])")


def _event_to_on_attr(event_class: str) -> str:
    name = event_class.removesuffix("Event")
    return _CAMEL_RE.sub("_", name).lower()


def _observe_patterns(events: list[str]) -> list[re.Pattern[str]]:
    pats: list[re.Pattern[str]] = []
    for ev in events:
        attr = re.escape(_event_to_on_attr(ev))
        pats.append(re.compile(rf"observe\s*\([^)]*\bon\.{attr}\b"))
        pats.append(re.compile(rf"observe\s*\([^)]*\bon\[\s*['\"]{attr}['\"]\s*\]"))
    return pats


def _detect_regex(text: str, pattern: str) -> Iterator[re.Match[str]]:
    return re.finditer(pattern, text, flags=re.MULTILINE)


# ── AST: holistic-pattern detectors (architecture axis) ────────────────────


def _self_attr_calls(method_body: list[ast.stmt], attrs: set[str]) -> Iterator[ast.Call]:
    """Yield `self.X(...)` calls inside `method_body` where X is in `attrs`."""
    for node in ast.walk(ast.Module(body=method_body, type_ignores=[])):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "self"
            and func.attr in attrs
        ):
            yield node


def _detect_ast_init_call(tree: ast.Module, cfg: dict) -> Iterator[ast.Call]:
    """Match charms whose __init__ body contains a `self.X(...)` call where.

    X is one of `cfg["attrs"]`. Signal for the `unconditional-init` pattern:
    reconcile runs on every charm invocation by virtue of being in __init__.

    Walks classes that look like ops.CharmBase subclasses (any class whose
    __init__ takes >=1 positional arg beyond self — a soft heuristic, but
    good enough on charm source where __init__ is almost exclusively the
    charm class).
    """
    attrs = set(cfg["attrs"])
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for item in node.body:
            if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                yield from _self_attr_calls(item.body, attrs)


_RELATION_LIFECYCLE_SUFFIXES = (
    "relation_created",
    "relation_joined",
    "relation_changed",
    "relation_departed",
    "relation_broken",
)

_SYMMETRIC_RESOURCE_SUFFIXES = (
    "storage_attached",
    "storage_detaching",
    "pebble_ready",
    "pebble_custom_notice",
    "pebble_check_failed",
    "pebble_check_recovered",
)


def _relation_prefix(event: str) -> str | None:
    """Return the relation-name prefix if `event` is a standard relation.

    lifecycle event (`<relation>_relation_{created,joined,changed,departed,broken}`),
    else None.

    A bare event with no prefix at all (`self.on[relation_name].relation_created`,
    the dynamic/reusable-relation-name idiom) resolves to the trailing attribute
    name alone -- e.g. `relation_created` -- with nothing to strip. CALIBRATION
    #22 follow-up #7: treat that bare form as its own single relation endpoint
    (prefix `""`) rather than falling through to `None`, which previously let
    it dodge the relation-scoped classification entirely.
    """
    for suffix in _RELATION_LIFECYCLE_SUFFIXES:
        if event == suffix:
            return ""
        marker = "_" + suffix
        if event.endswith(marker) and len(event) > len(marker):
            return event[: -len(marker)]
    return None


def _is_relation_scoped_binding(events: set[str]) -> bool:
    """CALIBRATION #21 cut #1: every qualifying event is a standard.

    relation-lifecycle event, and they come from at most 2 distinct relation
    endpoints — relation-scoped plumbing, not charm-wide convergence.

    Covers both the single-relation shape (`chopsticks`, `discourse-k8s-operator`
    — one relation's own lifecycle) and the mirrored-relation shape
    (`loki-k8s-operator` — two relation endpoints, each contributing only
    standard lifecycle events).
    """
    prefixes: set[str] = set()
    for event in events:
        prefix = _relation_prefix(event)
        if prefix is None:
            return False
        prefixes.add(prefix)
    return len(prefixes) <= 2


def _symmetric_resource_split(event: str) -> tuple[str, str] | None:
    """Split `event` into (instance-prefix, suffix) if it ends with a known.

    per-instance resource event suffix (storage/pebble events ops generates
    once per storage mount or container). Returns None otherwise.
    """
    for suffix in _SYMMETRIC_RESOURCE_SUFFIXES:
        marker = "_" + suffix
        if event.endswith(marker) and len(event) > len(marker):
            return event[: -len(marker)], suffix
    return None


def _is_symmetric_resource_fanout(events: set[str]) -> bool:
    """CALIBRATION #21 cut #2: every qualifying event shares one common.

    non-relation suffix (e.g. `*_storage_detaching`, `*_pebble_ready`) across
    what look like N distinct resource instances — symmetric-resource
    fan-out, not reconcile. Covers the `mysql-operators` shape (4 storage
    mounts' `*_storage_detaching` events into one handler).
    """
    suffixes: set[str] = set()
    prefixes: set[str] = set()
    for event in events:
        split = _symmetric_resource_split(event)
        if split is None:
            return False
        prefix, suffix = split
        prefixes.add(prefix)
        suffixes.add(suffix)
    return len(suffixes) == 1 and len(prefixes) == len(events)


# The six baseline, non-relational Juju lifecycle hooks every charm
# eventually observes regardless of architecture. A shared handler bound
# only to a subset of these is idempotent-reconfiguration boilerplate, not
# a reconcile-shaped convergence signal -- CALIBRATION.md #22 follow-up #6,
# confirmed by a third real-world instance in #24 (charmed-linstor's
# linstor-controller, alongside #22's auditd-operator and
# chrony-client-operator).
_BASELINE_LIFECYCLE_EVENTS = frozenset({
    "install",
    "config_changed",
    "upgrade_charm",
    "update_status",
    "start",
    "stop",
})


def _is_baseline_lifecycle_only(events: set[str]) -> bool:
    """Return true when every event in `events` is one of the six baseline Juju.

    lifecycle hooks (`install`/`config_changed`/`upgrade_charm`, optionally
    plus `update_status`/`start`/`stop`) and nothing else -- no relation,
    leader, secret, cert, pebble, or custom-library event is present.
    """
    return bool(events) and events <= _BASELINE_LIFECYCLE_EVENTS


def _detect_ast_observe_shared_handler(tree: ast.Module, cfg: dict) -> Iterator[ast.Call]:
    """Match the holistic `reconcile` pattern: a single handler method is.

    bound to >= `min_events` distinct events via `framework.observe(...)`.

    Bind-count is the discriminating signal, not the handler's name — a
    `_reconcile_state` / `_handle` / `_update` method that receives three
    or more event types is reconcile-shaped. Two shared bindings (e.g.
    `leader_elected + leader_settings_changed -> _on_leader`) is a
    single-responsibility pattern, not reconcile. A binding whose events
    are entirely drawn from the six baseline Juju lifecycle hooks (see
    `_is_baseline_lifecycle_only`) is excluded too — see CALIBRATION.md
    #22 follow-up #6 / #24.

    Event identifier: the trailing attribute name of `args[0]` (works for
    `self.on.<x>`, `self.on['c'].<x>`, etc.; skips bare names like the
    `reconcile-all` loop variable). Handler identifier: trailing attribute
    name of `args[1]`. Events matching any suffix in `exclude_suffixes`
    (default: `_error`) are filtered out before counting.

    Two further exclusions (CALIBRATION #21 follow-ups #1/#2) declassify
    otherwise-qualifying bindings that are relation-scoped plumbing or
    symmetric-resource fan-out rather than charm-wide convergence — see
    `_is_relation_scoped_binding` / `_is_symmetric_resource_fanout`.
    """
    min_events = int(cfg.get("min_events", 3))
    exclude_suffixes = tuple(cfg.get("exclude_suffixes", ["_error"]))

    per_handler_events: dict[str, set[str]] = {}
    per_handler_calls: dict[str, list[ast.Call]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "observe"):
            continue
        if len(node.args) < 2:
            continue
        event = node.args[0].attr if isinstance(node.args[0], ast.Attribute) else None
        handler = node.args[1].attr if isinstance(node.args[1], ast.Attribute) else None
        if event is None or handler is None:
            continue
        if event.endswith(exclude_suffixes):
            continue
        per_handler_events.setdefault(handler, set()).add(event)
        per_handler_calls.setdefault(handler, []).append(node)
    for handler, events in per_handler_events.items():
        if len(events) < min_events:
            continue
        if _is_relation_scoped_binding(events):
            continue
        if _is_symmetric_resource_fanout(events):
            continue
        if _is_baseline_lifecycle_only(events):
            continue
        yield from per_handler_calls[handler]


def _detect_ast_shared_method(tree: ast.Module, cfg: dict) -> Iterator[ast.Call]:
    """Match charms with the `part-reconcile` pattern: per-event `_on_*`.

    handler methods that each delegate into a shared reconcile method.

    Fires when at least `cfg["min_callers"]` distinct `_on_*` (or otherwise
    named) handler methods inside a single class body contain a call to
    `self.X(...)` where X is in `cfg["attrs"]`. Yields one ast.Call node
    per qualifying caller so evidence lines are reported per handler.
    """
    attrs = set(cfg["attrs"])
    min_callers = int(cfg.get("min_callers", 2))
    handler_re = re.compile(cfg.get("handler_re", r"^_on_"))

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        callers: list[ast.Call] = []
        for item in node.body:
            if not isinstance(item, ast.FunctionDef):
                continue
            if not handler_re.match(item.name):
                continue
            first_call = next(_self_attr_calls(item.body, attrs), None)
            if first_call is not None:
                callers.append(first_call)
        if len(callers) >= min_callers:
            yield from callers


# ── public entry point ─────────────────────────────────────────────────────────


def _detect_pytest_config_key(charm_root: Path, config: dict) -> list[Evidence]:
    """Look for pytest config keys (e.g. `log_level`) in the four standard.

    config-file locations a charm might use.

    File / section conventions:
      * pyproject.toml -> [tool.pytest.ini_options]   (TOML)
      * pytest.ini     -> [pytest]                    (INI)
      * setup.cfg      -> [tool:pytest]               (INI)
      * tox.ini        -> [pytest]                    (INI)

    Stops on the first parse error per file; treats malformed config the
    same as absent.
    """
    keys = set(config["keys"])
    results: list[Evidence] = []

    pp = charm_root / "pyproject.toml"
    if pp.is_file():
        try:
            data = tomllib.loads(pp.read_text(encoding="utf-8", errors="replace"))
        except (tomllib.TOMLDecodeError, OSError):
            data = {}
        ini_opts = ((data.get("tool") or {}).get("pytest") or {}).get("ini_options") or {}
        if isinstance(ini_opts, dict):
            results.extend(
                Evidence(
                    "pyproject.toml",
                    0,
                    "pytest-config-key",
                    f"[tool.pytest.ini_options] {key}={ini_opts[key]!r}"[:120],
                )
                for key in keys
                if key in ini_opts
            )

    for filename, section_name in (
        ("pytest.ini", "pytest"),
        ("setup.cfg", "tool:pytest"),
        ("tox.ini", "pytest"),
    ):
        path = charm_root / filename
        if not path.is_file():
            continue
        cp = configparser.ConfigParser()
        try:
            cp.read_string(path.read_text(encoding="utf-8", errors="replace"))
        except (configparser.Error, ValueError, OSError):
            continue
        if section_name not in cp:
            continue
        results.extend(
            Evidence(
                filename,
                0,
                "pytest-config-key",
                f"[{section_name}] {key}={cp[section_name][key]}"[:120],
            )
            for key in keys
            if key in cp[section_name]
        )
    return results


# Directories a YAML sweep must not descend into: vendored charm libs ship
# their own metadata, and virtualenvs / build trees carry vendored manifests
# that say nothing about this charm.
_YAML_SKIP_DIRS = {".git", ".tox", ".venv", "venv", "node_modules", "build", "dist", "vendor"}


def _yaml_files(charm_root: Path, globs: list[str]) -> list[Path]:
    """Files matching `globs` under `charm_root`, minus vendored/build trees.

    Sorted and de-duplicated so overlapping globs (``**/*.yaml`` plus
    ``**/*.yml``) yield each file once, in a stable order.
    """
    out: set[Path] = set()
    for pattern in globs:
        for path in charm_root.glob(pattern):
            if not path.is_file():
                continue
            parts = path.relative_to(charm_root).parts
            if any(p in _YAML_SKIP_DIRS or p.startswith(".") for p in parts[:-1]):
                continue
            if _is_vendored_lib(parts):
                continue
            out.add(path)
    return sorted(out)


def _yaml_documents(path: Path) -> list[object]:
    """Return the parsed documents in `path`, or [] if it isn't loadable.

    Uses ``safe_load_all`` because charm repos carry multi-document YAML
    (k8s manifests, kustomize output) alongside charm metadata.
    """
    try:
        return [
            doc
            for doc in yaml.safe_load_all(path.read_text(encoding="utf-8", errors="replace"))
            if doc
        ]
    except (yaml.YAMLError, OSError, RecursionError):
        return []


def _find_mapping_key(node: object, keys: set[str]) -> str | None:
    """Return the first of `keys` present as a mapping key anywhere in `node`."""
    if isinstance(node, dict):
        for key in node:
            if isinstance(key, str) and key in keys:
                return key
        for value in node.values():
            found = _find_mapping_key(value, keys)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_mapping_key(item, keys)
            if found:
                return found
    return None


def _detect_yaml_key(charm_root: Path, config: dict) -> list[Evidence]:
    """Match YAML files declaring one of `keys` as a mapping key.

    config:
      files: list of globs relative to the charm root
             (default: ``["**/*.yaml", "**/*.yml"]``).
      key:   a single key name, or
      keys:  a list of key names. At least one of the two is required.

    Matching is structural rather than textual — the file is parsed and
    searched at any nesting depth — so a `checks:` appearing inside a string
    or a comment doesn't count. The reported line is found by scanning for
    the key afterwards, purely so the dashboard can deep-link; a structural
    match with no locatable line still reports line 0.
    """
    globs = list(config.get("files") or ["**/*.yaml", "**/*.yml"])
    keys = set(config.get("keys") or [])
    if "key" in config:
        keys.add(config["key"])
    if not keys:
        return []

    results: list[Evidence] = []
    for path in _yaml_files(charm_root, globs):
        documents = _yaml_documents(path)
        if not documents:
            continue
        found = next((k for k in (_find_mapping_key(doc, keys) for doc in documents) if k), None)
        if not found:
            continue
        rel = str(path.relative_to(charm_root))
        text = path.read_text(encoding="utf-8", errors="replace")
        key_re = re.compile(rf"^\s*[\"']?{re.escape(found)}[\"']?\s*:", re.MULTILINE)
        match = key_re.search(text)
        line = text.count("\n", 0, match.start()) + 1 if match else 0
        results.append(Evidence(rel, line, "yaml-key", f"{found}:"))
    return results


def _detect_requires_interface(charm_root: Path, config: dict) -> list[Evidence]:
    """Match `requires:` block interfaces in charmcraft.yaml / metadata.yaml.

    config:
      interfaces: list of interface names to match on the `requires` block.
      invert:     bool — if True, evidence is emitted only when the charm
                  has metadata but NONE of the listed interfaces are required
                  (used for `db.none`-style "absence of any known variant"
                  features). Defaults to False.
    """
    wanted = set(config.get("interfaces") or [])
    invert = bool(config.get("invert"))
    meta_files = _metadata._find_metadata_files(charm_root)
    if not meta_files:
        return []
    matches: list[Evidence] = []
    first_meta_rel: str | None = None
    for meta_path in meta_files:
        data = _metadata._load_yaml(meta_path)
        if not data:
            continue
        rel = str(meta_path.relative_to(charm_root))
        if first_meta_rel is None:
            first_meta_rel = rel
        block = data.get("requires") or {}
        if not isinstance(block, dict):
            continue
        for name, info in block.items():
            iface = (info or {}).get("interface", "") if isinstance(info, dict) else ""
            if iface in wanted:
                matches.append(
                    Evidence(rel, 0, "requires-interface", f"requires {name}: {iface}"[:120])
                )
    if invert:
        if first_meta_rel is not None and not matches:
            return [
                Evidence(first_meta_rel, 0, "requires-interface", "no listed interface required")
            ]
        return []
    return matches


def _detect_relation_count(charm_root: Path, config: dict) -> list[Evidence]:
    """Bucket a charm by its requires / provides relation count.

    config:
      role:     "requires" | "provides" | "peers".
      min:      inclusive lower bound on the count.
      max:      inclusive upper bound; open-ended if omitted.
      optional: bool — if True, count only relations declared
                ``optional: true`` under `role` (Juju "optional relations",
                Juju 3.6+ / interpreted by charmcraft). Defaults to False.

    Relations are deduped by name across charmcraft.yaml + metadata.yaml,
    matching the merge that metadata.read() does; a relation counts as
    optional if any file declares it optional.
    """
    role = config["role"]
    min_ = int(config["min"])
    max_ = int(config["max"]) if "max" in config else None
    only_optional = bool(config.get("optional"))

    meta_files = _metadata._find_metadata_files(charm_root)
    if not meta_files:
        return []
    seen: dict[str, bool] = {}
    first_rel: str | None = None
    for meta_path in meta_files:
        data = _metadata._load_yaml(meta_path)
        if not data:
            continue
        if first_rel is None:
            first_rel = str(meta_path.relative_to(charm_root))
        block = data.get(role) or {}
        if not isinstance(block, dict):
            continue
        for name, info in block.items():
            opt = bool(info.get("optional")) if isinstance(info, dict) else False
            seen[name] = seen.get(name, False) or opt

    count = sum(1 for opt in seen.values() if opt) if only_optional else len(seen)
    if count < min_:
        return []
    if max_ is not None and count > max_:
        return []
    label = f"{role}{'-optional' if only_optional else ''}={count}"
    return [Evidence(first_rel or "charmcraft.yaml", 0, "relation-count", label)]


def detect_feature(
    charm_root: Path, feature: Detectable, source: CharmSource | None = None
) -> list[Evidence]:
    """Evidence for `feature` in the charm at `charm_root`.

    Pass `source` to share one read-and-parse pass across every feature in
    the catalogue; omitting it builds a throwaway cache for this call alone.
    """
    if source is None:
        source = CharmSource(charm_root)
    evidence: list[Evidence] = []

    # File-independent detectors run once over the charm root rather than per
    # Python file in scope — they read YAML/INI/TOML, which `_select_files`
    # (Python-only) would never hand them.
    for det in feature.detectors:
        if det.kind == "pytest-config-key":
            evidence.extend(_detect_pytest_config_key(charm_root, det.config))
        elif det.kind == "requires-interface":
            evidence.extend(_detect_requires_interface(charm_root, det.config))
        elif det.kind == "relation-count":
            evidence.extend(_detect_relation_count(charm_root, det.config))
        elif det.kind == "yaml-key":
            evidence.extend(_detect_yaml_key(charm_root, det.config))

    # Pre-compile observe-event regexes per detector.
    observe_pats: dict[int, list[re.Pattern[str]]] = {}
    for i, det in enumerate(feature.detectors):
        if det.kind == "observe-event":
            observe_pats[i] = _observe_patterns(det.config["events"])

    # Skip import detectors that target a lib the charm itself provides —
    # importing charms.X from src/ when you ARE charm X is self-referential,
    # not a consumer signal.
    provided_lib_detectors: set[int] = set()
    for i, det in enumerate(feature.detectors):
        if det.kind == "import":
            mod = det.config.get("module", "")
            if _charm_provides_lib(charm_root, mod, source.charm_name):
                provided_lib_detectors.add(i)

    # AST-walking detector kinds, keyed by the walker they delegate to. All
    # report the same way: one Evidence per node, snippet taken from the
    # node's own source line.
    ast_walkers = {
        "ast-init-call": _detect_ast_init_call,
        "ast-observe-shared-handler": _detect_ast_observe_shared_handler,
        "ast-shared-method": _detect_ast_shared_method,
    }

    for src in source.files(feature.scope):
        text, tree, rel = src.text, src.tree, src.rel

        for i, det in enumerate(feature.detectors):
            if det.kind == "import" and tree is not None:
                if i in provided_lib_detectors:
                    continue
                for imp in _detect_import(tree, det.config):
                    line = ast.get_source_segment(text, imp) or ""
                    evidence.append(
                        Evidence(rel, imp.lineno, det.kind, line.splitlines()[0][:120])
                    )
            elif det.kind == "call" and tree is not None:
                evidence.extend(
                    Evidence(rel, call.lineno, det.kind, src.line(call.lineno)[:120])
                    for call in _detect_call(tree, det.config)
                )
            elif det.kind == "observe-event":
                for pat in observe_pats[i]:
                    for m in pat.finditer(text):
                        lineno = text.count("\n", 0, m.start()) + 1
                        evidence.append(Evidence(rel, lineno, det.kind, m.group(0)[:120]))
            elif det.kind in ast_walkers and tree is not None:
                evidence.extend(
                    Evidence(rel, node.lineno, det.kind, src.line(node.lineno)[:120])
                    for node in ast_walkers[det.kind](tree, det.config)
                )
            elif det.kind == "regex":
                for m in _detect_regex(text, det.config["pattern"]):
                    lineno = text.count("\n", 0, m.start()) + 1
                    evidence.append(Evidence(rel, lineno, det.kind, m.group(0).strip()[:120]))

    return evidence
