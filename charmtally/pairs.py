"""Pair k8s and machine charms in the scanned corpus.

A k8s charm and a machine charm that implement "the same thing" are
a *pair* (`postgresql` + `postgresql-k8s`, `mysql-router` +
`mysql-router-k8s`, …). Pairs are surfaced as a descriptive
cross-charm metric — useful for "which workloads are charmed for
both substrates?" — not as a scoring signal.

Pair identification

1. Restrict the k8s side to charms with `has_containers == True` (the
   existing scanner-derived signal). The machine side is the
   complement.
2. Normalise the k8s charm name by stripping the trailing
   `-k8s-operator` / `-k8s` / `-operator` markers — leaving a
   canonical "root".
3. A pair is formed when the same root appears on both sides. Match
   is **high** confidence when roots are identical; **medium** when
   they differ by edit-distance ≤ 2 (one typo / hyphen). No matches
   below that — false positives outweigh true positives once you go
   farther.

Sub-metrics (v1)

* `same_repo` — both members share the same `repo_url` (after
  stripping the `.git` suffix). True monorepo, no clone needed.
* `shares_charmlib` — either member's `library_names` (the `lib/charms/<n>/`
  vendored set, surfaced via `__meta__`) contains a name normalised
  to the other side's name root. Strong signal that a shared
  charmlib mediates between the two.

Deferred to v2 (out of scope here)

* `shared-src` vs `copy-paste` distinction within the same repo —
  needs the clones plus a source diff.
* `% unique code` — needs the clones plus a clone-aware metric.

The detector is conservative on ambiguous roots (e.g.
`prometheus-scrape-config-k8s` shouldn't pair with
`prometheus-operator`). The fix when one slips through is the
`pair_exclude:` / `pair_alias:` overrides in `corpus-overrides.yaml`
(planned, not built in this module).
"""

from __future__ import annotations

from dataclasses import dataclass

_K8S_SUFFIXES = ("-k8s-operator", "-k8s")
_GENERIC_SUFFIXES = ("-operator",)


@dataclass(frozen=True)
class Pair:
    root: str
    k8s_name: str
    machine_name: str
    k8s_repo_url: str
    machine_repo_url: str
    confidence: str  # "high" | "medium"
    same_repo: bool
    shares_charmlib: bool


def _strip_suffix(name: str, suffixes: tuple[str, ...]) -> str:
    for suffix in suffixes:
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)]
    return name


def _normalise_k8s(name: str) -> str:
    """Drop the k8s marker and any trailing -operator, leaving a root."""
    stripped = _strip_suffix(name, _K8S_SUFFIXES)
    return _strip_suffix(stripped, _GENERIC_SUFFIXES)


def _normalise_machine(name: str) -> str:
    """Drop the -operator marker on the machine side, leaving the root."""
    return _strip_suffix(name, _GENERIC_SUFFIXES)


def _levenshtein(a: str, b: str) -> int:
    """Plain Levenshtein. Bounded inputs (charm names are short), so the
    cost of the naive O(len(a)*len(b)) table is fine."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        curr = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            ins = curr[j - 1] + 1
            dele = prev[j] + 1
            sub = prev[j - 1] + (0 if ca == cb else 1)
            curr[j] = min(ins, dele, sub)
        prev = curr
    return prev[-1]


def _strip_dot_git(url: str) -> str:
    return url.removesuffix(".git").rstrip("/")


def _normalise_lib_to_root(lib: str) -> str:
    """A vendored lib `foo_bar_k8s` corresponds to charm root `foo-bar`."""
    underscored = lib.replace("_", "-")
    return _normalise_k8s(underscored)


def _shares_charmlib(
    a_libs: tuple[str, ...],
    b_libs: tuple[str, ...],
    a_root: str,
    b_root: str,
) -> bool:
    """True if either side vendors a lib whose name normalises to the
    other side's root. Catches both the "k8s charm vendors the machine
    charm's lib" and the reverse case."""
    if not a_libs and not b_libs:
        return False
    if any(_normalise_lib_to_root(lib) == b_root for lib in a_libs):
        return True
    return any(_normalise_lib_to_root(lib) == a_root for lib in b_libs)


def find_pairs(results: dict) -> list[Pair]:
    """Return the list of k8s/machine pairs in the scanned corpus.

    *results* matches the shape of the scanner's `results.json`: a dict
    of `slug -> {"name", "repo_url", "features": {"__meta__": {…}, ...}}`.
    Pairs are sorted by `root` for stable output.
    """
    k8s_side: list[dict] = []
    machine_side: list[dict] = []
    for slug, record in results.items():
        if slug.startswith("__") or not isinstance(record, dict):
            continue
        meta = record.get("features", {}).get("__meta__", {}) or {}
        name = record.get("name") or slug.split("/", 1)[-1]
        entry = {
            "name": name,
            "repo_url": record.get("repo_url", ""),
            "libs": tuple(meta.get("library_names") or ()),
        }
        if meta.get("has_containers"):
            entry["root"] = _normalise_k8s(name)
            k8s_side.append(entry)
        else:
            entry["root"] = _normalise_machine(name)
            machine_side.append(entry)

    machines_by_root: dict[str, list[dict]] = {}
    for m in machine_side:
        machines_by_root.setdefault(m["root"], []).append(m)

    pairs: list[Pair] = []
    for k in k8s_side:
        root = k["root"]
        candidates = machines_by_root.get(root)
        confidence = "high"
        if not candidates:
            # Edit-distance fallback: scan machine roots within radius 2.
            best: dict | None = None
            best_dist = 3
            for m in machine_side:
                d = _levenshtein(root, m["root"])
                if d < best_dist:
                    best, best_dist = m, d
            if best is None or best_dist > 2:
                continue
            candidates = [best]
            confidence = "medium"
        for m in candidates:
            same_repo = bool(k["repo_url"]) and _strip_dot_git(k["repo_url"]) == _strip_dot_git(m["repo_url"])
            shares = _shares_charmlib(k["libs"], m["libs"], root, m["root"])
            pairs.append(
                Pair(
                    root=root,
                    k8s_name=k["name"],
                    machine_name=m["name"],
                    k8s_repo_url=k["repo_url"],
                    machine_repo_url=m["repo_url"],
                    confidence=confidence,
                    same_repo=same_repo,
                    shares_charmlib=shares,
                )
            )
    pairs.sort(key=lambda p: (p.confidence != "high", p.root, p.k8s_name))
    return pairs
