"""`charmtally scan`'s two-phase loop: failure isolation and --jobs.

Cloning is stubbed — the git interaction has its own tests in
test_scan_clone.py — but the scan half runs for real, including through a
process pool, because "the parallel run agrees with the serial one" is the
whole claim.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from .. import cli, scan
from ..cli import main

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_CHARM = "import ops\n\n\nclass C(ops.CharmBase):\n    pass\n"


def _make_charm(root: Path, name: str) -> Path:
    d = root / name
    (d / "src").mkdir(parents=True)
    (d / "charmcraft.yaml").write_text(f"type: charm\nname: {name}\n")
    (d / "src" / "charm.py").write_text(_CHARM)
    return d


def _corpus(path: Path, names: list[str]) -> Path:
    rows = ["Team,Charm Name,Repository,Key Charm for this Team,Branch (if not the default),Notes"]
    rows += [f"charm-tech,{n},https://example.com/canonical/{n},FALSE,," for n in names]
    path.write_text("\n".join(rows) + "\n")
    return path


def _stub_clones(monkeypatch: pytest.MonkeyPatch, clones: dict[str, scan.Clone | None]) -> None:
    """Map each ref's charm name onto a prepared checkout (or a failure)."""

    def fake(ref, workdir):
        return clones.get(ref.name)

    monkeypatch.setattr(scan, "ensure_clone_status", fake)


def _run(tmp_path: Path, names: list[str], *extra: str) -> dict:
    out = tmp_path / "results.json"
    assert (
        main([
            "scan",
            "--corpus",
            str(_corpus(tmp_path / "corpus.csv", names)),
            "--workdir",
            str(tmp_path / "work"),
            "--out",
            str(out),
            *extra,
        ])
        == 0
    )
    return json.loads(out.read_text())


class TestFailureIsolation:
    def test_one_charms_exception_costs_only_that_charm(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing is written until every charm has been scanned, so an
        unguarded raise here used to discard the whole run's work."""
        good, bad = _make_charm(tmp_path, "good"), _make_charm(tmp_path, "bad")
        _stub_clones(monkeypatch, {"good": scan.Clone(good), "bad": scan.Clone(bad)})

        real = scan.scan_charm

        def explode(charm_root, features, patterns=None, *, stale=False):
            if charm_root == bad:
                raise RecursionError("boom")
            return real(charm_root, features, patterns, stale=stale)

        monkeypatch.setattr(scan, "scan_charm", explode)

        results = _run(tmp_path, ["good", "bad"], "--jobs", "1")

        assert "good" in results
        assert "bad" not in results
        assert "boom" in results["__skipped__"]["bad"]
        assert "RecursionError" in results["__skipped__"]["bad"]

    def test_clone_failure_is_still_recorded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_clones(monkeypatch, {"gone": None})
        results = _run(tmp_path, ["gone"], "--jobs", "1")
        assert results["__skipped__"] == {"gone": "clone failed"}


class TestJobs:
    def test_parallel_scan_matches_the_serial_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        names = ["a", "b", "c"]
        clones = {n: scan.Clone(_make_charm(tmp_path, n)) for n in names}
        _stub_clones(monkeypatch, clones)

        serial = _run(tmp_path, names, "--jobs", "1")
        parallel = _run(tmp_path, names, "--jobs", "3")

        assert list(parallel) == list(serial)  # order too: records are keyed in corpus order
        assert parallel == serial


class TestStale:
    def test_a_charm_scanned_from_an_unrefreshed_clone_is_marked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A charm whose refresh failed is scanned at last week's commit; the
        row has to say so rather than read as a fresh one."""
        _stub_clones(
            monkeypatch,
            {
                "fresh": scan.Clone(_make_charm(tmp_path, "fresh")),
                "old": scan.Clone(_make_charm(tmp_path, "old"), stale=True),
            },
        )
        results = _run(tmp_path, ["fresh", "old"], "--jobs", "1")

        assert results["old"]["features"]["__meta__"]["stale"] is True
        assert results["fresh"]["features"]["__meta__"]["stale"] is False


def test_ordered_map_preserves_input_order() -> None:
    """The stderr progress log and the record order both depend on this."""
    from concurrent.futures import ThreadPoolExecutor

    items = list(range(20))
    assert list(cli._ordered_map(str, items, 1, ThreadPoolExecutor)) == [str(i) for i in items]
    assert list(cli._ordered_map(str, items, 4, ThreadPoolExecutor)) == [str(i) for i in items]
