"""Tests for the architecture-pattern loader added with the architecture axis."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..catalogue import default_path, load, load_patterns

if TYPE_CHECKING:
    from pathlib import Path


def test_default_path_resolves_inside_the_package():
    """The catalogue ships as package data, so it must resolve without a checkout.

    Resolving it relative to the repo root worked from a source tree and left
    every installed environment with a CLI that died on a missing file — the
    wheel contains no repo root. Keep this asserting on the package directory,
    not just on existence.
    """
    path = default_path()
    assert path.is_file(), f"{path} is missing — is features.yaml still inside charmtally/?"
    assert path.parent.name == "charmtally"


def test_load_features_still_works():
    """The original load() function is unchanged by the architecture refactor."""
    feats = load(default_path())
    assert len(feats) > 0
    assert all(f.name and f.detectors for f in feats)


def test_load_patterns_returns_patterns():
    """The committed features.yaml defines an architecture section."""
    pats = load_patterns(default_path())
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


def test_expected_rare_defaults_to_false(tmp_path: Path) -> None:
    p = tmp_path / "f.yaml"
    p.write_text(
        "version: 1\n"
        "features:\n"
        "  - name: f1\n"
        "    library: ops\n"
        "    summary: s\n"
        "    scope: src\n"
        "    detect:\n"
        "      - kind: regex\n"
        "        pattern: 'x'\n"
    )
    feats = load(p)
    assert feats[0].expected_rare is False


def test_expected_rare_parsed_when_true(tmp_path: Path) -> None:
    p = tmp_path / "f.yaml"
    p.write_text(
        "version: 1\n"
        "features:\n"
        "  - name: f1\n"
        "    library: ops\n"
        "    summary: s\n"
        "    scope: src\n"
        "    expected_rare: true\n"
        "    detect:\n"
        "      - kind: regex\n"
        "        pattern: 'x'\n"
    )
    feats = load(p)
    assert feats[0].expected_rare is True
