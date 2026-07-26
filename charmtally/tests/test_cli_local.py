"""`charmtally local` must score a charm the same way `charmtally scan` does.

Both commands share scan_charm, but only `scan` used to pass the architecture
patterns, so the architecture short-circuits in scoring.py never fired for a
local run and a charm could be reported as a clear-gap locally and
not-applicable in the real corpus scan.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

from ..catalogue import load, load_patterns
from ..cli import DEFAULT_CATALOGUE, main
from ..scan import scan_charm

# A reconcile-shaped charm: one handler bound to five distinct non-baseline
# events. scoring.py suppresses the ops.collect-status gap for these.
_RECONCILE_CHARM = """\
import ops


class MyCharm(ops.CharmBase):
    def __init__(self, framework):
        super().__init__(framework)
        self.framework.observe(self.on.db_relation_changed, self._reconcile)
        self.framework.observe(self.on.ingress_relation_broken, self._reconcile)
        self.framework.observe(self.on.leader_elected, self._reconcile)
        self.framework.observe(self.on.secret_changed, self._reconcile)
        self.framework.observe(self.on.certificates_available, self._reconcile)

    def _reconcile(self, _):
        self.unit.status = ops.ActiveStatus()
"""


def _write_charm(root: Path) -> Path:
    src = root / "src"
    src.mkdir(parents=True)
    (src / "charm.py").write_text(_RECONCILE_CHARM)
    (root / "charmcraft.yaml").write_text("type: charm\nname: demo\nprovides:\n  db:\n    interface: pgsql\n")
    return root


def test_local_matches_scan_scoring(tmp_path: Path) -> None:
    charm = _write_charm(tmp_path / "demo")
    feats = load(DEFAULT_CATALOGUE)
    pats = load_patterns(DEFAULT_CATALOGUE)

    buf = io.StringIO()
    with redirect_stdout(buf):
        assert main(["local", str(charm)]) == 0
    from_cli = json.loads(buf.getvalue())["demo"]

    from_scan = scan_charm(charm, feats, pats)
    assert from_cli["__meta__"]["architecture"] == from_scan["__meta__"]["architecture"]
    for name, rec in from_scan.items():
        if name.startswith("__"):
            continue
        assert from_cli[name].get("score") == rec.get("score"), name


def test_local_detects_architecture(tmp_path: Path) -> None:
    """Guards the fix itself: without patterns the architecture list is empty
    and the collect-status gap rule fires when it shouldn't."""
    charm = _write_charm(tmp_path / "demo")

    buf = io.StringIO()
    with redirect_stdout(buf):
        assert main(["local", str(charm)]) == 0
    result = json.loads(buf.getvalue())["demo"]

    assert "reconcile" in result["__meta__"]["architecture"]
    assert result["ops.collect-status"]["score"] == "not-applicable"
