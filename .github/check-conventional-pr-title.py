# Ported from canonical/jubilant under Apache-2.0; original copyright Canonical Ltd.
"""Check that a PR title follows the Conventional Commits specification.

Reads the PR title from the PR_TITLE environment variable.
Exits non-zero and prints an error if the title is invalid.

Reference: https://www.conventionalcommits.org/en/v1.0.0/

This repo defines a restricted set of commit types and disallows scopes
in PR titles, matching the canonical/jubilant convention.
"""

from __future__ import annotations

import os
import re
import sys

_TYPES = frozenset({
    "chore",
    "ci",
    "docs",
    "feat",
    "fix",
    "perf",
    "refactor",
    "revert",
    "test",
})

# <type>[optional scope][optional !]: <description>
_PATTERN = re.compile(
    r"^(?P<type>[A-Za-z]+)"
    r"(?:\((?P<scope>[^()]+)\))?"
    r"(?P<breaking>!)?"
    r": "
    r"(?P<description>.+)$"
)

_DOCS_URL = "CONTRIBUTING.md#pull-requests"


def _main() -> None:
    title = os.environ.get("PR_TITLE", "").strip()
    if not title:
        print("PR_TITLE environment variable is not set or empty.", file=sys.stderr)
        sys.exit(1)

    match = _PATTERN.match(title)
    if not match:
        print(
            f"PR title does not follow Conventional Commits format.\n"
            f"Expected: <type>[!]: <description>\n"
            f"Got: {title!r}\n"
            f"Read more: {_DOCS_URL}",
            file=sys.stderr,
        )
        sys.exit(1)

    scope = match.group("scope")
    if scope is not None:
        print(
            f"Scopes must not be used in PR titles.\nGot: {title!r}\nRead more: {_DOCS_URL}",
            file=sys.stderr,
        )
        sys.exit(1)

    commit_type = match.group("type")
    if commit_type not in _TYPES:
        print(
            f"Invalid type {commit_type!r} in PR title.\n"
            f"Valid types: {', '.join(sorted(_TYPES))}\n"
            f"Got: {title!r}\n"
            f"Read more: {_DOCS_URL}",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"OK: {title!r}")


if __name__ == "__main__":
    _main()
