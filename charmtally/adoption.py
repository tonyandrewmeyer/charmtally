"""Charm-tech adoption metrics: a handful of headline numbers, over time.

Where `trend.py` answers "what changed between two scans" across the whole
feature catalogue, this module answers a much narrower question: **is the
tooling charm-tech ships actually being picked up?** It is deliberately
small — three to six metrics, each a single number per snapshot date, each
with a breakdown a reader can drill into. Adding a fifth metric is cheap;
adding a fiftieth would defeat the point of the page.

Metrics are computed from the same dated `snapshots/scored-*.json` files the
history page reads, so a metric is only as old as the data behind it:

  - **Feature-drift guard.** A metric whose inputs weren't scanned at a given
    date yields *no point* for that date, rather than a misleading zero.
    `ops.typed-relation` and `jubilant.integration-tests` have been in the
    catalogue since the first snapshot, so those series run the full history;
    `testing.pytest-operator`, `ops.typed-config` and
    `__meta__.charmlibs_count` were added later. A metric with *several*
    inputs, only some of which are that old, keeps its history and marks the
    thin points `partial` instead — see `compute_integration_testing` and
    `compute_typed_relation`.
  - **Eligibility.** Reactive and legacy-classic charms pre-date ops entirely
    (see `scoring.score_absent`, which scores every ops feature
    `not-applicable` for them). Counting them in a denominator would make
    adoption look permanently stuck, so `eligible_charms` is the denominator
    for every metric measuring adoption of an *ops-era API*. It is not a
    universal denominator: a metric about tooling that works regardless of
    charm framework — Jubilant drives a deployed Juju model and neither
    knows nor cares what the charm is built on — measures against the whole
    corpus, and says so in its `denominator_note`.

A metric's `compute` returns None when its inputs are missing; the series
simply skips that date.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import rocks

if TYPE_CHECKING:
    from collections.abc import Callable

    from .trend import Snapshot

# Metric keys are stable identifiers: they key the JSON emitted by
# `charmtally adoption --emit-json` and any dashboard bookmark, so renaming
# one breaks links and downstream consumers the same way renaming a feature
# breaks snapshots. Add rather than rename.
TYPED_RELATION = "typed-relation"
INTEGRATION_TESTING = "integration-testing"
CHARMLIBS_SHARE = "charmlibs-share"
ROOTLESS = "rootless"


@dataclass(frozen=True)
class Metric:
    """One headline number, tracked over time.

    `compute` maps a snapshot to a point dict (`value`, `numerator`,
    `denominator`, `breakdown`) or None when that snapshot lacks the inputs.
    A metric with `compute=None` is *declared but not yet defined* — it
    renders as a placeholder card so the page shows the intended shape of
    the scorecard rather than silently omitting a metric still being agreed.
    """

    key: str
    title: str
    question: str
    unit: str  # "percent"
    compute: Callable[[Snapshot], dict | None] | None = None
    # Breakdown keys, in the order they should stack in the chart. The last
    # one is conventionally the "not adopted" bucket.
    breakdown_keys: tuple[str, ...] = ()
    detail: str = ""
    scope: str = "charms"  # "charms" | "rocks" | "charms+rocks"
    pending: str = ""  # why this metric has no data yet, if it has none
    # Set when this metric's denominator is NOT `eligible_charms`, to say what
    # it is instead. Rendered on the card and in the page footnote, because a
    # reader comparing two cards will otherwise assume a shared denominator.
    denominator_note: str = ""
    caveats: tuple[str, ...] = field(default_factory=tuple)


# ── shared helpers ──────────────────────────────────────────────────────────


def _meta(charm: dict) -> dict:
    meta = charm.get("features", {}).get("__meta__")
    return meta if isinstance(meta, dict) else {}


def eligible_charms(snapshot: Snapshot) -> dict[str, dict]:
    """Charms a charm-tech adoption number can fairly be measured against.

    Excludes reactive and legacy-classic charms: neither can adopt an ops
    API, so leaving them in the denominator would cap every metric below
    100% for reasons no amount of adoption work could shift.
    """
    return {
        slug: charm
        for slug, charm in snapshot.charms.items()
        if not _meta(charm).get("is_reactive") and not _meta(charm).get("is_legacy_classic")
    }


def _present(charm: dict, feature: str) -> bool:
    rec = charm.get("features", {}).get(feature)
    return bool(rec and rec.get("present"))


def _point(
    snapshot: Snapshot,
    *,
    value: float,
    numerator: int,
    denominator: int,
    breakdown: dict[str, float] | None = None,
    counts: dict[str, int] | None = None,
    partial: str = "",
) -> dict:
    """Assemble one series point. `partial` names an input that wasn't scanned."""
    return {
        "date": snapshot.date,
        "value": round(value, 1),
        "numerator": numerator,
        "denominator": denominator,
        "breakdown": {k: round(v, 1) for k, v in (breakdown or {}).items()},
        "counts": counts or {},
        "partial": partial,
    }


def _percent(numerator: int, denominator: int) -> float:
    return 100 * numerator / denominator if denominator else 0.0


# ── metric: typed relation data ─────────────────────────────────────────────


def compute_typed_relation(snapshot: Snapshot) -> dict | None:
    """Share of eligible charms using any of the typed-data APIs.

    Any one of `Relation.load`, `Relation.save`, `load_config` or
    `load_params` counts: they are one story — "this charm parses Juju data
    into a declared model instead of poking at dicts" — and a charm that has
    typed its config has adopted it as surely as one that has typed a
    relation.

    A snapshot scanned before `ops.typed-config` existed is computed from the
    relation half alone and flagged `partial` rather than dropped; the
    alternative is truncating the history of the oldest metric on the page.
    No snapshot in the committed series is in that state — the backfill
    replayed them all onto one catalogue — but the fallback stands for the
    next feature that joins this metric.
    """
    if "ops.typed-relation" not in snapshot.feature_names:
        return None
    charms = eligible_charms(snapshot)
    if not charms:
        return None
    scanned_config = "ops.typed-config" in snapshot.feature_names
    features = (
        ("ops.typed-relation", "ops.typed-config") if scanned_config else ("ops.typed-relation",)
    )
    users = sum(1 for c in charms.values() if any(_present(c, f) for f in features))
    return _point(
        snapshot,
        value=_percent(users, len(charms)),
        numerator=users,
        denominator=len(charms),
        breakdown={
            "typed": _percent(users, len(charms)),
            "untyped": _percent(len(charms) - users, len(charms)),
        },
        counts={"typed": users, "untyped": len(charms) - users},
        partial="" if scanned_config else "ops.typed-config not yet scanned",
    )


# ── metric: integration testing ─────────────────────────────────────────────

_JUBILANT = "jubilant"
_PYTEST_OPERATOR = "pytest-operator"
_OTHER_TESTS = "other-integration-tests"
_NO_TESTS = "no-integration-tests"


def compute_integration_testing(snapshot: Snapshot) -> dict | None:
    """How charms drive their integration tests, as four shares.

    Measured against the **whole corpus**, not `eligible_charms`: jubilant
    talks to a deployed Juju model over the CLI, so a reactive or
    legacy-classic charm can adopt it as readily as an ops charm. Excluding
    them here would hide real adopters (there is already one) for a reason
    that only holds for ops-API metrics.

    A charm mid-migration can hit both framework detectors; jubilant is
    counted first, so a part-migrated charm reads as adopted rather than as
    a pytest-operator holdout.

    `other-integration-tests` is charms with a `tests/integration/` directory
    where neither framework was detected — a bespoke harness, or a detector
    miss. It is reported rather than folded into either framework bucket.
    When `testing.pytest-operator` wasn't in the catalogue at this date, its
    charms land in that bucket and the point is flagged `partial`.
    """
    if "jubilant.integration-tests" not in snapshot.feature_names:
        return None
    charms = snapshot.charms
    if not charms:
        return None
    scanned_pytest_operator = "testing.pytest-operator" in snapshot.feature_names

    counts = {_JUBILANT: 0, _PYTEST_OPERATOR: 0, _OTHER_TESTS: 0, _NO_TESTS: 0}
    for charm in charms.values():
        if _present(charm, "jubilant.integration-tests"):
            counts[_JUBILANT] += 1
        elif scanned_pytest_operator and _present(charm, "testing.pytest-operator"):
            counts[_PYTEST_OPERATOR] += 1
        elif _meta(charm).get("has_integration_tests"):
            counts[_OTHER_TESTS] += 1
        else:
            counts[_NO_TESTS] += 1

    total = len(charms)
    if not scanned_pytest_operator:
        # Drop the bucket entirely rather than reporting it as 0%: at these
        # dates pytest-operator charms are sitting in `other-integration-tests`,
        # and a 0% column reads as "nobody uses it".
        del counts[_PYTEST_OPERATOR]
    return _point(
        snapshot,
        value=_percent(counts[_JUBILANT], total),
        numerator=counts[_JUBILANT],
        denominator=total,
        breakdown={k: _percent(v, total) for k, v in counts.items()},
        counts=counts,
        partial="" if scanned_pytest_operator else "testing.pytest-operator not yet scanned",
    )


# ── metric: charmlibs share ─────────────────────────────────────────────────

_NO_LIBS = "no-libraries"


def _has_charmlibs_data(snapshot: Snapshot) -> bool:
    """Report whether this snapshot's `__meta__` carries charmlibs counts at all.

    Checked by key presence, not by value: `CharmMeta.from_dict` defaults a
    missing `charmlibs_count` to 0, so a value of 0 can't be told apart from
    "this scan didn't look" without asking the raw dict.
    """
    return any("charmlibs_count" in _meta(charm) for charm in snapshot.charms.values())


def compute_charmlibs_share(snapshot: Snapshot) -> dict | None:
    """Mean per-charm share of libraries that come from `charmlibs`.

    Per charm: `charmlibs / (charmlibs + vendored Charmhub libs used)`. The
    denominator counts libraries the charm *consumes*: `library_count`
    excludes `lib/charms/<own_name>/`, so publishing a Charmhub library
    doesn't count against the publisher's charmlibs share. The
    headline is the mean of that ratio over charms that use at least one
    library either way — an unweighted mean, so a charm with two libraries
    counts as much as one with twenty. That is the intent: this tracks how
    far each charm has moved, not how many library *imports* across the
    corpus happen to be charmlibs.

    Charms using no libraries at all have no ratio to contribute and are
    excluded from the mean; their share of the corpus is reported separately
    as the `no-libraries` breakdown rather than being quietly dropped.
    """
    if not _has_charmlibs_data(snapshot):
        return None
    charms = eligible_charms(snapshot)
    if not charms:
        return None

    ratios: list[float] = []
    with_any_charmlib = 0
    no_libs = 0
    for charm in charms.values():
        meta = _meta(charm)
        charmlibs = int(meta.get("charmlibs_count") or 0)
        charmhub = int(meta.get("library_count") or 0)
        total = charmlibs + charmhub
        if total == 0:
            no_libs += 1
            continue
        ratios.append(charmlibs / total)
        if charmlibs:
            with_any_charmlib += 1

    if not ratios:
        return None
    mean_share = 100 * sum(ratios) / len(ratios)
    return _point(
        snapshot,
        value=mean_share,
        numerator=with_any_charmlib,
        denominator=len(ratios),
        breakdown={
            "charmlibs": mean_share,
            "charmhub-libs": 100 - mean_share,
            _NO_LIBS: _percent(no_libs, len(charms)),
        },
        counts={
            "charms-with-libraries": len(ratios),
            "charms-using-any-charmlib": with_any_charmlib,
            _NO_LIBS: no_libs,
        },
    )


# ── metric: rootless (charms + rocks) ───────────────────────────────────────

_DAEMON = "run-user: _daemon_"
_NON_ROOT = "charm-user: non-root"
_SUDOER = "charm-user: sudoer"
_OTHER_USER = "other non-root user"
_AS_ROOT = "root (or unset)"
#: `charm-user` values that mean "not root". `root` is a real value too, and
#: buckets with an unset key: both run as root.
_CHARM_USER_BUCKETS = {"non-root": _NON_ROOT, "sudoer": _SUDOER}


def _rock_bucket(record: dict) -> str:
    """Bucket one rock by its `run-user`, or None-ish → root."""
    run_user = record.get("run_user")
    if not run_user:
        return _AS_ROOT
    return _DAEMON if run_user == rocks.DAEMON_USER else _OTHER_USER


def _charm_bucket(meta: dict) -> str:
    """Bucket one k8s charm by its `charm-user`.

    An unset key is Juju's default, which is root — the same reading as an
    explicit `charm-user: root`, so the two share a bucket. A value that is
    neither (a typo; charmcraft would reject it) counts as non-root rather
    than being silently read as root.
    """
    charm_user = meta.get("charm_user")
    if not charm_user:
        return _AS_ROOT
    if charm_user == "root":
        return _AS_ROOT
    return _CHARM_USER_BUCKETS.get(charm_user, _OTHER_USER)


def _has_charm_user_data(snapshot: Snapshot) -> bool:
    """Whether this snapshot's `__meta__` carries `charm_user` at all.

    Key presence, not value, for the same reason as `_has_charmlibs_data`:
    an unset `charm-user` is legitimately None, so None can't distinguish
    "runs as root" from "this scan didn't look".
    """
    return any("charm_user" in _meta(charm) for charm in snapshot.charms.values())


def compute_rootless(snapshot: Snapshot) -> dict | None:
    """Share of rocks and k8s charms that declare a non-root user.

    One denominator over two populations, because the question — did this
    artefact drop root? — is the same one on both sides even though the key
    differs. Both halves must have been scanned for a point to exist: a
    number computed from rocks alone, or charms alone, would read as a
    corpus-wide share while measuring half the corpus.

    Rocks whose `rockcraft.yaml` couldn't be fetched are excluded rather than
    counted as root; `readable` is what separates "runs as root" from "we
    couldn't look".
    """
    if not snapshot.rocks_scanned or not _has_charm_user_data(snapshot):
        return None

    counts = {_DAEMON: 0, _NON_ROOT: 0, _SUDOER: 0, _OTHER_USER: 0, _AS_ROOT: 0}
    for record in snapshot.rocks.values():
        if not record.get("readable"):
            continue
        counts[_rock_bucket(record)] += 1
    for charm in snapshot.charms.values():
        meta = _meta(charm)
        if not meta.get("has_containers"):
            continue
        counts[_charm_bucket(meta)] += 1

    total = sum(counts.values())
    if not total:
        return None
    rootless = total - counts[_AS_ROOT]
    return _point(
        snapshot,
        value=_percent(rootless, total),
        numerator=rootless,
        denominator=total,
        breakdown={k: _percent(v, total) for k, v in counts.items()},
        counts=counts,
    )


# ── the scorecard ───────────────────────────────────────────────────────────

METRICS: tuple[Metric, ...] = (
    Metric(
        key=TYPED_RELATION,
        title="Typed Juju data",
        question="Are charms reading Juju data through the typed APIs?",
        unit="percent",
        compute=compute_typed_relation,
        breakdown_keys=("typed", "untyped"),
        detail=(
            "Percent of eligible charms calling any of <code>load_config</code>, "
            "<code>load_params</code>, <code>Relation.load</code> or "
            "<code>Relation.save</code> — any one counts. Backed by the "
            "<code>ops.typed-relation</code> and <code>ops.typed-config</code> "
            "features."
        ),
    ),
    Metric(
        key=INTEGRATION_TESTING,
        title="Jubilant adoption",
        question="What do charms use to drive their integration tests?",
        unit="percent",
        compute=compute_integration_testing,
        breakdown_keys=(_JUBILANT, _PYTEST_OPERATOR, _OTHER_TESTS, _NO_TESTS),
        detail=(
            "Percent of <em>all</em> scanned charms whose integration tests "
            "import <code>jubilant</code>, split against pytest-operator, "
            "other harnesses, and charms with no integration tests found at "
            "all."
        ),
        denominator_note=(
            "the whole scanned corpus, not just eligible charms — jubilant "
            "drives a deployed model, so a reactive or legacy-classic charm "
            "can use it too"
        ),
        caveats=(
            "A charm hitting both framework detectors is counted as jubilant — "
            "part-migrated reads as adopted.",
        ),
    ),
    Metric(
        key=CHARMLIBS_SHARE,
        title="charmlibs share",
        question="How much of a charm's library surface comes from charmlibs?",
        unit="percent",
        compute=compute_charmlibs_share,
        # `no-libraries` is deliberately not here: it is a share of *all*
        # eligible charms, while the two ratios are shares of one charm's
        # libraries averaged over charms that have any. Stacking it on top of
        # a stack that already totals 100 pushed it off the pinned y-axis, so
        # it drew as nothing. It stays in `breakdown` and `counts` for the
        # fallback table and the JSON.
        breakdown_keys=("charmlibs", "charmhub-libs"),
        detail=(
            "Mean over charms of <code>charmlibs / (charmlibs + vendored "
            "Charmhub libs)</code>. Charms using no libraries at all are "
            "excluded from the mean and tracked separately."
        ),
        caveats=(
            "Unweighted mean of per-charm ratios: every charm counts once, "
            "regardless of how many libraries it pulls in.",
        ),
    ),
    Metric(
        key=ROOTLESS,
        title="Rootless by default",
        question="Are rocks and charms declaring a non-root user?",
        unit="percent",
        compute=compute_rootless,
        scope="charms+rocks",
        breakdown_keys=(_DAEMON, _NON_ROOT, _SUDOER, _OTHER_USER, _AS_ROOT),
        detail=(
            "Percent of scanned artefacts that declare a non-root user: rocks "
            "setting <code>run-user</code> in <code>rockcraft.yaml</code> "
            "(<code>_daemon_</code> is the only value rockcraft accepts today) "
            "and Kubernetes charms setting <code>charm-user</code> in "
            "<code>charmcraft.yaml</code> (<code>non-root</code> or "
            "<code>sudoer</code>). Anything that leaves the key unset runs as "
            "root, which is the default on both sides."
        ),
        denominator_note=(
            "rocks plus Kubernetes charms, not just eligible charms — "
            "<code>run-user</code> is a rock key and <code>charm-user</code> "
            "only affects k8s charms, so machine charms can adopt neither"
        ),
        caveats=(
            "The two halves are not the same process: <code>run-user</code> is "
            "the rock's OCI user (the workload), <code>charm-user</code> is the "
            "user the charm code itself runs as.",
            "<code>sudoer</code> counts towards the headline — it is a non-root "
            "user — but it keeps its own bucket, because it can still escalate "
            "via sudo.",
            "Archived repos and forks are dropped from the rocks half, as are "
            "rocks whose rockcraft.yaml could not be read at scan time.",
        ),
    ),
)


def metric_by_key(key: str) -> Metric | None:
    """Look up a declared metric, or None if `key` names no metric."""
    return next((m for m in METRICS if m.key == key), None)


def compute_series(snapshots: list[Snapshot], *, only: str | None = None) -> dict[str, list[dict]]:
    """Per-metric time series, one point per snapshot the metric can be read from.

    Dates a metric can't be computed for are absent from its list rather than
    present with a null — a consumer plotting the series sees a gap, not a
    dip to zero. A declared-but-undefined metric (`compute=None`) maps to an
    empty list, so the page can still render its card.
    """
    out: dict[str, list[dict]] = {}
    for metric in METRICS:
        if only and metric.key != only:
            continue
        if metric.compute is None:
            out[metric.key] = []
            continue
        points = [metric.compute(s) for s in snapshots]
        out[metric.key] = [p for p in points if p is not None]
    return out


def latest_and_delta(series: list[dict]) -> tuple[dict | None, float | None]:
    """Return the most recent point, and its change since the previous one.

    Returns `(None, None)` for an empty series and `(point, None)` when there
    is only one point — a single reading has nothing to be a delta from, and
    rendering 0.0 there would read as "no movement" rather than "no history".
    """
    if not series:
        return None, None
    latest = series[-1]
    if len(series) < 2:
        return latest, None
    return latest, round(latest["value"] - series[-2]["value"], 1)
