"""Dump every shared-handler binding in a charm tree, with the cut it hits.

Usage:
    uv run python -m charmtally.tools.bindings <charm-root> [<charm-root> ...]

Not part of the pipeline. This is a calibration instrument: the `reconcile`
detector (`detectors._ast._detect_ast_observe_shared_handler`) only ever *yields*
the bindings that survive its three cuts, which is the right shape for a
scan and the wrong shape for auditing one. Several calibration rounds have
needed to see the bindings that did **not** survive, and each wrote its own
throwaway re-implementation of the accumulation logic — which is how a
sweep quietly stops agreeing with the detector it is meant to be measuring
(CALIBRATION #41 hit exactly that risk and avoided it by importing the
detector's helpers instead).

So this *calls* the accumulation the detector calls —
`detectors._ast.iter_observe_bindings`, over the per-file resolution tables
`build_observe_context` builds — and differs only in reporting every
binding rather than the survivors. It emits JSON: one row per
(charm, file, handler) binding, carrying the resolved event set, which
events `_relation_prefix` could resolve and which it could not, the
relation endpoints the resolvable half names, whether the binding
qualifies, and the first cut that stops it if it does not.

A binding is *crossing* when it mixes events with a relation-lifecycle
suffix and events `_relation_prefix` returns None for. That is the
condition under which the relation-scoped cut bails at its first
unresolvable event and never reaches its endpoint count — follow-up
#10/#18's shape. `--crossing-only` filters to those.

The event floor and excluded suffixes match `features.yaml`'s `reconcile`
pattern. Pass `--min-events` to sweep against a different floor.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from ..detectors import CharmSource
from ..detectors._ast import (
    _is_baseline_lifecycle_only,
    _is_relation_scoped_binding,
    _is_symmetric_resource_fanout,
    _relation_prefix,
    build_observe_context,
    iter_observe_bindings,
)

if TYPE_CHECKING:
    from ..detectors._files import SourceFile

_DEFAULT_MIN_EVENTS = 3
_DEFAULT_EXCLUDE_SUFFIXES = ("_error",)


def bindings_in_file(
    src: SourceFile, exclude_suffixes: tuple[str, ...] = _DEFAULT_EXCLUDE_SUFFIXES
) -> dict[str, dict]:
    """Return ``{handler: {"events": set[str], "lines": list[int]}}`` for one file.

    The detector's own accumulation, called rather than restated:
    `iter_observe_bindings` yields every `(event, handler, call)` binding
    `_detect_ast_observe_shared_handler` counts, over the same per-file
    resolution tables `build_observe_context` gives the detector. All this
    adds is the reporting — the detector goes on to apply its cuts, and this
    keeps every binding so a calibration round can see the ones that did not
    survive them.
    """
    out: dict[str, dict] = {}
    ctx = build_observe_context(src)
    for event, handler, call in iter_observe_bindings(src, ctx, exclude_suffixes):
        rec = out.setdefault(handler, {"events": set(), "lines": []})
        rec["events"].add(event)
        rec["lines"].append(call.lineno)
    return out


def classify(events: set[str], min_events: int = _DEFAULT_MIN_EVENTS) -> dict:
    """Describe one binding's event set the way the detector's cuts see it."""
    prefixes = {e: _relation_prefix(e) for e in events}
    lifecycle = sorted(e for e, p in prefixes.items() if p is not None)
    unresolved = sorted(e for e, p in prefixes.items() if p is None)
    endpoints = sorted({p for p in prefixes.values() if p is not None})
    if len(events) < min_events:
        cut = "below-floor"
    elif _is_relation_scoped_binding(events):
        cut = "relation-scoped"
    elif _is_symmetric_resource_fanout(events):
        cut = "symmetric-fanout"
    elif _is_baseline_lifecycle_only(events):
        cut = "baseline-only"
    else:
        cut = None
    return {
        "lifecycle": lifecycle,
        "unresolved": unresolved,
        # `_relation_prefix` maps the bare `self.on[<unresolved key>].<event>`
        # form to "" (CALIBRATION #22 follow-up #7), so an endpoint list of
        # [""] means one *apparent* endpoint that may hide several relations.
        "endpoints": endpoints,
        "crossing": bool(lifecycle and unresolved),
        "qualifies": cut is None,
        "cut": cut,
    }


def scan_root(charm_root: Path, min_events: int, exclude_suffixes: tuple[str, ...]) -> list[dict]:
    """Return one row per binding found under `charm_root`'s `src` scope."""
    rows = []
    for src in CharmSource(charm_root).files("src"):
        if src.tree is None:
            continue
        for handler, rec in bindings_in_file(src, exclude_suffixes).items():
            rows.append({
                "charm_root": str(charm_root),
                "file": src.rel,
                "line": min(rec["lines"]),
                "handler": handler,
                "events": sorted(rec["events"]),
                **classify(rec["events"], min_events),
            })
    return rows


def main(argv: list[str] | None = None) -> int:
    """Read each charm root given on the command line and emit its bindings as JSON."""
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("charm_root", type=Path, nargs="+", help="Charm root directory to read.")
    p.add_argument(
        "--min-events",
        type=int,
        default=_DEFAULT_MIN_EVENTS,
        help=f"Event floor to classify against (default {_DEFAULT_MIN_EVENTS}).",
    )
    p.add_argument(
        "--exclude-suffix",
        action="append",
        dest="exclude_suffixes",
        help="Event-name suffix to drop before counting (repeatable; default `_error`).",
    )
    p.add_argument(
        "--crossing-only",
        action="store_true",
        help="Only report bindings that mix resolvable and unresolvable events.",
    )
    p.add_argument(
        "--qualifying-only",
        action="store_true",
        help="Only report bindings that survive every cut, as the detector yields them.",
    )
    args = p.parse_args(argv)

    suffixes = tuple(args.exclude_suffixes or _DEFAULT_EXCLUDE_SUFFIXES)
    rows = []
    for root in args.charm_root:
        if not root.is_dir():
            print(f"not a directory: {root}", file=sys.stderr)
            return 2
        rows.extend(scan_root(root, args.min_events, suffixes))
    if args.crossing_only:
        rows = [r for r in rows if r["crossing"]]
    if args.qualifying_only:
        rows = [r for r in rows if r["qualifies"]]
    json.dump(rows, sys.stdout, indent=1)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
