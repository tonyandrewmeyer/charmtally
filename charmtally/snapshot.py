"""Thin a scored file down to what the snapshot series actually needs.

The dated `snapshots/scored-*.json` files are the whole history: they cannot
be regenerated, so every byte written stays in git forever. A scored file is
the wrong thing to keep verbatim, because most of it is *evidence for a
reading* rather than the reading itself — `evidence` (the file/line hits a
detector fired on) and `rationale` (the scoring rule's one-liner) are between
them 45% of the bytes, and nothing that reads a snapshot ever looks at
either. Both are regenerable from a fresh scan of the same commit; the
readings are not.

What survives is chosen two different ways, on purpose:

  - **Feature records are an allowlist** (`present`, `score`). There are only
    four keys and the other two are the bulk, so naming what stays is both
    exact and stable.
  - **`__meta__` is kept whole.** An allowlist here would be a bet that no
    future metric wants a field today's metrics ignore — and that bet is
    already being lost: `adoption.py` grew `charm_user` and `charmlibs_count`
    readers after the first snapshots were written, and could only backfill
    them because the fat snapshots had kept the fields. `__meta__` is 9% of a
    scored file; buying option value on the whole history for that is cheap.

Key *presence* is preserved rather than defaulted, because several callers
read it as a signal: `adoption._has_charmlibs_data` and `_has_charm_user_data`
distinguish "this scan didn't look" from "the value is falsey" by asking
whether the key is there at all, and `cmd_score` deliberately pops `score`
off a present feature that has no note to attach.

Top-level `__`-keyed blocks (`__skipped__`, `__backfill__`, `__rocks__`) pass
through untouched: they are provenance and the rocks half, together about 2%
of the file, and thinning them would trade a real loss of history for nothing.
"""

from __future__ import annotations

#: Per-feature keys a thin snapshot keeps. `present` drives every adoption
#: number and timeline cell; `score` is the cell state for an absent feature.
KEPT_FEATURE_KEYS = ("present", "score")
#: The keys thinning removes, recorded in `__thin__` so a reader of a mixed
#: snapshots directory can see why an old file has fields a new one lacks.
DROPPED_FEATURE_KEYS = ("evidence", "rationale")
#: Marks a snapshot as thinned. Underscored like the other top-level blocks,
#: so every existing consumer skips it without being taught about it.
THIN_KEY = "__thin__"
#: The per-charm metadata block, kept whole rather than allowlisted.
META_KEY = "__meta__"


def thin_features(features: dict) -> dict:
    """Drop evidence and rationale from one charm's features dict.

    `__meta__` rides through whole; every other entry is reduced to the keys
    it actually has out of `KEPT_FEATURE_KEYS`.
    """
    out: dict = {}
    for name, record in features.items():
        if name == META_KEY or not isinstance(record, dict):
            out[name] = record
            continue
        out[name] = {k: record[k] for k in KEPT_FEATURE_KEYS if k in record}
    return out


def thin(scored: dict) -> dict:
    """Return a thinned copy of a scored file, ready to write as a snapshot.

    Shape-preserving: a thin snapshot is a scored file with fields removed,
    not a new format, so `trend.load_snapshots` reads thin and fat snapshots
    out of the same directory with no idea which is which.
    """
    out: dict = {}
    for slug, record in scored.items():
        if slug.startswith("__") or not isinstance(record, dict):
            out[slug] = record
            continue
        charm = dict(record)
        features = charm.get("features")
        if isinstance(features, dict):
            charm["features"] = thin_features(features)
        out[slug] = charm
    out[THIN_KEY] = {
        "dropped": list(DROPPED_FEATURE_KEYS),
        "kept": list(KEPT_FEATURE_KEYS),
        "note": "regenerable from a fresh scan of the same commit",
        "tool": "charmtally snapshot",
    }
    return out
