"""Run a single feature's detectors against a charm tree.

Implements four detector kinds for the v1 spike:
    import         — AST: matches `import X` and `from X import Y` (with optional names filter)
    call           — AST: matches `*.<attr>(...)` where <attr> is the trailing dotted suffix
    observe-event  — regex: matches `observe(... on.<snake_name>(...)|on['<snake_name>'] ...)`
                     for each given event class (translated CamelCase→snake_case, dropping trailing 'Event')
    regex          — raw multiline regex over file contents

The `yaml-key` kind is deferred — pebble.checks has a regex fallback that covers v1.
"""

from __future__ import annotations

import ast
import configparser
import re
import warnings
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib  # Python 3.11+ stdlib
except ModuleNotFoundError:  # pragma: no cover — exercised only on 3.10
    import tomli as tomllib

from . import metadata as _metadata
from .catalogue import Feature


@dataclass(frozen=True)
class Evidence:
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


def _charm_provides_lib(charm_root: Path, module: str) -> bool:
    """True if this charm's own lib/charms/<pkg>/ tree provides the given module.

    Prevents flagging the lib-provider charm when it imports its own library
    from src/ — e.g. grafana-agent importing charms.grafana_agent because it
    IS grafana-agent, not because it consumes the COS agent lib.
    """
    if not module.startswith("charms."):
        return False
    pkg = module.split(".")[1]
    lib_dir = charm_root / "lib" / "charms" / pkg
    return lib_dir.is_dir()


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


def _parse(path: Path) -> ast.Module | None:
    try:
        with warnings.catch_warnings():
            # Scanned charm code frequently contains regex string literals with
            # invalid escape sequences (e.g. "\d"), which ast.parse reports as
            # SyntaxWarning. Those are noise, not scan failures.
            warnings.simplefilter("ignore", SyntaxWarning)
            return ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
    except (SyntaxError, ValueError):
        return None


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


def _detect_import(tree: ast.Module, cfg: dict) -> Iterator[ast.AST]:
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


def _detect_call(tree: ast.Module, cfg: dict) -> Iterator[ast.AST]:
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


def _detect_ast_init_call(tree: ast.Module, cfg: dict) -> Iterator[ast.AST]:
    """Match charms whose __init__ body contains a `self.X(...)` call where
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


def _detect_ast_observe_shared_handler(tree: ast.Module, cfg: dict) -> Iterator[ast.AST]:
    """Match the holistic `reconcile` pattern: a single handler method is
    bound to >= `min_events` distinct events via `framework.observe(...)`.

    Bind-count is the discriminating signal, not the handler's name — a
    `_reconcile_state` / `_handle` / `_update` method that receives three
    or more event types is reconcile-shaped. Two shared bindings (e.g.
    `leader_elected + leader_settings_changed -> _on_leader`) is a
    single-responsibility pattern, not reconcile.

    Event identifier: the trailing attribute name of `args[0]` (works for
    `self.on.<x>`, `self.on['c'].<x>`, etc.; skips bare names like the
    `reconcile-all` loop variable). Handler identifier: trailing attribute
    name of `args[1]`. Events matching any suffix in `exclude_suffixes`
    (default: `_error`) are filtered out before counting.
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
        if len(events) >= min_events:
            yield from per_handler_calls[handler]


def _detect_ast_shared_method(tree: ast.Module, cfg: dict) -> Iterator[ast.AST]:
    """Match charms with the `part-reconcile` pattern: per-event `_on_*`
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
    """Look for pytest config keys (e.g. `log_level`) in the four standard
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
            for key in keys:
                if key in ini_opts:
                    results.append(
                        Evidence(
                            "pyproject.toml",
                            0,
                            "pytest-config-key",
                            f"[tool.pytest.ini_options] {key}={ini_opts[key]!r}"[:120],
                        )
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
        for key in keys:
            if key in cp[section_name]:
                results.append(
                    Evidence(
                        filename,
                        0,
                        "pytest-config-key",
                        f"[{section_name}] {key}={cp[section_name][key]}"[:120],
                    )
                )
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
    invert = bool(config.get("invert", False))
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
                matches.append(Evidence(rel, 0, "requires-interface", f"requires {name}: {iface}"[:120]))
    if invert:
        if first_meta_rel is not None and not matches:
            return [Evidence(first_meta_rel, 0, "requires-interface", "no listed interface required")]
        return []
    return matches


def detect_feature(charm_root: Path, feature: Feature) -> list[Evidence]:
    files = _select_files(charm_root, feature.scope)
    evidence: list[Evidence] = []

    # File-independent detectors run once over the charm root, not per
    # Python file in scope. Today: pytest-config-key, requires-interface.
    for det in feature.detectors:
        if det.kind == "pytest-config-key":
            evidence.extend(_detect_pytest_config_key(charm_root, det.config))
        elif det.kind == "requires-interface":
            evidence.extend(_detect_requires_interface(charm_root, det.config))

    # Pre-compile observe-event regexes per detector.
    observe_pats: dict[int, list[re.Pattern[str]]] = {}
    for i, det in enumerate(feature.detectors):
        if det.kind == "observe-event":
            observe_pats[i] = _observe_patterns(det.config["events"])

    # Skip import detectors that target a lib the charm itself provides.
    # lib/charms/X/ existing means this charm IS the lib provider; importing
    # charms.X from src/ is self-referential, not a consumer signal.
    provided_lib_detectors: set[int] = set()
    for i, det in enumerate(feature.detectors):
        if det.kind == "import":
            mod = det.config.get("module", "")
            if _charm_provides_lib(charm_root, mod):
                provided_lib_detectors.add(i)

    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        tree = _parse(path)
        rel = str(path.relative_to(charm_root))

        for i, det in enumerate(feature.detectors):
            if det.kind == "import" and tree is not None:
                if i in provided_lib_detectors:
                    continue
                for node in _detect_import(tree, det.config):
                    line = ast.get_source_segment(text, node) or ""
                    evidence.append(Evidence(rel, node.lineno, det.kind, line.splitlines()[0][:120]))
            elif det.kind == "call" and tree is not None:
                for node in _detect_call(tree, det.config):
                    line = text.splitlines()[node.lineno - 1] if node.lineno <= len(text.splitlines()) else ""
                    evidence.append(Evidence(rel, node.lineno, det.kind, line.strip()[:120]))
            elif det.kind == "observe-event":
                for pat in observe_pats[i]:
                    for m in pat.finditer(text):
                        lineno = text.count("\n", 0, m.start()) + 1
                        evidence.append(Evidence(rel, lineno, det.kind, m.group(0)[:120]))
            elif det.kind == "ast-init-call" and tree is not None:
                for node in _detect_ast_init_call(tree, det.config):
                    line = text.splitlines()[node.lineno - 1] if node.lineno <= len(text.splitlines()) else ""
                    evidence.append(Evidence(rel, node.lineno, det.kind, line.strip()[:120]))
            elif det.kind == "ast-observe-shared-handler" and tree is not None:
                for node in _detect_ast_observe_shared_handler(tree, det.config):
                    line = text.splitlines()[node.lineno - 1] if node.lineno <= len(text.splitlines()) else ""
                    evidence.append(Evidence(rel, node.lineno, det.kind, line.strip()[:120]))
            elif det.kind == "ast-shared-method" and tree is not None:
                for node in _detect_ast_shared_method(tree, det.config):
                    line = text.splitlines()[node.lineno - 1] if node.lineno <= len(text.splitlines()) else ""
                    evidence.append(Evidence(rel, node.lineno, det.kind, line.strip()[:120]))
            elif det.kind == "regex":
                for m in _detect_regex(text, det.config["pattern"]):
                    lineno = text.count("\n", 0, m.start()) + 1
                    evidence.append(Evidence(rel, lineno, det.kind, m.group(0).strip()[:120]))
            # yaml-key intentionally not implemented for v1 spike.

    return evidence
