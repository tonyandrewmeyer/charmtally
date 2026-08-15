"""Tests for the AST detector kinds added for the holistic-family
architecture patterns (part-reconcile / unconditional-init)."""

from __future__ import annotations

from pathlib import Path

from ..catalogue import Detector, Feature, default_path
from ..catalogue import load as catalogue_load
from ..detectors import detect_feature


def _feature(detector_kind: str, **cfg) -> Feature:
    return Feature(
        name="test",
        library="ops",
        summary="test",
        scope="src",
        detectors=(Detector(kind=detector_kind, config=cfg),),
    )


def _write_charm(tmp_path: Path, code: str) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "charm.py").write_text(code)
    (tmp_path / "charmcraft.yaml").write_text("type: charm\nname: t\n")
    return tmp_path


# ── ast-init-call (unconditional-init pattern) ───────────────────────────────


def test_ast_init_call_fires_on_reconcile_in_init(tmp_path: Path) -> None:
    _write_charm(
        tmp_path,
        """
class MyCharm:
    def __init__(self, framework):
        self.framework = framework
        self._reconcile()

    def _reconcile(self):
        pass
""",
    )
    ev = detect_feature(tmp_path, _feature("ast-init-call", attrs=["_reconcile", "reconcile"]))
    assert len(ev) == 1
    assert ev[0].detector_kind == "ast-init-call"
    assert "_reconcile()" in ev[0].snippet


def test_ast_init_call_no_match_when_reconcile_only_in_handler(tmp_path: Path) -> None:
    """A class that has _reconcile but only calls it from handlers (not init)
    is the part-reconcile pattern, not unconditional-init."""
    _write_charm(
        tmp_path,
        """
class MyCharm:
    def __init__(self, framework):
        self.framework = framework

    def _on_config_changed(self, event):
        self._reconcile()

    def _reconcile(self):
        pass
""",
    )
    ev = detect_feature(tmp_path, _feature("ast-init-call", attrs=["_reconcile"]))
    assert ev == []


def test_ast_init_call_matches_any_attr_in_list(tmp_path: Path) -> None:
    """attrs config is a set; any one matching is enough."""
    _write_charm(
        tmp_path,
        """
class MyCharm:
    def __init__(self, framework):
        self._update_charm()

    def _update_charm(self):
        pass
""",
    )
    ev = detect_feature(tmp_path, _feature("ast-init-call", attrs=["_reconcile", "_update_charm"]))
    assert len(ev) == 1


# ── ast-shared-method (part-reconcile pattern) ───────────────────────────────


def test_ast_shared_method_fires_at_min_callers(tmp_path: Path) -> None:
    _write_charm(
        tmp_path,
        """
class MyCharm:
    def __init__(self, framework):
        self.framework = framework

    def _on_install(self, event):
        self._reconcile()

    def _on_config_changed(self, event):
        self._reconcile()

    def _reconcile(self):
        pass
""",
    )
    ev = detect_feature(
        tmp_path, _feature("ast-shared-method", attrs=["_reconcile"], min_callers=2)
    )
    assert len(ev) == 2  # one evidence per qualifying caller
    assert all(e.detector_kind == "ast-shared-method" for e in ev)


def test_ast_shared_method_no_match_below_min_callers(tmp_path: Path) -> None:
    """Single handler delegating to reconcile isn't part-reconcile yet —
    that's just delta-with-helper."""
    _write_charm(
        tmp_path,
        """
class MyCharm:
    def __init__(self, framework): pass
    def _on_install(self, event):
        self._reconcile()
    def _on_config_changed(self, event):
        pass
    def _reconcile(self): pass
""",
    )
    ev = detect_feature(
        tmp_path, _feature("ast-shared-method", attrs=["_reconcile"], min_callers=2)
    )
    assert ev == []


def test_ast_shared_method_ignores_non_handler_methods(tmp_path: Path) -> None:
    """helper methods named without the _on_ prefix don't count toward
    the caller threshold."""
    _write_charm(
        tmp_path,
        """
class MyCharm:
    def __init__(self, framework): pass
    def _helper_one(self):
        self._reconcile()
    def _helper_two(self):
        self._reconcile()
    def _on_install(self, event):
        self._reconcile()
    def _reconcile(self): pass
""",
    )
    ev = detect_feature(
        tmp_path, _feature("ast-shared-method", attrs=["_reconcile"], min_callers=2)
    )
    # Only 1 _on_* caller, below threshold even though two helpers also call.
    assert ev == []


def test_ast_shared_method_custom_handler_re(tmp_path: Path) -> None:
    """The handler_re config lets the pattern match e.g. `_handle_*`."""
    _write_charm(
        tmp_path,
        """
class MyCharm:
    def __init__(self, framework): pass
    def _handle_install(self, event):
        self._reconcile()
    def _handle_change(self, event):
        self._reconcile()
    def _reconcile(self): pass
""",
    )
    ev = detect_feature(
        tmp_path,
        _feature(
            "ast-shared-method", attrs=["_reconcile"], min_callers=2, handler_re=r"^_handle_"
        ),
    )
    assert len(ev) == 2


# testing.caplog regex


def _caplog_feature() -> Feature:
    return Feature(
        name="testing.caplog",
        library="python",
        summary="test",
        scope="tests",
        detectors=(
            Detector(kind="regex", config={"pattern": r"def\s+test_\w*\s*\([^)]*\bcaplog\b"}),
        ),
    )


def _write_test_file(tmp_path: Path, body: str) -> Path:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_thing.py").write_text(body)
    (tmp_path / "charmcraft.yaml").write_text("type: charm\nname: t\n")
    return tmp_path


def test_caplog_fires_on_test_function_with_caplog(tmp_path: Path) -> None:
    _write_test_file(
        tmp_path,
        "def test_logs_warning(caplog):\n    assert caplog.records == []\n",
    )
    ev = detect_feature(tmp_path, _caplog_feature())
    assert len(ev) == 1
    assert "caplog" in ev[0].snippet


def test_caplog_fires_with_extra_args_around(tmp_path: Path) -> None:
    _write_test_file(
        tmp_path,
        "def test_logs(monkeypatch, caplog, tmp_path):\n    pass\n",
    )
    ev = detect_feature(tmp_path, _caplog_feature())
    assert len(ev) == 1


def test_caplog_does_not_fire_on_non_test_function(tmp_path: Path) -> None:
    _write_test_file(
        tmp_path,
        "def helper_logs(caplog):\n    pass\n",
    )
    ev = detect_feature(tmp_path, _caplog_feature())
    assert ev == []


def test_caplog_does_not_fire_on_usage_without_fixture_param(tmp_path: Path) -> None:
    _write_test_file(
        tmp_path,
        "def test_thing():\n    caplog = make_caplog()\n    assert caplog\n",
    )
    ev = detect_feature(tmp_path, _caplog_feature())
    assert ev == []


# ── pytest-config-key (testing.pytest-log-config) ────────────────────────────


def _pytest_log_feature() -> Feature:
    return Feature(
        name="testing.pytest-log-config",
        library="python",
        summary="t",
        scope="tests",
        detectors=(
            Detector(
                kind="pytest-config-key",
                config={"keys": ["log_level", "log_cli_level", "log_file_level"]},
            ),
        ),
    )


def _seed_charm_root(tmp_path: Path) -> Path:
    (tmp_path / "charmcraft.yaml").write_text("type: charm\nname: t\n")
    return tmp_path


def test_pytest_log_config_pyproject_toml(tmp_path: Path) -> None:
    _seed_charm_root(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\nlog_level = 'INFO'\n")
    ev = detect_feature(tmp_path, _pytest_log_feature())
    assert len(ev) == 1
    assert ev[0].file == "pyproject.toml"
    assert "log_level" in ev[0].snippet


def test_pytest_log_config_pytest_ini(tmp_path: Path) -> None:
    _seed_charm_root(tmp_path)
    (tmp_path / "pytest.ini").write_text("[pytest]\nlog_cli_level = DEBUG\n")
    ev = detect_feature(tmp_path, _pytest_log_feature())
    assert len(ev) == 1
    assert ev[0].file == "pytest.ini"


def test_pytest_log_config_setup_cfg(tmp_path: Path) -> None:
    _seed_charm_root(tmp_path)
    (tmp_path / "setup.cfg").write_text("[tool:pytest]\nlog_file_level = WARNING\n")
    ev = detect_feature(tmp_path, _pytest_log_feature())
    assert len(ev) == 1
    assert ev[0].file == "setup.cfg"


def test_pytest_log_config_tox_ini(tmp_path: Path) -> None:
    _seed_charm_root(tmp_path)
    (tmp_path / "tox.ini").write_text("[pytest]\nlog_level = INFO\n")
    ev = detect_feature(tmp_path, _pytest_log_feature())
    assert len(ev) == 1
    assert ev[0].file == "tox.ini"


def test_pytest_log_config_none_present(tmp_path: Path) -> None:
    _seed_charm_root(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\nminversion = '7.0'\n")
    (tmp_path / "pytest.ini").write_text("[pytest]\naddopts = -v\n")
    ev = detect_feature(tmp_path, _pytest_log_feature())
    assert ev == []


def test_pytest_log_config_wrong_section_in_pyproject(tmp_path: Path) -> None:
    """A `log_level` key outside [tool.pytest.ini_options] must not match."""
    _seed_charm_root(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[tool.something_else]\nlog_level = 'INFO'\n")
    ev = detect_feature(tmp_path, _pytest_log_feature())
    assert ev == []


def test_pytest_log_config_multiple_files_fanout(tmp_path: Path) -> None:
    """Both pyproject.toml and pytest.ini set keys — both surface as evidence."""
    _seed_charm_root(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\nlog_level = 'INFO'\n")
    (tmp_path / "pytest.ini").write_text("[pytest]\nlog_cli_level = DEBUG\n")
    ev = detect_feature(tmp_path, _pytest_log_feature())
    assert {e.file for e in ev} == {"pyproject.toml", "pytest.ini"}


def test_pytest_log_config_malformed_toml_treated_as_absent(tmp_path: Path) -> None:
    _seed_charm_root(tmp_path)
    (tmp_path / "pyproject.toml").write_text("this is not toml [[[\n")
    ev = detect_feature(tmp_path, _pytest_log_feature())
    assert ev == []


def test_pytest_log_config_with_comments_tolerated(tmp_path: Path) -> None:
    """A real charm's tox.ini often has comments mixed with keys."""
    _seed_charm_root(tmp_path)
    (tmp_path / "tox.ini").write_text(
        "# pytest settings\n[pytest]\n# verbose logging during CI\nlog_level = DEBUG\naddopts = -v\n"
    )
    ev = detect_feature(tmp_path, _pytest_log_feature())
    assert len(ev) == 1


# ── ast-observe-shared-handler (reconcile pattern) ───────────────────────────


def _reconcile_feature(**overrides) -> Feature:
    cfg = {"min_events": 3, "exclude_suffixes": ["_error"]}
    cfg.update(overrides)
    return Feature(
        name="reconcile",
        library="ops",
        summary="t",
        scope="src",
        detectors=(Detector(kind="ast-observe-shared-handler", config=cfg),),
    )


def test_reconcile_fires_when_handler_bound_to_3_events(tmp_path: Path) -> None:
    _write_charm(
        tmp_path,
        """
class C:
    def __init__(self, framework):
        self.framework.observe(self.on.install, self._reconcile)
        self.framework.observe(self.on.config_changed, self._reconcile)
        self.framework.observe(self.on.leader_elected, self._reconcile)
    def _reconcile(self, event): pass
""",
    )
    ev = detect_feature(tmp_path, _reconcile_feature())
    assert len(ev) == 3
    assert all(e.detector_kind == "ast-observe-shared-handler" for e in ev)


def test_reconcile_misses_when_only_2_events(tmp_path: Path) -> None:
    """Two shared bindings is a single-responsibility pattern, not reconcile."""
    _write_charm(
        tmp_path,
        """
class C:
    def __init__(self, framework):
        self.framework.observe(self.on.leader_elected, self._on_leader)
        self.framework.observe(self.on.leader_settings_changed, self._on_leader)
    def _on_leader(self, event): pass
""",
    )
    ev = detect_feature(tmp_path, _reconcile_feature())
    assert ev == []


def test_reconcile_handler_name_independent(tmp_path: Path) -> None:
    """The bind-count, not the handler name, is the signal."""
    _write_charm(
        tmp_path,
        """
class C:
    def __init__(self, framework):
        self.framework.observe(self.on.install, self._handle)
        self.framework.observe(self.on.config_changed, self._handle)
        self.framework.observe(self.on.upgrade_charm, self._handle)
        self.framework.observe(self.on.leader_elected, self._handle)
    def _handle(self, event): pass
""",
    )
    ev = detect_feature(tmp_path, _reconcile_feature())
    assert len(ev) == 4


def test_reconcile_excludes_error_events(tmp_path: Path) -> None:
    """3 error events into one handler is not reconcile — they're routed errors."""
    _write_charm(
        tmp_path,
        """
class C:
    def __init__(self, framework):
        self.framework.observe(self.on.collect_error, self._on_error)
        self.framework.observe(self.on.config_error, self._on_error)
        self.framework.observe(self.on.relation_error, self._on_error)
    def _on_error(self, event): pass
""",
    )
    ev = detect_feature(tmp_path, _reconcile_feature())
    assert ev == []


def test_reconcile_subscripted_events_use_trailing_attr(tmp_path: Path) -> None:
    """`self.on['c'].pebble_ready` resolves to event name 'pebble_ready'."""
    _write_charm(
        tmp_path,
        """
class C:
    def __init__(self, framework):
        self.framework.observe(self.on['db'].pebble_ready, self._reconcile)
        self.framework.observe(self.on.install, self._reconcile)
        self.framework.observe(self.on.config_changed, self._reconcile)
    def _reconcile(self, event): pass
""",
    )
    ev = detect_feature(tmp_path, _reconcile_feature())
    assert len(ev) == 3


def test_reconcile_ignores_loop_variable_events(tmp_path: Path) -> None:
    """`for ev in self.on.events().values(): observe(ev, ...)` — `ev` is a
    Name, not an Attribute, so this is NOT counted as reconcile (it's
    `reconcile-all`, a different pattern)."""
    _write_charm(
        tmp_path,
        """
class C:
    def __init__(self, framework):
        for ev in self.on.events().values():
            self.framework.observe(ev, self._reconcile)
    def _reconcile(self, event): pass
""",
    )
    ev = detect_feature(tmp_path, _reconcile_feature())
    assert ev == []


def test_reconcile_distinct_handlers_dont_aggregate(tmp_path: Path) -> None:
    """Two different handlers, each bound to 2 events: no reconcile signal."""
    _write_charm(
        tmp_path,
        """
class C:
    def __init__(self, framework):
        self.framework.observe(self.on.install, self._on_setup)
        self.framework.observe(self.on.upgrade_charm, self._on_setup)
        self.framework.observe(self.on.start, self._on_run)
        self.framework.observe(self.on.config_changed, self._on_run)
    def _on_setup(self, event): pass
    def _on_run(self, event): pass
""",
    )
    ev = detect_feature(tmp_path, _reconcile_feature())
    assert ev == []


# ── CALIBRATION #21 cut #1: relation-scoped shared-handler exclusion ────────


def test_reconcile_excludes_single_relation_lifecycle(tmp_path: Path) -> None:
    """chopsticks shape: one peer relation's own lifecycle bound to a single
    handler is relation-scoped plumbing, not reconcile."""
    _write_charm(
        tmp_path,
        """
class C:
    def __init__(self, framework):
        self.framework.observe(self.on.cluster_relation_joined, self._on_cluster_changed)
        self.framework.observe(self.on.cluster_relation_changed, self._on_cluster_changed)
        self.framework.observe(self.on.cluster_relation_departed, self._on_cluster_changed)
    def _on_cluster_changed(self, event): pass
""",
    )
    ev = detect_feature(tmp_path, _reconcile_feature())
    assert ev == []


def test_reconcile_excludes_single_relation_lifecycle_discourse_shape(tmp_path: Path) -> None:
    """discourse-k8s-operator shape: one oauth relation's lifecycle regenerating
    client config is relation-scoped, not charm-wide convergence."""
    _write_charm(
        tmp_path,
        """
class C:
    def __init__(self, framework):
        self.framework.observe(self.on.oauth_relation_created, self._on_oauth_relation_changed)
        self.framework.observe(self.on.oauth_relation_joined, self._on_oauth_relation_changed)
        self.framework.observe(self.on.oauth_relation_changed, self._on_oauth_relation_changed)
    def _on_oauth_relation_changed(self, event): pass
""",
    )
    ev = detect_feature(tmp_path, _reconcile_feature())
    assert ev == []


def test_reconcile_excludes_two_mirrored_relations_standard_lifecycle(tmp_path: Path) -> None:
    """loki-k8s-operator shape: two mirrored relation endpoints, each
    contributing only standard lifecycle events, is still relation-scoped."""
    _write_charm(
        tmp_path,
        """
class C:
    def __init__(self, framework):
        self.framework.observe(self.on.grafana_source_relation_created, self._on_grafana_source_changed)
        self.framework.observe(self.on.grafana_source_relation_joined, self._on_grafana_source_changed)
        self.framework.observe(self.on.grafana_source_relation_changed, self._on_grafana_source_changed)
        self.framework.observe(self.on.send_datasource_relation_created, self._on_grafana_source_changed)
        self.framework.observe(self.on.send_datasource_relation_joined, self._on_grafana_source_changed)
        self.framework.observe(self.on.send_datasource_relation_changed, self._on_grafana_source_changed)
    def _on_grafana_source_changed(self, event): pass
""",
    )
    ev = detect_feature(tmp_path, _reconcile_feature())
    assert ev == []


def test_reconcile_fires_when_three_or_more_relation_endpoints(tmp_path: Path) -> None:
    """Cut #1 caps at 2 relation endpoints — a handler spanning 3+ relations'
    lifecycles is charm-wide convergence again, not narrow plumbing."""
    _write_charm(
        tmp_path,
        """
class C:
    def __init__(self, framework):
        self.framework.observe(self.on.db_relation_changed, self._reconcile)
        self.framework.observe(self.on.cache_relation_changed, self._reconcile)
        self.framework.observe(self.on.mq_relation_changed, self._reconcile)
    def _reconcile(self, event): pass
""",
    )
    ev = detect_feature(tmp_path, _reconcile_feature())
    assert len(ev) == 3


# ── CALIBRATION #22 follow-up #7: bare-prefix relation endpoint ─────────────


def test_reconcile_excludes_bare_relation_prefix_single_endpoint(tmp_path: Path) -> None:
    """cos-coordinated-workers shape: `self.on[relation_name].relation_created`
    (dynamic/reusable relation name) produces a bare `relation_created` event
    with an empty prefix. One such bare endpoint's standard lifecycle quintet
    is still relation-scoped plumbing, not reconcile."""
    _write_charm(
        tmp_path,
        """
class C:
    def __init__(self, framework, relation_name):
        self.framework.observe(self.on[relation_name].relation_created, self._on_cluster_changed)
        self.framework.observe(self.on[relation_name].relation_joined, self._on_cluster_changed)
        self.framework.observe(self.on[relation_name].relation_changed, self._on_cluster_changed)
        self.framework.observe(self.on[relation_name].relation_departed, self._on_cluster_changed)
        self.framework.observe(self.on[relation_name].relation_broken, self._on_cluster_changed)
    def _on_cluster_changed(self, event): pass
""",
    )
    ev = detect_feature(tmp_path, _reconcile_feature())
    assert ev == []


def test_reconcile_excludes_bare_plus_one_named_relation_prefix(tmp_path: Path) -> None:
    """Named-prefix behaviour is unchanged by the bare-prefix fix: a bare
    endpoint (1) plus one named-prefix endpoint (2) is still <= 2 relation
    endpoints, so it's excluded the same as two named prefixes would be."""
    _write_charm(
        tmp_path,
        """
class C:
    def __init__(self, framework, relation_name):
        self.framework.observe(self.on[relation_name].relation_created, self._on_changed)
        self.framework.observe(self.on[relation_name].relation_changed, self._on_changed)
        self.framework.observe(self.on.cluster_relation_changed, self._on_changed)
    def _on_changed(self, event): pass
""",
    )
    ev = detect_feature(tmp_path, _reconcile_feature())
    assert ev == []


def test_reconcile_fires_when_bare_prefix_pushes_past_two_endpoints(tmp_path: Path) -> None:
    """Boundary at the <= 2 count: a bare-prefix endpoint plus two distinct
    named-prefix endpoints is 3 relation endpoints total -- past cut #1's cap,
    so this is charm-wide convergence again, not narrow plumbing."""
    _write_charm(
        tmp_path,
        """
class C:
    def __init__(self, framework, relation_name):
        self.framework.observe(self.on[relation_name].relation_changed, self._reconcile)
        self.framework.observe(self.on.db_relation_changed, self._reconcile)
        self.framework.observe(self.on.cache_relation_changed, self._reconcile)
    def _reconcile(self, event): pass
""",
    )
    ev = detect_feature(tmp_path, _reconcile_feature())
    assert len(ev) == 3


# ── CALIBRATION #21 cut #2: symmetric-resource fan-out exclusion ────────────


def test_reconcile_excludes_symmetric_storage_detaching(tmp_path: Path) -> None:
    """mysql-operators shape: N symmetric storage mounts' `*_storage_detaching`
    events into one cleanup handler is resource fan-out, not reconcile."""
    _write_charm(
        tmp_path,
        """
class C:
    def __init__(self, framework):
        self.framework.observe(self.on.data_storage_detaching, self._on_storage_detaching)
        self.framework.observe(self.on.logs_storage_detaching, self._on_storage_detaching)
        self.framework.observe(self.on.certs_storage_detaching, self._on_storage_detaching)
        self.framework.observe(self.on.config_storage_detaching, self._on_storage_detaching)
    def _on_storage_detaching(self, event): pass
""",
    )
    ev = detect_feature(tmp_path, _reconcile_feature())
    assert ev == []


def test_reconcile_excludes_symmetric_pebble_ready(tmp_path: Path) -> None:
    """Same fan-out shape for N containers' `*_pebble_ready` events."""
    _write_charm(
        tmp_path,
        """
class C:
    def __init__(self, framework):
        self.framework.observe(self.on.api_pebble_ready, self._on_any_pebble_ready)
        self.framework.observe(self.on.worker_pebble_ready, self._on_any_pebble_ready)
        self.framework.observe(self.on.scheduler_pebble_ready, self._on_any_pebble_ready)
    def _on_any_pebble_ready(self, event): pass
""",
    )
    ev = detect_feature(tmp_path, _reconcile_feature())
    assert ev == []


def test_reconcile_fires_when_resource_suffixes_mixed(tmp_path: Path) -> None:
    """Mixing storage-detaching and pebble-ready events isn't a single
    symmetric-resource fan-out (two different suffixes) — still reconcile."""
    _write_charm(
        tmp_path,
        """
class C:
    def __init__(self, framework):
        self.framework.observe(self.on.data_storage_detaching, self._reconcile)
        self.framework.observe(self.on.api_pebble_ready, self._reconcile)
        self.framework.observe(self.on.worker_pebble_ready, self._reconcile)
    def _reconcile(self, event): pass
""",
    )
    ev = detect_feature(tmp_path, _reconcile_feature())
    assert len(ev) == 3


# ── CALIBRATION #21 regression: strong-TP with no naming tells stays reconcile ──


def test_reconcile_regression_kfp_api_shape_still_fires(tmp_path: Path) -> None:
    """kfp-api's `_on_event` — heterogeneous lifecycle/config/relation events
    funnelled into one full-state-convergence handler, no naming tells — must
    still classify as reconcile after both CALIBRATION #21 cuts land."""
    _write_charm(
        tmp_path,
        """
class C:
    def __init__(self, framework):
        self.framework.observe(self.on.install, self._on_event)
        self.framework.observe(self.on.config_changed, self._on_event)
        self.framework.observe(self.on.leader_elected, self._on_event)
        self.framework.observe(self.on.mysql_relation_changed, self._on_event)
        self.framework.observe(self.on.kfp_api_pebble_ready, self._on_event)
        self.framework.observe(self.on.upgrade_charm, self._on_event)
    def _on_event(self, event):
        self._check_leader()
        self._check_config()
        self._apply_k8s_resources()
        self._reconcile_authorization_policies()
        self._ensure_bucket_exists()
        self.update_layer()
""",
    )
    ev = detect_feature(tmp_path, _reconcile_feature())
    assert len(ev) == 6


# ── CALIBRATION #22 follow-up #6 / #24: baseline-lifecycle-only exclusion ───


def test_reconcile_excludes_baseline_lifecycle_only(tmp_path: Path) -> None:
    """`install`/`config_changed`/`upgrade_charm` alone is idempotent-
    reconfiguration boilerplate, not reconcile (CALIBRATION #22 follow-up
    #6 / #24 — charmed-linstor/linstor-controller, auditd-operator,
    chrony-client-operator all share exactly this shape)."""
    _write_charm(
        tmp_path,
        """
class C:
    def __init__(self, framework):
        self.framework.observe(self.on.install, self._set_pod_spec)
        self.framework.observe(self.on.config_changed, self._set_pod_spec)
        self.framework.observe(self.on.upgrade_charm, self._set_pod_spec)
    def _set_pod_spec(self, event): pass
""",
    )
    ev = detect_feature(tmp_path, _reconcile_feature())
    assert ev == []


def test_reconcile_excludes_baseline_lifecycle_plus_update_status_start_stop(
    tmp_path: Path,
) -> None:
    """The exclusion covers the full six-hook baseline set, not just the
    three-hook triad."""
    _write_charm(
        tmp_path,
        """
class C:
    def __init__(self, framework):
        self.framework.observe(self.on.config_changed, self._configure)
        self.framework.observe(self.on.update_status, self._configure)
        self.framework.observe(self.on.start, self._configure)
        self.framework.observe(self.on.stop, self._configure)
    def _configure(self, event): pass
""",
    )
    ev = detect_feature(tmp_path, _reconcile_feature())
    assert ev == []


def test_reconcile_fires_when_baseline_plus_one_relation_event(tmp_path: Path) -> None:
    """One non-baseline event (a relation event here) alongside the
    baseline triad is enough to keep the binding qualifying — the
    exclusion only fires when *every* event is baseline."""
    _write_charm(
        tmp_path,
        """
class C:
    def __init__(self, framework):
        self.framework.observe(self.on.install, self._reconcile)
        self.framework.observe(self.on.config_changed, self._reconcile)
        self.framework.observe(self.on.upgrade_charm, self._reconcile)
        self.framework.observe(self.on.foo_relation_changed, self._reconcile)
    def _reconcile(self, event): pass
""",
    )
    ev = detect_feature(tmp_path, _reconcile_feature())
    assert len(ev) == 4


def test_reconcile_fires_when_baseline_plus_pebble_event(tmp_path: Path) -> None:
    """A pebble-ready event alongside baseline hooks is not excluded by
    the baseline-only cut — that's a distinct, not-yet-cut shape
    (CALIBRATION #24 follow-up, kube-state-metrics-operator)."""
    _write_charm(
        tmp_path,
        """
class C:
    def __init__(self, framework):
        self.framework.observe(self.on.workload_pebble_ready, self._manage_workload)
        self.framework.observe(self.on.config_changed, self._manage_workload)
        self.framework.observe(self.on.upgrade_charm, self._manage_workload)
    def _manage_workload(self, event): pass
""",
    )
    ev = detect_feature(tmp_path, _reconcile_feature())
    assert len(ev) == 3


# ── CALIBRATION #35 follow-up #14 shape 1: local alias of observe ───────────


def test_reconcile_fires_through_local_observe_alias(tmp_path: Path) -> None:
    """traefik-k8s-operator shape: `observe = self.framework.observe`, then
    calling the bare-Name alias — invisible to a plain `.observe` attribute
    check since the call site has no attribute access at all."""
    _write_charm(
        tmp_path,
        """
class C:
    def __init__(self, framework):
        observe = self.framework.observe
        observe(self.on.install, self._reconcile)
        observe(self.on.config_changed, self._reconcile)
        observe(self.on.leader_elected, self._reconcile)
    def _reconcile(self, event): pass
""",
    )
    ev = detect_feature(tmp_path, _reconcile_feature())
    assert len(ev) == 3


def test_reconcile_alias_scoped_to_enclosing_function(tmp_path: Path) -> None:
    """An unrelated bare-Name call sharing the alias's name in a *different*
    method must not be treated as an observe call."""
    _write_charm(
        tmp_path,
        """
class C:
    def __init__(self, framework):
        observe = self.framework.observe
        observe(self.on.install, self._reconcile)
        observe(self.on.config_changed, self._reconcile)
    def _reconcile(self, event): pass
    def other(self, observe):
        observe(self.on.upgrade_charm, self._reconcile)
""",
    )
    ev = detect_feature(tmp_path, _reconcile_feature())
    assert ev == []


def test_reconcile_does_not_alias_unrelated_bare_call(tmp_path: Path) -> None:
    """A bare-Name call to something that was never assigned from
    `<x>.framework.observe` stays unmatched."""
    _write_charm(
        tmp_path,
        """
class C:
    def __init__(self, framework):
        observe = some_other_function
        observe(self.on.install, self._reconcile)
        observe(self.on.config_changed, self._reconcile)
        observe(self.on.update_status, self._reconcile)
    def _reconcile(self, event): pass
""",
    )
    ev = detect_feature(tmp_path, _reconcile_feature())
    assert ev == []


# ── CALIBRATION #35 follow-up #14 shape 2: loop over a literal event list ───


def test_reconcile_fires_for_inline_literal_event_list_loop(tmp_path: Path) -> None:
    """`for event in [self.on.a, self.on.b, self.on.c]: observe(event, h)` —
    a hand-curated, finite event set on one shared handler, structurally
    `reconcile` even though it's wired through a loop."""
    _write_charm(
        tmp_path,
        """
class C:
    def __init__(self, framework):
        for event in [self.on.install, self.on.config_changed, self.on.leader_elected]:
            self.framework.observe(event, self._reconcile)
    def _reconcile(self, event): pass
""",
    )
    ev = detect_feature(tmp_path, _reconcile_feature())
    assert len(ev) == 3


def test_reconcile_fires_for_variable_bound_literal_event_list_loop(tmp_path: Path) -> None:
    """mediawiki-k8s-operator shape: the literal list is assigned to a named
    variable first, then looped over — one hop of resolution needed."""
    _write_charm(
        tmp_path,
        """
class C:
    def __init__(self, framework):
        reconciliation_events = [
            self.on.install, self.on.config_changed, self.on.leader_elected,
        ]
        for event in reconciliation_events:
            self.framework.observe(event, self._reconciliation)
    def _reconciliation(self, event): pass
""",
    )
    ev = detect_feature(tmp_path, _reconcile_feature())
    assert len(ev) == 3


def test_reconcile_still_ignores_reconcile_all_events_call(tmp_path: Path) -> None:
    """`self.on.events().values()` stays excluded — it's a `Call`, not a
    literal list, so it must not resolve into a fake event set. This is the
    existing `reconcile-all` idiom and must not regress."""
    _write_charm(
        tmp_path,
        """
class C:
    def __init__(self, framework):
        for ev in self.on.events().values():
            self.framework.observe(ev, self._reconcile)
    def _reconcile(self, event): pass
""",
    )
    ev = detect_feature(tmp_path, _reconcile_feature())
    assert ev == []


def test_reconcile_loop_excludes_action_events(tmp_path: Path) -> None:
    """charm-kubernetes-control-plane shape: a loop fanning several *actions*
    out to one dispatcher is action routing, not reconcile — action events
    must not count toward the threshold even inside a resolved loop."""
    _write_charm(
        tmp_path,
        """
class C:
    def __init__(self, framework):
        action_events = [
            self.on.restart_action, self.on.upgrade_action, self.on.get_kubeconfig_action,
        ]
        for action in action_events:
            self.framework.observe(action, self.charm_actions)
    def charm_actions(self, event): pass
""",
    )
    ev = detect_feature(tmp_path, _reconcile_feature())
    assert ev == []


def test_reconcile_loop_variable_scoped_to_its_own_loop(tmp_path: Path) -> None:
    """Two different loops in different methods reusing the same loop
    variable name must not have their element lists cross-contaminate."""
    _write_charm(
        tmp_path,
        """
class C:
    def __init__(self, framework):
        for event in [self.on.install, self.on.config_changed]:
            self.framework.observe(event, self._reconcile)
    def _reconcile(self, event): pass
    def other(self, framework):
        for event in [self.on.start, self.on.stop, self.on.update_status]:
            self.framework.observe(event, self._other_handler)
    def _other_handler(self, event): pass
""",
    )
    ev = detect_feature(tmp_path, _reconcile_feature())
    # _reconcile only sees 2 events (below threshold); _other_handler sees 3
    # baseline-only events (excluded by the baseline-lifecycle cut) — net zero.
    assert ev == []


def test_reconcile_loop_still_respects_relation_scoped_cut(tmp_path: Path) -> None:
    """A resolved loop binding that would otherwise qualify is still subject
    to the existing relation-scoped exclusion (CALIBRATION #21 cut #1)."""
    _write_charm(
        tmp_path,
        """
class C:
    def __init__(self, framework):
        events = [
            self.on.oauth_relation_created,
            self.on.oauth_relation_joined,
            self.on.oauth_relation_changed,
        ]
        for event in events:
            self.framework.observe(event, self._on_oauth_relation_changed)
    def _on_oauth_relation_changed(self, event): pass
""",
    )
    ev = detect_feature(tmp_path, _reconcile_feature())
    assert ev == []


def test_reconcile_takahe_operator_alias_shape_stays_delta(tmp_path: Path) -> None:
    """Must-not-regress case named explicitly in CALIBRATION #34/#35:
    takahe-operator takes `framework` as a constructor parameter and calls
    `framework.observe(...)` throughout (already visible via the plain
    `.observe` attribute check — no handler there crosses the threshold).
    Confirms the new alias/loop machinery doesn't over-fire on it."""
    _write_charm(
        tmp_path,
        """
class C:
    def __init__(self, framework: object):
        framework.observe(self.on.install, self._on_container_pebble_ready)
        framework.observe(self.on.database_created, self._on_database_changed)
        framework.observe(self.on.ingress_ready, self._on_ingress_ready)
    def _on_container_pebble_ready(self, event): pass
    def _on_database_changed(self, event): pass
    def _on_ingress_ready(self, event): pass
""",
    )
    ev = detect_feature(tmp_path, _reconcile_feature())
    assert ev == []


# ── requires-interface (db.* features) ───────────────────────────────────────


def _requires_feature(interfaces: list[str], invert: bool = False) -> Feature:
    cfg: dict = {"interfaces": interfaces}
    if invert:
        cfg["invert"] = True
    return Feature(
        name="db.test",
        library="metadata",
        summary="t",
        scope="any",
        detectors=(Detector(kind="requires-interface", config=cfg),),
    )


def _write_metadata(tmp_path: Path, body: str) -> Path:
    (tmp_path / "metadata.yaml").write_text(body)
    return tmp_path


def test_requires_interface_matches_listed_interface(tmp_path: Path) -> None:
    _write_metadata(
        tmp_path,
        "name: t\nrequires:\n  db:\n    interface: postgresql_client\n",
    )
    ev = detect_feature(tmp_path, _requires_feature(["postgresql_client", "pgsql"]))
    assert len(ev) == 1
    assert "postgresql_client" in ev[0].snippet


def test_requires_interface_ignores_provides_and_peers(tmp_path: Path) -> None:
    _write_metadata(
        tmp_path,
        "name: t\n"
        "provides:\n  db:\n    interface: postgresql_client\n"
        "peers:\n  cluster:\n    interface: postgresql_client\n",
    )
    ev = detect_feature(tmp_path, _requires_feature(["postgresql_client"]))
    assert ev == []


def test_requires_interface_invert_fires_when_none_match(tmp_path: Path) -> None:
    _write_metadata(
        tmp_path,
        "name: t\nrequires:\n  ingress:\n    interface: ingress\n",
    )
    ev = detect_feature(
        tmp_path, _requires_feature(["postgresql_client", "mysql_client"], invert=True)
    )
    assert len(ev) == 1


def test_requires_interface_invert_does_not_fire_when_some_match(tmp_path: Path) -> None:
    _write_metadata(
        tmp_path,
        "name: t\nrequires:\n  db:\n    interface: mysql_client\n",
    )
    ev = detect_feature(
        tmp_path, _requires_feature(["postgresql_client", "mysql_client"], invert=True)
    )
    assert ev == []


def test_requires_interface_invert_does_not_fire_without_metadata(tmp_path: Path) -> None:
    ev = detect_feature(tmp_path, _requires_feature(["postgresql_client"], invert=True))
    assert ev == []


# ── relation-count (requires-N / provides-N buckets) ─────────────────────────


def _count_feature(
    role: str, min_: int, max_: int | None = None, optional: bool = False
) -> Feature:
    cfg: dict = {"role": role, "min": min_}
    if max_ is not None:
        cfg["max"] = max_
    if optional:
        cfg["optional"] = True
    return Feature(
        name="count.test",
        library="metadata",
        summary="t",
        scope="any",
        detectors=(Detector(kind="relation-count", config=cfg),),
    )


def test_relation_count_fires_on_exact_bucket(tmp_path: Path) -> None:
    _write_metadata(
        tmp_path,
        "name: t\nrequires:\n  a: {interface: x}\n  b: {interface: y}\n  c: {interface: z}\n",
    )
    ev = detect_feature(tmp_path, _count_feature("requires", 3, 3))
    assert len(ev) == 1 and "requires=3" in ev[0].snippet


def test_relation_count_misses_when_outside_range(tmp_path: Path) -> None:
    _write_metadata(
        tmp_path,
        "name: t\nrequires:\n  a: {interface: x}\n",
    )
    # Bucket 2..2 shouldn't fire for 1 requires.
    assert detect_feature(tmp_path, _count_feature("requires", 2, 2)) == []


def test_relation_count_open_ended_upper(tmp_path: Path) -> None:
    _write_metadata(
        tmp_path,
        "name: t\nprovides:\n" + "".join(f"  r{i}: {{interface: i{i}}}\n" for i in range(7)),
    )
    assert detect_feature(tmp_path, _count_feature("provides", 6)) != []
    assert detect_feature(tmp_path, _count_feature("provides", 8)) == []


def test_relation_count_zero_bucket_fires_when_role_absent(tmp_path: Path) -> None:
    _write_metadata(tmp_path, "name: t\n")
    assert detect_feature(tmp_path, _count_feature("requires", 0, 0)) != []


def test_relation_count_optional_counts_only_optional_true(tmp_path: Path) -> None:
    _write_metadata(
        tmp_path,
        "name: t\nrequires:\n"
        "  a: {interface: x, optional: true}\n"
        "  b: {interface: y, optional: true}\n"
        "  c: {interface: z}\n",
    )
    # Total is 3, optional is 2.
    assert detect_feature(tmp_path, _count_feature("requires", 3, 3)) != []
    assert detect_feature(tmp_path, _count_feature("requires", 2, 2, optional=True)) != []
    assert detect_feature(tmp_path, _count_feature("requires", 3, 3, optional=True)) == []


def test_relation_count_requires_metadata_file(tmp_path: Path) -> None:
    # No metadata → detector stays silent even for the zero bucket, so we
    # don't count random directories as "0-requires charms".
    assert detect_feature(tmp_path, _count_feature("requires", 0, 0)) == []


# ── yaml-key ─────────────────────────────────────────────────────────────────


def _write_yaml_charm(tmp_path: Path, files: dict[str, str]) -> Path:
    (tmp_path / "charmcraft.yaml").write_text("type: charm\nname: t\n")
    for rel, body in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return tmp_path


def test_yaml_key_fires_on_top_level_key(tmp_path: Path) -> None:
    _write_yaml_charm(
        tmp_path,
        {
            "src/layer.yaml": "services:\n  web:\n    command: run\nchecks:\n  up:\n    level: alive\n"
        },
    )
    ev = detect_feature(tmp_path, _feature("yaml-key", files=["**/*.yaml"], key="checks"))
    assert len(ev) == 1
    assert ev[0].file == "src/layer.yaml"
    assert ev[0].detector_kind == "yaml-key"
    assert ev[0].line == 4


def test_yaml_key_fires_on_nested_key(tmp_path: Path) -> None:
    """Pebble layers are often nested under a container name."""
    _write_yaml_charm(
        tmp_path,
        {"layer.yaml": "containers:\n  web:\n    checks:\n      up:\n        level: alive\n"},
    )
    ev = detect_feature(tmp_path, _feature("yaml-key", files=["**/*.yaml"], key="checks"))
    assert len(ev) == 1


def test_yaml_key_absent_yields_no_evidence(tmp_path: Path) -> None:
    _write_yaml_charm(tmp_path, {"src/layer.yaml": "services:\n  web:\n    command: run\n"})
    assert detect_feature(tmp_path, _feature("yaml-key", files=["**/*.yaml"], key="checks")) == []


def test_yaml_key_ignores_similar_key_names(tmp_path: Path) -> None:
    """Structural matching means `health_checks` is not a `checks` match."""
    _write_yaml_charm(tmp_path, {"src/layer.yaml": "health_checks:\n  up: true\n"})
    assert detect_feature(tmp_path, _feature("yaml-key", files=["**/*.yaml"], key="checks")) == []


def test_yaml_key_ignores_the_key_inside_a_string(tmp_path: Path) -> None:
    """The regex fallback this replaces matched text anywhere; parsing doesn't."""
    _write_yaml_charm(
        tmp_path, {"src/layer.yaml": 'description: "the checks: block is not set here"\n'}
    )
    assert detect_feature(tmp_path, _feature("yaml-key", files=["**/*.yaml"], key="checks")) == []


def test_yaml_key_skips_vendored_libs_and_build_trees(tmp_path: Path) -> None:
    _write_yaml_charm(
        tmp_path,
        {
            "lib/charms/other/v0/layer.yaml": "checks:\n  up: {}\n",
            ".venv/thing/layer.yaml": "checks:\n  up: {}\n",
            "build/layer.yaml": "checks:\n  up: {}\n",
        },
    )
    assert detect_feature(tmp_path, _feature("yaml-key", files=["**/*.yaml"], key="checks")) == []


def test_yaml_key_matches_across_globs_without_duplicates(tmp_path: Path) -> None:
    _write_yaml_charm(tmp_path, {"a.yaml": "checks:\n  up: {}\n", "b.yml": "checks:\n  up: {}\n"})
    ev = detect_feature(
        tmp_path, _feature("yaml-key", files=["**/*.yaml", "**/*.yml", "**/*.yaml"], key="checks")
    )
    assert sorted(e.file for e in ev) == ["a.yaml", "b.yml"]


def test_yaml_key_handles_multi_document_yaml(tmp_path: Path) -> None:
    _write_yaml_charm(tmp_path, {"manifests.yaml": "kind: Pod\n---\nchecks:\n  up: {}\n"})
    assert (
        len(detect_feature(tmp_path, _feature("yaml-key", files=["**/*.yaml"], key="checks"))) == 1
    )


def test_yaml_key_tolerates_unparsable_yaml(tmp_path: Path) -> None:
    _write_yaml_charm(tmp_path, {"broken.yaml": "checks:\n  - [unclosed\n"})
    assert detect_feature(tmp_path, _feature("yaml-key", files=["**/*.yaml"], key="checks")) == []


def test_yaml_key_accepts_a_keys_list(tmp_path: Path) -> None:
    _write_yaml_charm(tmp_path, {"layer.yaml": "log-targets:\n  loki: {}\n"})
    ev = detect_feature(
        tmp_path, _feature("yaml-key", files=["**/*.yaml"], keys=["checks", "log-targets"])
    )
    assert len(ev) == 1


def test_yaml_key_without_a_key_is_a_no_op(tmp_path: Path) -> None:
    _write_yaml_charm(tmp_path, {"layer.yaml": "checks:\n  up: {}\n"})
    assert detect_feature(tmp_path, _feature("yaml-key", files=["**/*.yaml"])) == []


# ── charm-library provider suppression ───────────────────────────────────────


def _write_lib_charm(tmp_path: Path, *, charm_name: str, vendored: str, code: str) -> Path:
    """A charm named `charm_name` with `lib/charms/<vendored>/` on disk."""
    (tmp_path / "charmcraft.yaml").write_text(f"type: charm\nname: {charm_name}\n")
    src = tmp_path / "src"
    src.mkdir()
    (src / "charm.py").write_text(code)
    lib = tmp_path / "lib" / "charms" / vendored / "v0"
    lib.mkdir(parents=True)
    (lib / "thelib.py").write_text("LIBAPI = 0\n")
    return tmp_path


def test_import_fires_when_the_charm_merely_vendors_the_lib(tmp_path: Path) -> None:
    """`charmcraft fetch-lib` vendors a CONSUMED lib into lib/charms/<pkg>/, so
    the directory existing must not suppress the import detector."""
    _write_lib_charm(
        tmp_path,
        charm_name="postgresql",
        vendored="data_platform_libs",
        code="from charms.data_platform_libs.v0.data_interfaces import DataPeerData\n",
    )
    ev = detect_feature(tmp_path, _feature("import", module="charms.data_platform_libs"))
    assert len(ev) == 1
    assert "data_interfaces" in ev[0].snippet


def test_import_suppressed_when_the_charm_provides_the_lib(tmp_path: Path) -> None:
    """grafana-agent importing charms.grafana_agent is self-referential, not a
    consumer signal — the charm's own name identifies it as the provider."""
    _write_lib_charm(
        tmp_path,
        charm_name="grafana-agent",
        vendored="grafana_agent",
        code="from charms.grafana_agent.v0.cos_agent import COSAgentRequirer\n",
    )
    assert detect_feature(tmp_path, _feature("import", module="charms.grafana_agent")) == []


def test_import_fires_when_name_matches_but_lib_is_absent(tmp_path: Path) -> None:
    """No lib/charms/<pkg>/ tree means the charm isn't shipping the lib, whatever
    it happens to be called."""
    (tmp_path / "charmcraft.yaml").write_text("type: charm\nname: rolling-ops\n")
    src = tmp_path / "src"
    src.mkdir()
    (src / "charm.py").write_text(
        "from charms.rolling_ops.v0.rollingops import RollingOpsManager\n"
    )
    assert len(detect_feature(tmp_path, _feature("import", module="charms.rolling_ops"))) == 1


def test_import_suppression_ignores_non_charmlib_modules(tmp_path: Path) -> None:
    """The provider rule only applies to charms.* modules; a charm called `ops`
    would otherwise stop reporting its ops imports."""
    _write_lib_charm(tmp_path, charm_name="ops", vendored="ops", code="import ops\n")
    assert len(detect_feature(tmp_path, _feature("import", module="ops"))) == 1


# ── committed features.yaml patterns ─────────────────────────────────────────
#
# These features were found detecting nothing across the whole corpus because
# their patterns didn't match the shapes charms actually write. The bug was in
# the catalogue, not the engine, so the tests run the committed patterns.


def _catalogue_feature(name: str) -> Feature:
    return next(f for f in catalogue_load(default_path()) if f.name == name)


def test_log_forwarding_fires_on_a_quoted_dict_key(tmp_path: Path) -> None:
    _write_charm(
        tmp_path,
        """
def layer(self):
    return {"services": {}, "log-targets": self.pebble_log_targets}
""",
    )
    ev = detect_feature(tmp_path, _catalogue_feature("pebble.log-forwarding"))
    assert len(ev) == 1


def test_log_forwarding_fires_on_a_dict_subscript(tmp_path: Path) -> None:
    """Charms that build the layer up by assignment never write `log-targets:`."""
    _write_charm(
        tmp_path,
        """
def add_targets(self, layer):
    layer["log-targets"] = {}
""",
    )
    assert len(detect_feature(tmp_path, _catalogue_feature("pebble.log-forwarding"))) == 1


def test_log_forwarding_fires_on_a_standalone_yaml_layer(tmp_path: Path) -> None:
    _write_yaml_charm(
        tmp_path,
        {
            "src/layer.yaml": "services:\n  web:\n    command: run\nlog-targets:\n  loki:\n    type: loki\n"
        },
    )
    ev = detect_feature(tmp_path, _catalogue_feature("pebble.log-forwarding"))
    assert [e.detector_kind for e in ev] == ["yaml-key"]


def test_log_forwarding_fires_on_the_log_forwarder_lib(tmp_path: Path) -> None:
    """The idiomatic route: loki_push_api's LogForwarder injects the log
    targets into the layer, so the charm never names the key itself."""
    _write_charm(tmp_path, "from charms.loki_k8s.v1.loki_push_api import LogForwarder\n")
    assert len(detect_feature(tmp_path, _catalogue_feature("pebble.log-forwarding"))) == 1


def test_log_forwarding_absent_from_a_plain_layer(tmp_path: Path) -> None:
    _write_charm(
        tmp_path, 'LAYER = {"services": {"web": {"command": "run", "override": "replace"}}}\n'
    )
    assert detect_feature(tmp_path, _catalogue_feature("pebble.log-forwarding")) == []


def test_restart_delay_fires_on_a_quoted_backoff_key(tmp_path: Path) -> None:
    _write_charm(
        tmp_path,
        """
LAYER = {"services": {"pgbouncer": {"backoff-delay": "24h", "backoff-factor": 1}}}
""",
    )
    ev = detect_feature(tmp_path, _catalogue_feature("pebble.restart-delay"))
    assert len(ev) == 2


def test_restart_delay_fires_on_backoff_limit_in_yaml(tmp_path: Path) -> None:
    _write_yaml_charm(tmp_path, {"src/layer.yaml": "services:\n  web:\n    backoff-limit: 30s\n"})
    assert detect_feature(tmp_path, _catalogue_feature("pebble.restart-delay")) != []


def test_restart_delay_absent_from_an_untuned_layer(tmp_path: Path) -> None:
    _write_charm(
        tmp_path, 'LAYER = {"services": {"web": {"command": "run", "startup": "enabled"}}}\n'
    )
    assert detect_feature(tmp_path, _catalogue_feature("pebble.restart-delay")) == []


def test_typed_relation_fires_on_load_with_the_class_first(tmp_path: Path) -> None:
    """The API is `Relation.load(cls, src)` — data class first, then the app."""
    _write_charm(
        tmp_path,
        """
def _observer(self, event):
    data = event.relation.load(DatabaseModel, event.app)
""",
    )
    assert len(detect_feature(tmp_path, _catalogue_feature("ops.typed-relation"))) == 1


def test_typed_relation_fires_on_save(tmp_path: Path) -> None:
    _write_charm(
        tmp_path,
        """
def publish(self):
    relation = self.model.get_relation("tracing")
    relation.save(TracingRequirerData(receivers=["otlp_http"]), self.app)
""",
    )
    assert len(detect_feature(tmp_path, _catalogue_feature("ops.typed-relation"))) == 1


def test_typed_relation_ignores_a_lib_data_class_helper(tmp_path: Path) -> None:
    """The corpus's only hit for the old pattern: a charm-lib helper whose
    receiver is a data class, not a relation."""
    _write_charm(
        tmp_path,
        """
def _kratos_info(self):
    return KratosInfoData.load(self.model, KRATOS_INFO_INTEGRATION_NAME)
""",
    )
    assert detect_feature(tmp_path, _catalogue_feature("ops.typed-relation")) == []


# ── CharmSource (shared parse cache) ─────────────────────────────────────────


def test_charm_source_parses_each_file_once(tmp_path: Path, monkeypatch) -> None:
    """A charm is matched against ~70 features and patterns; each file must be
    read and parsed once for the charm, not once per feature."""
    from .. import detectors

    _write_charm(tmp_path, "import ops\n")
    (tmp_path / "src" / "other.py").write_text("import ops\n")

    parsed: list[str] = []
    real = detectors._parse_text
    monkeypatch.setattr(
        detectors, "_parse_text", lambda text, path: parsed.append(str(path)) or real(text, path)
    )

    source = detectors.CharmSource(tmp_path)
    for _ in range(10):
        detect_feature(tmp_path, _feature("import", module="ops"), source)

    assert sorted(Path(p).name for p in parsed) == ["charm.py", "other.py"]


def test_charm_source_shares_files_across_scopes(tmp_path: Path, monkeypatch) -> None:
    """The "src" and "any" scopes overlap; an overlapping file is parsed once."""
    from .. import detectors

    _write_charm(tmp_path, "import ops\n")

    parsed: list[str] = []
    real = detectors._parse_text
    monkeypatch.setattr(
        detectors, "_parse_text", lambda text, path: parsed.append(str(path)) or real(text, path)
    )

    source = detectors.CharmSource(tmp_path)
    assert source.files("src")
    assert source.files("any")
    assert len(parsed) == 1


def test_shared_source_yields_the_same_evidence(tmp_path: Path) -> None:
    from .. import detectors

    _write_charm(tmp_path, "import ops\nops.main(x)\n")
    feature = _feature("import", module="ops")
    standalone = detect_feature(tmp_path, feature)
    shared = detect_feature(tmp_path, feature, detectors.CharmSource(tmp_path))
    assert standalone == shared


def test_source_file_line_is_bounds_safe(tmp_path: Path) -> None:
    from ..detectors import SourceFile

    (tmp_path / "f.py").write_text("a = 1\nb = 2\n")
    src = SourceFile(tmp_path / "f.py", tmp_path)
    assert src.line(1) == "a = 1"
    assert src.line(2) == "b = 2"
    assert src.line(0) == ""
    assert src.line(99) == ""
