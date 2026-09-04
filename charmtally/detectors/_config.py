"""The file-independent detector kinds.

These four read the charm root directly rather than the Python files
`_select_files` returns, which makes them the only detectors that can see a
charm's YAML, INI and TOML. They take a `CharmSource` rather than a
`SourceFile`, run once per charm rather than once per file, and build their
own `Evidence` — there is no node to point at.
"""

from __future__ import annotations

import configparser
import re
from typing import TYPE_CHECKING

from ._files import Evidence

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

if TYPE_CHECKING:
    from ._files import CharmSource


def _detect_pytest_config_key(source: CharmSource, config: dict) -> list[Evidence]:
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
    charm_root = source.charm_root
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


def _detect_yaml_key(source: CharmSource, config: dict) -> list[Evidence]:
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
    for path in source.yaml_files(globs):
        text, documents = source.yaml_documents(path)
        if not documents:
            continue
        found = next((k for k in (_find_mapping_key(doc, keys) for doc in documents) if k), None)
        if not found:
            continue
        rel = str(path.relative_to(source.charm_root))
        key_re = re.compile(rf"^\s*[\"']?{re.escape(found)}[\"']?\s*:", re.MULTILINE)
        match = key_re.search(text)
        line = text.count("\n", 0, match.start()) + 1 if match else 0
        results.append(Evidence(rel, line, "yaml-key", f"{found}:"))
    return results


def _detect_requires_interface(source: CharmSource, config: dict) -> list[Evidence]:
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
    meta_files = source.metadata_files()
    if not meta_files:
        return []
    matches: list[Evidence] = []
    first_meta_rel: str | None = None
    for meta_path in meta_files:
        data = source.metadata_data(meta_path)
        if not data:
            continue
        rel = str(meta_path.relative_to(source.charm_root))
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


def _detect_relation_count(source: CharmSource, config: dict) -> list[Evidence]:
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

    meta_files = source.metadata_files()
    if not meta_files:
        return []
    seen: dict[str, bool] = {}
    first_rel: str | None = None
    for meta_path in meta_files:
        data = source.metadata_data(meta_path)
        if not data:
            continue
        if first_rel is None:
            first_rel = str(meta_path.relative_to(source.charm_root))
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
