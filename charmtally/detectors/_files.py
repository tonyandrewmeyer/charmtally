"""File selection, reading and parsing: everything a detector runs *over*.

`_select_files` decides which Python files a scope covers; `SourceFile` and
`CharmSource` make sure each of those is read and parsed once per charm scan
rather than once per feature. The YAML sweep lives here too, because it is a
walk of the charm tree like the Python one — the detectors that consume it
are in `_config`.
"""

from __future__ import annotations

import ast
import warnings
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeVar, overload

import yaml

from .. import metadata as _metadata

if TYPE_CHECKING:
    from pathlib import Path

_Node = TypeVar("_Node", bound=ast.AST)


# libyaml's C loader where the wheel ships it (it is 12x faster than the
# pure-Python one, and this package parses every YAML file in every charm),
# falling back to the Python loader where it doesn't.
_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


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
    return _parse_text(_read_text(path), path)


def _read_text(path: Path) -> str:
    """Read `path` as text, or "" if it isn't readable."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# ── per-charm source cache ──────────────────────────────────────────────────────


class SourceFile:
    """One Python file, read and parsed at most once per charm scan."""

    __slots__ = (
        "_buckets",
        "_lines",
        "_queries",
        "_walk",
        "analysis",
        "path",
        "rel",
        "text",
        "tree",
    )

    def __init__(self, path: Path, charm_root: Path) -> None:
        self.path = path
        self.rel = str(path.relative_to(charm_root))
        self.text = path.read_text(encoding="utf-8", errors="replace")
        self.tree = _parse_text(self.text, path)
        self._lines: list[str] | None = None
        self._walk: list[ast.AST] | None = None
        self._buckets: dict[type[ast.AST], list[ast.AST]] | None = None
        self._queries: dict[tuple[type[ast.AST], ...], list] = {}
        # Derived per-file tables the detector modules own and key for
        # themselves — `_ast`'s `framework.observe` resolution tables live
        # here, so that building them is a per-file cost rather than a
        # per-detector one. `_files` knows nothing about what is in it.
        self.analysis: dict[str, object] = {}

    def line(self, lineno: int) -> str:
        """Return the 1-indexed source line `lineno`, stripped, or "" if out of range."""
        if self._lines is None:
            self._lines = self.text.splitlines()
        if 1 <= lineno <= len(self._lines):
            return self._lines[lineno - 1].strip()
        return ""

    def _index(self) -> tuple[list[ast.AST], dict[type[ast.AST], list[ast.AST]]]:
        """Walk the tree once, bucketing every node by its own type.

        The catalogue has 53 `import` and 10 `call`/`call-kwarg` detectors,
        and the architecture patterns re-walk for `ClassDef`, `Assign`, `For`
        and `Call` on top of that — each detector used to `ast.walk` the whole
        module for itself, which measured as most of a scan's CPU. One walk
        answers all of them.

        `_walk` keeps the full walk order so a query spanning several node
        types can be answered without a second walk; `_buckets` makes a
        single-type query a dict lookup.
        """
        walk: list[ast.AST] = []
        buckets: dict[type[ast.AST], list[ast.AST]] = {}
        if self.tree is not None:
            for node in ast.walk(self.tree):
                walk.append(node)
                bucket = buckets.get(node.__class__)
                if bucket is None:
                    bucket = buckets[node.__class__] = []
                bucket.append(node)
        self._walk = walk
        self._buckets = buckets
        return walk, buckets

    @overload
    def nodes(self) -> list[ast.AST]: ...

    @overload
    def nodes(self, *types: type[_Node]) -> list[_Node]: ...

    def nodes(self, *types: type[ast.AST]) -> list[ast.AST]:
        """Every node of the given `types`, in `ast.walk` order.

        Exactly what `[n for n in ast.walk(tree) if isinstance(n, types)]`
        returns, order included — evidence order is part of the scan's output,
        so a right-set-wrong-order index would be a silent output change.
        Call with no types for the whole walk.

        The returned list is the cache: iterate it, do not mutate it.

        Buckets are keyed on each node's concrete class and matched with
        `issubclass`, which agrees with `isinstance` for every node `ast.parse`
        produces. The deprecated `ast.Num` / `ast.Str` / `ast.Bytes` aliases
        are the exception — they answer `isinstance` for `Constant` nodes
        through a custom check no subclass relation backs — so ask for
        `ast.Constant`, not for those.
        """
        cached = self._queries.get(types)
        if cached is not None:
            return cached
        walk, buckets = self._walk, self._buckets
        if walk is None or buckets is None:
            walk, buckets = self._index()
        if not types:
            result = walk
        else:
            matched = [b for cls, b in buckets.items() if issubclass(cls, types)]
            if not matched:
                result = []
            elif len(matched) == 1:
                result = matched[0]
            else:
                # More than one concrete type answers the query, so walk order
                # across them comes from `_walk` rather than from the buckets.
                result = [n for n in walk if isinstance(n, types)]
        self._queries[types] = result
        return result

    @property
    def imports(self) -> list[ast.Import | ast.ImportFrom]:
        """Every import statement in the file, in `ast.walk` order."""
        return self.nodes(ast.Import, ast.ImportFrom)

    @property
    def calls(self) -> list[ast.Call]:
        """Every call expression in the file, in `ast.walk` order."""
        return self.nodes(ast.Call)


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
        self._meta_files: list[Path] | None = None
        self._meta_data: dict[Path, dict | None] = {}
        self._yaml_sweeps: dict[tuple[str, ...], list[Path]] = {}
        self._yaml_docs: dict[Path, tuple[str, list[object]]] = {}
        self._dep_files: list[tuple[str, str]] | None = None

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

    def metadata_files(self) -> list[Path]:
        """Return the charm's charmcraft.yaml / metadata.yaml paths, globbed for once.

        The catalogue has 21 `relation-count` and 15 `requires-interface`
        detectors, each of which used to re-run the whole-tree glob and
        re-parse what it found.
        """
        if self._meta_files is None:
            self._meta_files = _metadata._find_metadata_files(self.charm_root)
        return self._meta_files

    def metadata_data(self, path: Path) -> dict | None:
        """Return the parsed contents of one metadata file, loaded at most once."""
        if path not in self._meta_data:
            self._meta_data[path] = _metadata._load_yaml(path)
        return self._meta_data[path]

    def dependency_files(self) -> list[tuple[str, str]]:
        """Return the charm's dependency files as (path relative to the root, text).

        Globbed and read at most once per charm: every `requirement` detector
        in the catalogue reads the same handful of files, and there is one per
        dependency the catalogue asks about.
        """
        if self._dep_files is None:
            self._dep_files = [
                (str(path.relative_to(self.charm_root)), _read_text(path))
                for path in _metadata.dependency_files(self.charm_root)
            ]
        return self._dep_files

    def yaml_files(self, globs: list[str]) -> list[Path]:
        """Return the YAML files matching `globs`, swept for once per glob set."""
        key = tuple(globs)
        cached = self._yaml_sweeps.get(key)
        if cached is None:
            cached = _yaml_files(self.charm_root, globs)
            self._yaml_sweeps[key] = cached
        return cached

    def yaml_documents(self, path: Path) -> tuple[str, list[object]]:
        """Return the text and parsed documents of one YAML file, read at most once.

        The text comes back alongside the documents because `yaml-key` needs
        both — the documents to match structurally, the text to locate a line
        for the dashboard to deep-link to.
        """
        cached = self._yaml_docs.get(path)
        if cached is None:
            text = _read_text(path)
            cached = (text, _yaml_documents(text))
            self._yaml_docs[path] = cached
        return cached


# ── YAML sweep ──────────────────────────────────────────────────────────────────


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


def _yaml_documents(text: str) -> list[object]:
    """Return the parsed documents in `text`, or [] if it isn't loadable.

    Loads *all* documents because charm repos carry multi-document YAML
    (k8s manifests, kustomize output) alongside charm metadata.
    """
    try:
        return [doc for doc in yaml.load_all(text, Loader=_LOADER) if doc]
    except (yaml.YAMLError, RecursionError):
        return []
