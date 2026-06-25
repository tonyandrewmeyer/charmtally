"""Tests for the dashboard rendering — focused on the low-count
precision-floor annotation added with the scan-detector follow-ups."""

from __future__ import annotations

from ..catalogue import Detector, Feature
from ..dashboard import render


def _empty_meta() -> dict:
    return {
        "has_containers": False,
        "is_reactive": False,
        "is_legacy_classic": False,
        "is_subordinate": False,
        "is_workload_less": False,
        "architecture": [],
        "charm_name": None,
        "charmcraft_plugins": [],
        "bases": [],
        "min_juju_version": None,
        "library_count": 0,
        "provides_own_library": False,
        "has_terraform_module": False,
        "tooling": [],
    }


def _charm(name: str, *, present_features: set[str], all_features: list[str]) -> dict:
    feats: dict[str, dict] = {}
    for fname in all_features:
        feats[fname] = {
            "present": fname in present_features,
            "evidence": [],
            "score": "present" if fname in present_features else "not-applicable",
            "rationale": "",
        }
    feats["__meta__"] = _empty_meta()
    return {"name": name, "team": "team-a", "repo_url": f"https://x/{name}", "features": feats}


def _feature(name: str, *, expected_rare: bool = False) -> Feature:
    return Feature(
        name=name,
        library="ops",
        summary="s",
        scope="src",
        detectors=(Detector(kind="regex", config={"pattern": "x"}),),
        expected_rare=expected_rare,
    )


def test_low_count_marker_appears_when_present_below_floor() -> None:
    """A feature held by < 5 charms gets the ⚠ marker in the feature view."""
    feats = [_feature("rare-thing")]
    charms = [
        _charm(f"c{i}", present_features=({"rare-thing"} if i < 2 else set()), all_features=["rare-thing"])
        for i in range(10)
    ]
    html = render({c["name"]: c for c in charms}, feats)
    assert 'class="low-count"' in html


def test_low_count_marker_absent_when_at_or_above_floor() -> None:
    """At the floor (5 hits) the marker should not appear."""
    feats = [_feature("common-thing")]
    charms = [
        _charm(f"c{i}", present_features=({"common-thing"} if i < 5 else set()), all_features=["common-thing"])
        for i in range(10)
    ]
    html = render({c["name"]: c for c in charms}, feats)
    assert 'class="low-count"' not in html


def test_expected_rare_suppresses_low_count_marker() -> None:
    """A feature with `expected_rare: true` doesn't get the marker even at 0 hits."""
    feats = [_feature("genuinely-rare", expected_rare=True)]
    charms = [_charm(f"c{i}", present_features=set(), all_features=["genuinely-rare"]) for i in range(10)]
    html = render({c["name"]: c for c in charms}, feats)
    assert 'class="low-count"' not in html


# ── Pairs view (k8s/machine pair detection) ──────────────────────────────────


def test_pairs_view_absent_when_no_pairs_passed() -> None:
    """Without pairs= the Pairs section and nav link are not in the page."""
    feats = [_feature("f1")]
    charms = [_charm("c1", present_features={"f1"}, all_features=["f1"])]
    html = render({c["name"]: c for c in charms}, feats)
    assert 'id="pairs-view"' not in html
    assert "Pairs</a>" not in html


def test_pairs_view_renders_when_pairs_passed() -> None:
    feats = [_feature("f1")]
    charms = [_charm("c1", present_features={"f1"}, all_features=["f1"])]
    pairs = [
        {
            "root": "postgresql",
            "k8s_name": "postgresql-k8s",
            "machine_name": "postgresql",
            "k8s_repo_url": "https://x/p-k8s",
            "machine_repo_url": "https://x/p",
            "confidence": "high",
            "same_repo": False,
            "shares_charmlib": True,
        },
    ]
    html = render({c["name"]: c for c in charms}, feats, pairs=pairs)
    assert 'id="pairs-view"' in html
    assert "postgresql-k8s" in html
    assert "shared lib" in html
