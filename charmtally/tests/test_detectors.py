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


# ── testing.caplog regex (step-12 Nhan-1) ────────────────────────────────────


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
