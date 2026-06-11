"""Rule-based scoring (PLAN.md §C).

For each absent feature, emit `not-applicable | worth-considering | clear-gap`
plus a one-line rationale. Default for unmatched cases is `not-applicable`
with rationale "no rule fired" so the dashboard can filter on it.

The rules are deterministic and explicit — easy to audit, easy to add to.
LLM refinement is a later optional pass (see PLAN.md §C "LLM-assisted").
"""

from __future__ import annotations

from dataclasses import dataclass

from .metadata import CharmMeta

SCORE_NOT_APPLICABLE = "not-applicable"
SCORE_WORTH_CONSIDERING = "worth-considering"
SCORE_CLEAR_GAP = "clear-gap"


@dataclass(frozen=True)
class Score:
    label: str
    rationale: str


def _is_present(features: dict[str, dict], name: str) -> bool:
    return bool(features.get(name, {}).get("present"))


def score_absent(
    feature_name: str,
    features: dict[str, dict],
    meta: CharmMeta,
    architecture: list[str] | None = None,
) -> Score:
    """Score a feature that is *absent* from the charm.

    `features` is the per-charm features dict from scan output, used so rules
    can reference *other* features' presence (e.g. "has integration tests but
    no jubilant"). `architecture` is the list of pattern names that matched
    the charm (see `catalogue.load_patterns`); used to suppress gap rules
    for charms that delegate the feature to a framework or shared reconcile.
    """
    architecture = architecture or []

    # Reactive charms (charms.reactive framework) predate ops; none of the
    # ops.* / pebble.* / charmlibs.* / jubilant.* features in the catalogue
    # apply. Short-circuit before any per-feature rule so the dashboard
    # doesn't suggest migrations that aren't even meaningful for this charm.
    if meta.is_reactive:
        return Score(
            SCORE_NOT_APPLICABLE,
            "reactive charm (charms.reactive framework) — ops feature catalogue does not apply",
        )

    # Architecture-axis short-circuits (PLAN.md "Architecture axis").
    # A charm in the holistic family or using a component-graph framework
    # delegates event wiring (including pebble-ready) and status aggregation
    # to a shared reconcile / framework, so the per-feature absence rules
    # don't apply.
    if feature_name in ("ops.pebble-ready", "ops.collect-status"):
        if "component-graph" in architecture:
            return Score(
                SCORE_NOT_APPLICABLE,
                "component-graph charm (chisme / ops_sunbeam / coordinated_workers) — framework handles this",
            )
        if "reconcile-all" in architecture:
            return Score(
                SCORE_NOT_APPLICABLE,
                "reconcile-all charm — single handler bound to every event covers this",
            )
        if "reconcile" in architecture:
            return Score(SCORE_NOT_APPLICABLE, "reconcile-style charm — shared reconcile handler covers this")
        if "unconditional-init" in architecture:
            return Score(
                SCORE_NOT_APPLICABLE,
                "unconditional-init charm — reconcile runs from __init__ on every event",
            )

    if feature_name == "pebble.checks":
        if meta.has_containers:
            return Score(
                SCORE_WORTH_CONSIDERING,
                "K8s workload charm (has `containers:`) — pebble checks are the standard health-check mechanism",
            )
        return Score(SCORE_NOT_APPLICABLE, "not a K8s workload charm")

    if feature_name == "ops.pebble-custom-notice":
        if meta.has_containers:
            return Score(
                SCORE_WORTH_CONSIDERING,
                "K8s workload charm — custom notices let the workload signal events back to the charm",
            )
        return Score(SCORE_NOT_APPLICABLE, "not a K8s workload charm")

    if feature_name == "ops.pebble-ready":
        if meta.has_containers:
            return Score(
                SCORE_CLEAR_GAP,
                "K8s workload charm with no pebble-ready handler — almost certainly missing setup logic",
            )
        return Score(SCORE_NOT_APPLICABLE, "not a K8s workload charm")

    if feature_name == "ops.relation-app-data":
        non_peer = [r for r in meta.relations if r.role != "peers"]
        if non_peer:
            return Score(
                SCORE_WORTH_CONSIDERING,
                f"charm has {len(non_peer)} provides/requires relations — app-scope data is the canonical bag",
            )
        return Score(SCORE_NOT_APPLICABLE, "no provides/requires relations")

    if feature_name == "ops.collect-status":
        # Direct status assignment is the strongest signal: the charm is already
        # setting status but bypassing the collect-status machinery.
        if _is_present(features, "ops.status-set-directly"):
            return Score(
                SCORE_CLEAR_GAP,
                "charm sets status directly — migrate to collect-status for proper status aggregation",
            )
        # PLAN.md: "Has active relations but no CollectAppStatusEvent" → clear gap.
        # Read "active relations" as having any provides/requires/peers — most
        # non-trivial charms qualify.
        if meta.relations:
            return Score(
                SCORE_CLEAR_GAP,
                "charm has integrations — collect-status is the right way to aggregate status across handlers",
            )
        return Score(
            SCORE_WORTH_CONSIDERING,
            "small charm — collect-status is still good practice but lower-priority",
        )

    if feature_name == "ops.secrets":
        # CALIBRATION #13: Juju 3.3+ `type: secret` config options ARE
        # using the Juju secrets API — the operator creates a Juju secret
        # out-of-band, the charm reads `self.config[name]` and Juju
        # transparently resolves the URI to the secret content. No
        # `model.get_secret(...)` call needed.
        typed = set(meta.secret_typed_config)
        secret_like = set(meta.secret_like_config)
        if secret_like and secret_like <= typed:
            return Score(
                SCORE_NOT_APPLICABLE,
                "all secret-like config options declare `type: secret` — uses Juju secrets API",
            )
        if secret_like and typed:
            still_plain = sorted(secret_like - typed)
            names = ", ".join(still_plain[:3])
            suffix = "…" if len(still_plain) > 3 else ""
            return Score(
                SCORE_WORTH_CONSIDERING,
                f"some secret-like options still plain-typed ({names}{suffix}); rest already use `type: secret`",
            )
        if secret_like:
            names = ", ".join(sorted(secret_like)[:3])
            suffix = "…" if len(secret_like) > 3 else ""
            return Score(
                SCORE_CLEAR_GAP,
                f"config has secret-like options ({names}{suffix}) — should use Juju secrets API",
            )
        return Score(SCORE_NOT_APPLICABLE, "no obviously sensitive config options")

    if feature_name == "jubilant.integration-tests":
        if meta.has_integration_tests:
            return Score(
                SCORE_WORTH_CONSIDERING,
                "charm has tests/integration/ but no jubilant import — likely using older harness/pytest-operator",
            )
        return Score(SCORE_NOT_APPLICABLE, "no integration tests present")

    if feature_name == "ops.stored-state":
        # Special case: presence is a flag for *migration away from*. Absence
        # is the desired state.
        return Score(SCORE_NOT_APPLICABLE, "absence is desirable — StoredState is discouraged for new code")

    return Score(SCORE_NOT_APPLICABLE, "no rule defined for this feature")


def annotate_present(feature_name: str, meta: CharmMeta) -> Score | None:
    """For features whose *presence* warrants a note (e.g. StoredState migration)."""
    if feature_name == "ops.stored-state":
        return Score(
            SCORE_WORTH_CONSIDERING,
            "StoredState is discouraged — migrate to peer-relation data or external state",
        )
    return None
