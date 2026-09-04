"""`charmtally scan` must survive a charm that blows up, and parallelise.

The scan loop used to write `results.json` only after walking all 755 charms
with nothing guarding the loop body, so one unhandled exception discarded the
entire run's work. It now isolates failures per charm and fans the work out
over `--jobs`; what these tests pin down is that neither of those changes what
a successful scan produces.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from .. import scan
from ..cli import main

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_CHARM = """\
import ops


class MyCharm(ops.CharmBase):
    def __init__(self, framework):
        super().__init__(framework)
        framework.observe(self.on.start, self._on_start)

    def _on_start(self, event):
        self.unit.status = ops.ActiveStatus()
"""


def _make_charm(root: Path, name: str) -> Path:
    d = root / name
    (d / "src").mkdir(parents=True)
    (d / "charmcraft.yaml").write_text(f"type: charm\nname: {name}\n")
    (d / "src" / "charm.py").write_text(_CHARM)
    return d


def _corpus(path: Path, names: list[str]) -> Path:
    rows = ["Team,Charm Name,Repository,Key Charm for this Team,Branch (if not the default),Notes"]
    rows += [f"charm-tech,{n},https://github.com/example/{n},FALSE,," for n in names]
    path.write_text("\n".join(rows) + "\n")
    return path


def _fake_clones(
    monkeypatch: pytest.MonkeyPatch,
    checkouts: dict[str, Path],
    *,
    stale: set[str] = frozenset(),
) -> None:
    """Point `ensure_clone` at local directories instead of cloning anything.

    The clone phase runs in the parent process, so patching it here still
    holds when the scan phase fans out over a process pool.
    """

    def fake(ref, workdir):
        path = checkouts.get(ref.slug)
        if path is None:
            return scan.CloneResult(None)
        return scan.CloneResult(path, stale=ref.slug in stale)

    monkeypatch.setattr(scan, "ensure_clone", fake)


def _run(tmp_path: Path, names: list[str], jobs: int) -> dict:
    out = tmp_path / f"results-{jobs}.json"
    assert (
        main([
            "scan",
            "--corpus",
            str(_corpus(tmp_path / "corpus.csv", names)),
            "--workdir",
            str(tmp_path / "work"),
            "--jobs",
            str(jobs),
            "--out",
            str(out),
        ])
        == 0
    )
    return json.loads(out.read_text())


class TestFailureIsolation:
    def test_one_charm_that_raises_does_not_lose_the_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        checkouts = {n: _make_charm(tmp_path / "repos", n) for n in ("alpha", "boom", "gamma")}
        _fake_clones(monkeypatch, checkouts)

        real = scan.scan_charm

        def explode(charm_root, features, patterns=None):
            if charm_root.name == "boom":
                raise RuntimeError("pathological charm")
            return real(charm_root, features, patterns)

        monkeypatch.setattr(scan, "scan_charm", explode)

        results = _run(tmp_path, ["alpha", "boom", "gamma"], jobs=1)

        assert set(results) == {"alpha", "gamma", "__skipped__"}
        assert "pathological charm" in results["__skipped__"]["boom"]

    def test_a_failed_clone_is_skipped_with_a_reason(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        checkouts = {"alpha": _make_charm(tmp_path / "repos", "alpha")}
        _fake_clones(monkeypatch, checkouts)

        results = _run(tmp_path, ["alpha", "gone"], jobs=1)

        assert set(results) == {"alpha", "__skipped__"}
        assert results["__skipped__"] == {"gone": "clone failed"}


class TestStale:
    def test_a_stale_checkout_is_marked_in_meta(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A charm whose refresh failed is still scanned, but says so."""
        checkouts = {n: _make_charm(tmp_path / "repos", n) for n in ("alpha", "beta")}
        _fake_clones(monkeypatch, checkouts, stale={"beta"})

        results = _run(tmp_path, ["alpha", "beta"], jobs=1)

        assert "stale" not in results["alpha"]["features"]["__meta__"]
        assert results["beta"]["features"]["__meta__"]["stale"] is True


class TestJobs:
    def test_parallel_and_serial_agree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`--jobs N` may only change how long the scan takes.

        Including the order of the records: they are assembled in corpus
        order, not in whatever order the pool happens to finish them.
        """
        names = ["alpha", "beta", "gamma", "delta"]
        checkouts = {n: _make_charm(tmp_path / "repos", n) for n in names}
        _fake_clones(monkeypatch, checkouts)

        serial = _run(tmp_path, names, jobs=1)
        parallel = _run(tmp_path, names, jobs=3)

        assert list(parallel) == list(serial) == names
        assert parallel == serial
