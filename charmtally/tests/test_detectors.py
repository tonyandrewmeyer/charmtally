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
