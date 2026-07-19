"""Tests for the AST detector kinds added for the holistic-family
architecture patterns (part-reconcile / unconditional-init)."""

from __future__ import annotations

from pathlib import Path

from ..catalogue import Detector, Feature
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
    ev = detect_feature(tmp_path, _feature("ast-shared-method", attrs=["_reconcile"], min_callers=2))
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
    ev = detect_feature(tmp_path, _feature("ast-shared-method", attrs=["_reconcile"], min_callers=2))
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
    ev = detect_feature(tmp_path, _feature("ast-shared-method", attrs=["_reconcile"], min_callers=2))
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
        _feature("ast-shared-method", attrs=["_reconcile"], min_callers=2, handler_re=r"^_handle_"),
    )
    assert len(ev) == 2


# testing.caplog regex


def _caplog_feature() -> Feature:
    return Feature(
        name="testing.caplog",
        library="python",
        summary="test",
        scope="tests",
        detectors=(Detector(kind="regex", config={"pattern": r"def\s+test_\w*\s*\([^)]*\bcaplog\b"}),),
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
        self.framework.observe(self.on.start, self._reconcile)
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
    def _handle(self, event): pass
""",
    )
    ev = detect_feature(tmp_path, _reconcile_feature())
    assert len(ev) == 3


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
    ev = detect_feature(tmp_path, _requires_feature(["postgresql_client", "mysql_client"], invert=True))
    assert len(ev) == 1


def test_requires_interface_invert_does_not_fire_when_some_match(tmp_path: Path) -> None:
    _write_metadata(
        tmp_path,
        "name: t\nrequires:\n  db:\n    interface: mysql_client\n",
    )
    ev = detect_feature(tmp_path, _requires_feature(["postgresql_client", "mysql_client"], invert=True))
    assert ev == []


def test_requires_interface_invert_does_not_fire_without_metadata(tmp_path: Path) -> None:
    ev = detect_feature(tmp_path, _requires_feature(["postgresql_client"], invert=True))
    assert ev == []


# ── relation-count (requires-N / provides-N buckets) ─────────────────────────


def _count_feature(role: str, min_: int, max_: int | None = None, optional: bool = False) -> Feature:
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
