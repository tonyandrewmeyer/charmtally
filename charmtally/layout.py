"""Where a charm keeps its integration tests, which is not always under its root.

Three layouts turn up in the corpus, and only the first is visible from the
charm root alone:

- the common one — ``tests/integration/*.py`` beside ``src/``;
- the monorepo one — one ``tests/integration/`` at the *repo* root (or at an
  intermediate ``charms/`` directory) exercising every sub-charm beneath it,
  which is how the coordinator/worker pairs, the data-platform monorepos and
  ``ceph-charms`` are laid out;
- the zaza one — the OpenStack charms on opendev, which have no
  ``integration/`` directory at all: ``tests/tests.yaml`` names bundles and
  ``zaza.*`` test classes, and ``sunbeam-charms`` keeps several of those at
  the repo root under ``tests/<suite>/tests.yaml``.

Read one way and the second two populations report as having no integration
tests, which is what moved a third of the adoption page's "no integration
tests" bucket there. This module is the single answer to "where are this
charm's integration tests" that `metadata.read`, the ``tests``-scoped detector
sweep and the ``zaza-config`` detector all share, so none of them can disagree
about it.

The walk up stops at the repo root — the checkout the charm lives in, found
the same way `scan.head_sha` finds the SHA its evidence is linked against —
and never looks sideways: a sibling sub-charm's ``tests/integration/`` is that
charm's, not this one's.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

# `tests.yaml` keys that make a file a zaza test description rather than a
# namesake. `tests:` alone is too weak — it is also a pytest-ish word — but
# any `*_bundles` list is zaza's and nobody else's.
_ZAZA_KEYS = ("gate_bundles", "smoke_bundles", "dev_bundles", "tests")


def find_repo_root(charm_root: Path) -> Path:
    """Walk up from `charm_root` to the directory holding `.git`, else `charm_root`.

    `.git` is matched as an entry rather than a directory: a worktree or a
    submodule checkout has a `.git` *file* pointing elsewhere, and both are
    still the top of the tree the evidence paths are relative to.

    Resolved before the walk because `charmtally local .` hands in a relative
    path, whose `parents` run out at `.` rather than at the filesystem root.
    """
    charm_root = charm_root.resolve()
    for candidate in (charm_root, *charm_root.parents):
        if (candidate / ".git").exists():
            return candidate
    return charm_root


def _candidates(charm_root: Path) -> Iterator[Path]:
    """`charm_root`, then each ancestor up to and including the repo root.

    A charm root that is not inside a checkout is its own repo root, so the
    iterator yields it alone rather than escaping into whatever is above.

    Yields unresolved paths: what comes back is joined to files whose evidence
    is reported relative to `charm_root` *as given*, and resolving a symlinked
    checkout would make that relative path wrong. The repo root is compared
    resolved, since that is how `find_repo_root` finds it.
    """
    charm_root = charm_root.absolute()
    repo_root = find_repo_root(charm_root)
    yield charm_root
    if charm_root.resolve() == repo_root:
        return
    for parent in charm_root.parents:
        yield parent
        if parent.resolve() == repo_root:
            return


def _has_python(directory: Path) -> bool:
    return directory.is_dir() and any(p.is_file() for p in directory.rglob("*.py"))


def integration_tests_dir(charm_root: Path) -> Path | None:
    """Return the `tests/integration/` directory holding this charm's pytest suite, or None.

    The charm's own wins. Failing that, the nearest ancestor up to the repo
    root that has one — a monorepo's shared suite — on the reading that a
    `tests/integration/` above a sub-charm covers the sub-charms beneath it.
    A directory with no Python in it does not count: an empty or
    bundles-only directory is not a suite.
    """
    for base in _candidates(charm_root):
        candidate = base / "tests" / "integration"
        if _has_python(candidate):
            return candidate
    return None


def _is_zaza_config(path: Path) -> bool:
    try:
        data = yaml.load(path.read_text(encoding="utf-8", errors="replace"), Loader=_LOADER)  # ruff: ignore[unsafe-yaml-load]
    except (yaml.YAMLError, OSError):
        return False
    return isinstance(data, dict) and any(k in data for k in _ZAZA_KEYS)


def zaza_configs(charm_root: Path) -> list[Path]:
    """Return the zaza `tests.yaml` files describing this charm's integration suite.

    `tests/tests.yaml` is the classic single-charm layout; `tests/*/tests.yaml`
    is one suite per directory, which is how `sunbeam-charms` keeps its
    machine, identity and tempest suites at the repo root. Same walk as
    `integration_tests_dir`, and the first level that has any wins, so a
    charm with its own description never also reports the repo's.
    """
    for base in _candidates(charm_root):
        tests = base / "tests"
        if not tests.is_dir():
            continue
        found = [
            p for p in (tests / "tests.yaml", *sorted(tests.glob("*/tests.yaml"))) if p.is_file()
        ]
        found = [p for p in found if _is_zaza_config(p)]
        if found:
            return found
    return []
