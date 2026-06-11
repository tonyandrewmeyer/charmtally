"""Tests for the architecture-pattern loader added with the architecture axis."""

from __future__ import annotations

from pathlib import Path

from ..catalogue import load, load_patterns


def test_load_features_still_works():
    """The original load() function is unchanged by the architecture refactor."""
    feats = load(Path(__file__).resolve().parent.parent.parent / "features.yaml")
    assert len(feats) > 0
    assert all(f.name and f.detectors for f in feats)


def test_load_patterns_returns_patterns():
    """The committed features.yaml defines an architecture section."""
    pats = load_patterns(Path(__file__).resolve().parent.parent.parent / "features.yaml")
    names = {p.name for p in pats}
    assert "reconcile-all" in names
    assert "reconcile" in names
    assert "component-graph" in names


def test_load_patterns_missing_section_returns_empty(tmp_path: Path) -> None:
    p = tmp_path / "no-arch.yaml"
    p.write_text(
        "version: 1\n"
        "features:\n"
        "  - name: ops.foo\n"
        "    library: ops\n"
        "    summary: x\n"
        "    scope: src\n"
        "    detect:\n"
        "      - kind: regex\n"
        "        pattern: 'foo'\n"
    )
    assert load_patterns(p) == []


def test_load_patterns_parses_detectors(tmp_path: Path) -> None:
    p = tmp_path / "with-arch.yaml"
    p.write_text(
        "version: 1\n"
        "features: []\n"
        "architecture:\n"
        "  - name: pattern-x\n"
        "    summary: a test pattern\n"
        "    scope: src\n"
        "    detect:\n"
        "      - kind: regex\n"
        "        pattern: 'xyzzy'\n"
        "      - kind: import\n"
        "        module: foo.bar\n"
    )
    pats = load_patterns(p)
    assert len(pats) == 1
    assert pats[0].name == "pattern-x"
    assert len(pats[0].detectors) == 2
    assert pats[0].detectors[0].kind == "regex"
    assert pats[0].detectors[0].config["pattern"] == "xyzzy"
    assert pats[0].detectors[1].kind == "import"
    assert pats[0].detectors[1].config["module"] == "foo.bar"
