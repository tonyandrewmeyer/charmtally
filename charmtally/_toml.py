"""Cross-version TOML reader, shared by every module that parses TOML.

`tomllib` is stdlib from 3.11, `tomli` is a conditional dependency below
that. Exactly one of these resolves on any given interpreter, so a type
checker pinned to either version flags the other. Which one is unresolved
depends on the interpreter the checker is run under — CI's 3.12 env has no
tomli, a 3.10 dev env has no tomllib — so one of these two suppressions is
always the redundant one. `unused-ignore-comment` is switched off for this
file in pyproject.toml; it can't be done inline, because the inline form is
itself reported as unused on whichever interpreter doesn't need it.

It lives in its own module so the suppression (and the pyproject override
that makes it work) exists once rather than once per importer.
"""

from __future__ import annotations

try:
    import tomllib  # ty: ignore[unresolved-import]
except ModuleNotFoundError:  # pragma: no cover — exercised only on 3.10
    import tomli as tomllib  # ty: ignore[unresolved-import]

__all__ = ["tomllib"]
