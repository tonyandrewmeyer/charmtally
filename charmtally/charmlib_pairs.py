"""The curated charmlibs ↔ Charmhub library equivalence table.

`charmlib-pairs.yaml` asserts which `charmlibs-*` package replaces which
vendored Charmhub library. Nothing in a scan can derive that: the two names
are chosen by different people for different registries, and only a human
reading both libraries can say they are the same thing. See the file's own
header for the schema and for what is deliberately left unpaired.

The table is loaded through `importlib.resources`, not repo-root arithmetic,
for the same reason `catalogue.default_path` is — it is package data, and a
wheel that resolved it relative to a checkout would ship a CLI that dies on a
missing file.
"""

from __future__ import annotations

import functools
import importlib.resources
from dataclasses import dataclass
from pathlib import Path

import yaml

#: Where a library is available, from the point of view of the census.
CHARMLIBS_ONLY = "charmlibs-only"
CHARMHUB_ONLY = "charmhub-only"
BOTH = "both"


def default_path() -> Path:
    """Path to the pairing table shipped inside the package."""
    # Named literally rather than via `__package__`, which is typed `str | None`.
    return Path(str(importlib.resources.files("charmtally") / "charmlib-pairs.yaml"))


@dataclass(frozen=True)
class Pair:
    """One library, under both of the names it goes by."""

    id: str
    charmlibs: frozenset[str]
    charmhub: frozenset[str]


@dataclass(frozen=True)
class PairTable:
    """The table, plus the two lookups every caller wants of it."""

    pairs: tuple[Pair, ...]
    #: charmlibs package name → the pair it belongs to.
    by_charmlib: dict[str, Pair]
    #: `lib/charms/<dir>` name → the pair it belongs to.
    by_charmhub: dict[str, Pair]

    def identity(self, *, charmlib: str = "", charmhub: str = "") -> str:
        """Return the id this name counts under, or the name itself if unpaired.

        An unpaired name is its own identity: a Charmhub library nobody has
        republished is one library, and so is a charmlib nobody had published
        before. Falling back to the name is what lets the census count every
        library in use from a table that only lists the pairs.
        """
        pair = self.by_charmlib.get(charmlib) or self.by_charmhub.get(charmhub)
        return pair.id if pair else (charmlib or charmhub)


def load(path: Path | None = None) -> PairTable:
    """Read the pairing table. Raises if a row names a name twice."""
    raw = yaml.safe_load((path or default_path()).read_text(encoding="utf-8")) or {}
    pairs: list[Pair] = []
    by_charmlib: dict[str, Pair] = {}
    by_charmhub: dict[str, Pair] = {}
    for entry in raw.get("pairs") or []:
        pair = Pair(
            id=str(entry["id"]),
            charmlibs=frozenset(entry.get("charmlibs") or ()),
            charmhub=frozenset(entry.get("charmhub") or ()),
        )
        pairs.append(pair)
        # A name landing in two rows would make the census depend on row
        # order, which is the sort of quiet wrongness the table exists to
        # avoid — so it is an error rather than a last-write-wins.
        for name, index in ((pair.charmlibs, by_charmlib), (pair.charmhub, by_charmhub)):
            for one in name:
                if one in index:
                    msg = f"{one!r} is claimed by both {index[one].id!r} and {pair.id!r}"
                    raise ValueError(msg)
                index[one] = pair
    return PairTable(pairs=tuple(pairs), by_charmlib=by_charmlib, by_charmhub=by_charmhub)


@functools.lru_cache(maxsize=1)
def default_table() -> PairTable:
    """Return the shipped table, parsed once per process."""
    return load()
