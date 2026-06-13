"""Unit tests for the rule-based scoring layer."""

from __future__ import annotations

import pytest

from ..metadata import CharmMeta, Relation
from ..scoring import (
    SCORE_CLEAR_GAP,
    SCORE_NOT_APPLICABLE,
    SCORE_WORTH_CONSIDERING,
    annotate_present,
    score_absent,
)


def _meta(**kw) -> CharmMeta:
    defaults = dict(
        has_containers=False,
        relations=(),
        config_keys=(),
        secret_like_config=(),
        secret_typed_config=(),
        has_integration_tests=False,
        is_reactive=False,
    )
    defaults.update(kw)
    return CharmMeta(**defaults)


def _rel(name: str, role: str, interface: str = "") -> Relation:
    return Relation(name=name, role=role, interface=interface)


# ── pebble.checks ──────────────────────────────────────────────────────────────


def test_pebble_checks_k8s_charm():
    s = score_absent("pebble.checks", {}, _meta(has_containers=True))
    assert s.label == SCORE_WORTH_CONSIDERING


def test_pebble_checks_machine_charm():
    s = score_absent("pebble.checks", {}, _meta(has_containers=False))
    assert s.label == SCORE_NOT_APPLICABLE


# ── pebble.log-forwarding ──────────────────────────────────────────────────────


def test_pebble_log_forwarding_no_rule_fires():
    # No rule defined — falls through to not-applicable.
    s = score_absent("pebble.log-forwarding", {}, _meta(has_containers=True))
    assert s.label == SCORE_NOT_APPLICABLE


# ── ops.pebble-ready ──────────────────────────────────────────────────────────


def test_pebble_ready_absent_k8s_is_clear_gap():
    s = score_absent("ops.pebble-ready", {}, _meta(has_containers=True))
    assert s.label == SCORE_CLEAR_GAP


def test_pebble_ready_absent_machine_is_na():
    s = score_absent("ops.pebble-ready", {}, _meta(has_containers=False))
    assert s.label == SCORE_NOT_APPLICABLE


# ── ops.pebble-custom-notice ──────────────────────────────────────────────────


def test_pebble_custom_notice_k8s():
    s = score_absent("ops.pebble-custom-notice", {}, _meta(has_containers=True))
    assert s.label == SCORE_WORTH_CONSIDERING


def test_pebble_custom_notice_machine():
    s = score_absent("ops.pebble-custom-notice", {}, _meta(has_containers=False))
    assert s.label == SCORE_NOT_APPLICABLE


# ── ops.collect-status ────────────────────────────────────────────────────────


def test_collect_status_direct_set_is_clear_gap():
    features = {"ops.status-set-directly": {"present": True}}
    s = score_absent("ops.collect-status", features, _meta())
    assert s.label == SCORE_CLEAR_GAP


def test_collect_status_has_relations_is_clear_gap():
    meta = _meta(relations=(_rel("db", "requires", "pgsql"),))
    s = score_absent("ops.collect-status", {}, meta)
    assert s.label == SCORE_CLEAR_GAP


def test_collect_status_peers_only_is_clear_gap():
    # peers counts as a relation — most non-trivial charms have them.
    meta = _meta(relations=(_rel("replicas", "peers", "charm_peers"),))
    s = score_absent("ops.collect-status", {}, meta)
    assert s.label == SCORE_CLEAR_GAP


def test_collect_status_no_relations_is_worth_considering():
    s = score_absent("ops.collect-status", {}, _meta())
    assert s.label == SCORE_WORTH_CONSIDERING


def test_collect_status_status_set_direct_beats_relation_check():
    # Both conditions true — direct-set rule fires first.
    features = {"ops.status-set-directly": {"present": True}}
    meta = _meta(relations=(_rel("db", "requires", "pgsql"),))
    s = score_absent("ops.collect-status", features, meta)
    assert s.label == SCORE_CLEAR_GAP
    assert "directly" in s.rationale


# ── ops.secrets ───────────────────────────────────────────────────────────────


def test_secrets_secret_like_config_is_clear_gap():
    meta = _meta(
        config_keys=("admin-password", "username"),
        secret_like_config=("admin-password",),
    )
    s = score_absent("ops.secrets", {}, meta)
    assert s.label == SCORE_CLEAR_GAP


def test_secrets_no_secret_config_is_na():
    meta = _meta(config_keys=("juju-external-hostname", "port"))
    s = score_absent("ops.secrets", {}, meta)
    assert s.label == SCORE_NOT_APPLICABLE


def test_secrets_empty_charm_is_na():
    s = score_absent("ops.secrets", {}, _meta())
    assert s.label == SCORE_NOT_APPLICABLE


# ── ops.relation-app-data ─────────────────────────────────────────────────────


def test_relation_app_data_provides_is_worth_considering():
    meta = _meta(relations=(_rel("db", "provides", "pgsql"),))
    s = score_absent("ops.relation-app-data", {}, meta)
    assert s.label == SCORE_WORTH_CONSIDERING


def test_relation_app_data_requires_is_worth_considering():
    meta = _meta(relations=(_rel("db", "requires", "pgsql"),))
    s = score_absent("ops.relation-app-data", {}, meta)
    assert s.label == SCORE_WORTH_CONSIDERING


def test_relation_app_data_peers_only_is_na():
    # peers relations do not carry inter-application data in the same way.
    meta = _meta(relations=(_rel("replicas", "peers", "charm_peers"),))
    s = score_absent("ops.relation-app-data", {}, meta)
    assert s.label == SCORE_NOT_APPLICABLE


def test_relation_app_data_no_relations_is_na():
    s = score_absent("ops.relation-app-data", {}, _meta())
    assert s.label == SCORE_NOT_APPLICABLE


def test_relation_app_data_rationale_mentions_count():
    meta = _meta(
        relations=(
            _rel("db", "requires", "pgsql"),
            _rel("ingress", "requires", "ingress"),
        )
    )
    s = score_absent("ops.relation-app-data", {}, meta)
    assert "2" in s.rationale


# ── jubilant.integration-tests ────────────────────────────────────────────────


def test_jubilant_with_integration_tests_is_worth_considering():
    s = score_absent("jubilant.integration-tests", {}, _meta(has_integration_tests=True))
    assert s.label == SCORE_WORTH_CONSIDERING


def test_jubilant_no_integration_tests_is_na():
    s = score_absent("jubilant.integration-tests", {}, _meta())
    assert s.label == SCORE_NOT_APPLICABLE


# ── ops.stored-state ──────────────────────────────────────────────────────────


def test_stored_state_absent_is_na():
    # Absence is the desired state — no recommendation needed.
    s = score_absent("ops.stored-state", {}, _meta())
    assert s.label == SCORE_NOT_APPLICABLE


def test_stored_state_present_gets_migration_note():
    note = annotate_present("ops.stored-state", _meta())
    assert note is not None
    assert note.label == SCORE_WORTH_CONSIDERING
    assert "StoredState" in note.rationale or "stored" in note.rationale.lower()


# ── annotate_present fallthrough ──────────────────────────────────────────────


def test_annotate_present_returns_none_for_most_features():
    meta = _meta()
    for feat in (
        "ops.secrets",
        "ops.collect-status",
        "pebble.checks",
        "jubilant.integration-tests",
        "charmlibs.data-platform",
    ):
        assert annotate_present(feat, meta) is None, f"unexpected note for {feat}"


# ── unknown / uncovered features ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "feature",
    [
        "charmlibs.data-platform",
        "charmlibs.observability",
        "charmlibs.cos-agent",
        "charmlibs.rolling-ops",
        "ops.leader-events",
        "ops.open-ports",
        "ops.action-handlers",
        "ops-scenario.unit-tests",
        "pebble.restart-delay",
    ],
)
def test_uncovered_feature_is_not_applicable(feature: str):
    s = score_absent(feature, {}, _meta())
    assert s.label == SCORE_NOT_APPLICABLE


# ── reactive charms ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "feature",
    [
        "ops.pebble-ready",
        "ops.collect-status",
        "ops.secrets",
        "ops.relation-app-data",
        "pebble.checks",
        "jubilant.integration-tests",
        "charmlibs.observability",
    ],
)
def test_reactive_charm_short_circuits_to_not_applicable(feature: str):
    """A reactive charm gets NA for every feature, even those that would
    otherwise score clear-gap or worth-considering — the ops feature
    catalogue doesn't apply to charms.reactive."""
    # Build meta that WOULD otherwise produce strong signals
    meta = _meta(
        is_reactive=True,
        has_containers=True,
        relations=(_rel("db", "requires", "pgsql"),),
        config_keys=("admin-password",),
        secret_like_config=("admin-password",),
        has_integration_tests=True,
    )
    features = {"ops.status-set-directly": {"present": True}}
    s = score_absent(feature, features, meta)
    assert s.label == SCORE_NOT_APPLICABLE
    assert "reactive" in s.rationale.lower()


# ── legacy classic charms ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "feature",
    [
        "ops.pebble-ready",
        "ops.collect-status",
        "ops.secrets",
        "ops.relation-app-data",
        "pebble.checks",
        "jubilant.integration-tests",
        "charmlibs.observability",
    ],
)
def test_legacy_classic_charm_short_circuits_to_not_applicable(feature: str):
    """A pre-ops hooks-driven charm gets NA for every feature."""
    meta = _meta(
        is_legacy_classic=True,
        relations=(_rel("db", "requires", "pgsql"),),
        config_keys=("admin-password",),
        secret_like_config=("admin-password",),
        has_integration_tests=True,
    )
    s = score_absent(feature, {}, meta)
    assert s.label == SCORE_NOT_APPLICABLE
    assert "legacy classic" in s.rationale.lower()


# ── architecture-axis short-circuits ─────────────────────────────────────────


@pytest.mark.parametrize(
    "architecture,expected_keyword",
    [
        (["component-graph"], "component-graph"),
        (["reconcile-all"], "reconcile-all"),
        (["reconcile"], "reconcile-style"),
        (["unconditional-init"], "unconditional-init"),
        (["reconcile-all", "reconcile"], "reconcile-all"),  # reconcile-all wins (checked first)
    ],
)
@pytest.mark.parametrize("feature", ["ops.pebble-ready", "ops.collect-status"])
def test_architecture_short_circuits_feature_to_na(
    architecture: list[str],
    expected_keyword: str,
    feature: str,
):
    """A holistic / component-graph charm shouldn't get clear-gap on
    pebble-ready or collect-status — the framework or shared reconcile
    handler covers those events even when the per-feature detector
    can't see it."""
    meta = _meta(
        has_containers=True,
        relations=(_rel("db", "requires", "pgsql"),),
    )
    features = {"ops.status-set-directly": {"present": True}}
    s = score_absent(feature, features, meta, architecture)
    assert s.label == SCORE_NOT_APPLICABLE
    assert expected_keyword in s.rationale


def test_architecture_does_not_affect_other_features():
    """The architecture short-circuit only fires for pebble-ready and
    collect-status — other features still get their normal scoring."""
    meta = _meta(
        config_keys=("admin-password",),
        secret_like_config=("admin-password",),
    )
    s = score_absent("ops.secrets", {}, meta, ["component-graph"])
    assert s.label == SCORE_CLEAR_GAP  # component-graph doesn't manage secrets


def test_no_architecture_argument_works_back_compat():
    """score_absent's architecture argument is optional."""
    meta = _meta(has_containers=True)
    s = score_absent("ops.pebble-ready", {}, meta)
    assert s.label == SCORE_CLEAR_GAP  # standard rule still fires


def test_reactive_short_circuit_beats_architecture():
    """A reactive charm gets NA on the reactive rule before architecture
    matters (reactive charms predate ops; architecture axis is meaningless)."""
    meta = _meta(is_reactive=True, has_containers=True)
    s = score_absent("ops.pebble-ready", {}, meta, ["component-graph"])
    assert s.label == SCORE_NOT_APPLICABLE
    assert "reactive" in s.rationale.lower()


# ── ops.secrets: type: secret config (CALIBRATION #13) ──────────────────────


def test_secrets_all_typed_is_na():
    """All secret-like options declared `type: secret` ⇒ uses Juju secrets."""
    meta = _meta(
        config_keys=("san-password", "admin-password"),
        secret_like_config=("san-password", "admin-password"),
        secret_typed_config=("san-password", "admin-password"),
    )
    s = score_absent("ops.secrets", {}, meta)
    assert s.label == SCORE_NOT_APPLICABLE
    assert "type: secret" in s.rationale or "type" in s.rationale


def test_secrets_partial_typed_is_worth_considering():
    """Some typed, some not — partial adoption is worth-considering, not clear-gap."""
    meta = _meta(
        config_keys=("san-password", "admin-token"),
        secret_like_config=("san-password", "admin-token"),
        secret_typed_config=("san-password",),  # only one typed
    )
    s = score_absent("ops.secrets", {}, meta)
    assert s.label == SCORE_WORTH_CONSIDERING
    assert "admin-token" in s.rationale


def test_secrets_none_typed_still_clear_gap():
    """Existing behaviour preserved when no config is typed."""
    meta = _meta(
        config_keys=("admin-password",),
        secret_like_config=("admin-password",),
        secret_typed_config=(),
    )
    s = score_absent("ops.secrets", {}, meta)
    assert s.label == SCORE_CLEAR_GAP
