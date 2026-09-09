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


def _charm(
    name: str,
    *,
    present_features: set[str],
    all_features: list[str],
    team: str = "team-a",
    gaps: set[str] = frozenset(),  # type: ignore[assignment]
    meta: dict | None = None,
) -> dict:
    feats: dict[str, dict] = {}
    for fname in all_features:
        if fname in present_features:
            score = "present"
        elif fname in gaps:
            score = "clear-gap"
        else:
            score = "not-applicable"
        feats[fname] = {
            "present": fname in present_features,
            "evidence": [],
            "score": score,
            "rationale": "",
        }
    feats["__meta__"] = {**_empty_meta(), **(meta or {})}
    return {"name": name, "team": team, "repo_url": f"https://x/{name}", "features": feats}


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
        _charm(
            f"c{i}",
            present_features=({"rare-thing"} if i < 2 else set()),
            all_features=["rare-thing"],
        )
        for i in range(10)
    ]
    html = render({c["name"]: c for c in charms}, feats)
    assert 'class="low-count"' in html


def test_low_count_marker_absent_when_at_or_above_floor() -> None:
    """At the floor (5 hits) the marker should not appear."""
    feats = [_feature("common-thing")]
    charms = [
        _charm(
            f"c{i}",
            present_features=({"common-thing"} if i < 5 else set()),
            all_features=["common-thing"],
        )
        for i in range(10)
    ]
    html = render({c["name"]: c for c in charms}, feats)
    assert 'class="low-count"' not in html


def test_expected_rare_suppresses_low_count_marker() -> None:
    """A feature with `expected_rare: true` doesn't get the marker even at 0 hits."""
    feats = [_feature("genuinely-rare", expected_rare=True)]
    charms = [
        _charm(f"c{i}", present_features=set(), all_features=["genuinely-rare"]) for i in range(10)
    ]
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


# ── filter bars ──────────────────────────────────────────────────────────────
#
# The bars are driven entirely by markup the JS reads back — `data-filter-bar`
# on the bar, facet-derived buttons/options, and one data- attribute per
# filterable axis on each row. These assert the contract between the two
# halves; the behaviour on top of it is plain DOM code.


def test_charm_rows_carry_every_filterable_axis() -> None:
    feats = [_feature("f1"), _feature("f2")]
    charm = _charm(
        "sub-charm",
        present_features={"f1"},
        gaps={"f2"},
        all_features=["f1", "f2"],
        team="Data Platform",
        meta={
            "is_subordinate": True,
            "provides_own_library": True,
            "has_containers": True,
            "charmcraft_plugins": ["charm"],
            "bases": ["24.04"],
            "tooling": ["tox"],
        },
    )
    html = render({"sub-charm": charm}, feats)

    assert 'data-flags="lib-provider subordinate"' in html
    assert 'data-team="Data Platform"' in html
    assert 'data-tooling="tox"' in html
    assert 'data-features="f1"' in html
    assert 'data-gaps="f2"' in html
    assert 'data-shape="k8s"' in html
    assert 'data-status="has-gaps"' in html
    # `stack:` search matches plugin / base / tooling text, not just the name.
    assert 'data-stack="k8s charm 24.04 tox"' in html


def test_filter_facets_are_populated_from_the_corpus() -> None:
    feats = [_feature("f1")]
    charms = [
        _charm(
            "a",
            present_features={"f1"},
            all_features=["f1"],
            team="Observability",
            meta={"tooling": ["tox"]},
        ),
        _charm(
            "b", present_features=set(), all_features=["f1"], team="", meta={"tooling": ["just"]}
        ),
    ]
    html = render({c["name"]: c for c in charms}, feats)

    assert '<option value="Observability">' in html
    assert '<option value="(no team)">' in html  # charms with no owner stay filterable
    assert '<option value="tox">' in html
    assert '<option value="just">' in html
    # The architecture taxonomy is fixed, not corpus-derived: a bucket with no
    # charms this week still gets a button so the bar doesn't shift about.
    assert '<button data-value="reconcile-all"' in html
    assert '<button data-value="component-graph"' in html


def test_feature_and_team_rows_carry_filter_attributes() -> None:
    feats = [_feature("used"), _feature("unused")]
    charms = [
        _charm("a", present_features={"used"}, gaps={"unused"}, all_features=["used", "unused"]),
    ]
    html = render({c["name"]: c for c in charms}, feats)

    assert 'data-filter-bar="feature-table"' in html
    assert 'data-filter-bar="charm-table"' in html
    assert 'data-filter-bar="team-table"' in html
    # `unused` is held by nobody and is a clear gap for charm a.
    assert 'data-only="gaps unused' in html


def test_summary_and_rollups_link_into_the_filtered_charm_view() -> None:
    feats = [_feature("f1")]
    charms = [_charm("a", present_features={"f1"}, all_features=["f1"], team="Data Platform")]
    html = render({c["name"]: c for c in charms}, feats)

    assert 'href="?charm.shape=k8s#charm-view"' in html
    assert 'href="?charm.feat=has:f1#charm-view"' in html
    assert 'href="?charm.feat=gap:f1#charm-view"' in html
    assert 'href="?charm.team=Data%20Platform#charm-view"' in html


# ── autoescaping ─────────────────────────────────────────────────────────────


def test_charm_supplied_strings_are_escaped() -> None:
    """Charm names, repo URLs and rationales come from third-party
    repositories and the hyrum CSV, and the rendered page is published to
    GitHub Pages. select_autoescape(["html"]) tested the `j2` extension of
    `dashboard.html.j2` and returned False, so none of this was escaped.
    """
    feats = [_feature("ops.collect-status")]
    charm = _charm("evil", present_features=set(), all_features=["ops.collect-status"])
    charm["name"] = "<script>alert(1)</script>"
    charm["repo_url"] = 'https://x/y" onmouseover="alert(1)'
    charm["features"]["ops.collect-status"]["score"] = "clear-gap"
    charm["features"]["ops.collect-status"]["rationale"] = "<img src=x onerror=alert(1)>"

    html = render({"evil": charm}, feats)

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<img src=x onerror=alert(1)>" not in html
    assert 'onmouseover="alert(1)' not in html


def test_filter_data_attributes_are_escaped() -> None:
    """Team names reach the page as data- attribute values and as <option>
    values in the team picker; both come from the hyrum CSV."""
    feats = [_feature("f1")]
    charm = _charm(
        "c1", present_features={"f1"}, all_features=["f1"], team='x" onmouseover="alert(1)'
    )
    html = render({"c1": charm}, feats)

    assert 'onmouseover="alert(1)' not in html
    assert "&#34; onmouseover=&#34;alert(1)" in html


# ── evidence permalinks ──────────────────────────────────────────────────────


def _charm_with_evidence(
    fname: str,
    *,
    file: str = "src/charm.py",
    line: int = 12,
    repo_sha: str | None = "a" * 40,
    subpath: str | None = None,
) -> dict:
    charm = _charm("c1", present_features={fname}, all_features=[fname])
    charm["repo_url"] = "https://github.com/canonical/foo-operator"
    charm["features"][fname]["evidence"] = [
        {"file": file, "line": line, "detector_kind": "import", "snippet": "x"}
    ]
    charm["features"]["__meta__"]["repo_sha"] = repo_sha
    if subpath is not None:
        charm["subpath"] = subpath
    return charm


def test_evidence_link_is_a_permalink_at_the_scanned_commit() -> None:
    """Links pointed at `main`, so a line number drifted out from under the
    evidence as upstream moved. The scan records the commit it ran at."""
    charm = _charm_with_evidence("f1", repo_sha="deadbeef")
    html = render({"c1": charm}, [_feature("f1")])

    assert "https://github.com/canonical/foo-operator/blob/deadbeef/src/charm.py#L12" in html
    assert "/blob/main/" not in html


def test_evidence_link_of_a_sub_charm_includes_the_subpath() -> None:
    """Evidence paths are relative to the charm root; monorepo records need
    the sub-charm directory prepended or the link 404s."""
    charm = _charm_with_evidence("f1", repo_sha="cafe", subpath="charms/bar")
    html = render({"c1": charm}, [_feature("f1")])

    assert "/blob/cafe/charms/bar/src/charm.py#L12" in html


def test_evidence_link_falls_back_to_the_ref_without_a_repo_sha() -> None:
    """`charmtally local` scans a directory that need not be a git checkout,
    so repo_sha is None there."""
    charm = _charm_with_evidence("f1", repo_sha=None)
    html = render({"c1": charm}, [_feature("f1")])

    assert "/blob/main/src/charm.py#L12" in html


def test_evidence_link_drops_the_anchor_for_an_unlocated_line() -> None:
    """The file-independent detectors report line 0 — a structural match with
    no located line. `#L0` scrolled to nothing."""
    charm = _charm_with_evidence("f1", file="charmcraft.yaml", line=0, repo_sha="cafe")
    html = render({"c1": charm}, [_feature("f1")])

    assert "/blob/cafe/charmcraft.yaml" in html
    assert "#L0" not in html


def test_ops_requirement_shown_in_the_stack_column() -> None:
    feats = [_feature("thing")]
    charm = _charm(
        "c0",
        present_features=set(),
        all_features=["thing"],
        meta={"ops_requirement": ">=2.15"},
    )
    html = render({charm["name"]: charm}, feats)
    assert "ops&nbsp;&gt;=2.15" in html


def test_unpinned_ops_is_shown_rather_than_omitted() -> None:
    """An empty specifier means "any ops", which is not the same as unknown."""
    feats = [_feature("thing")]
    charm = _charm(
        "c0", present_features=set(), all_features=["thing"], meta={"ops_requirement": ""}
    )
    html = render({charm["name"]: charm}, feats)
    assert "ops&nbsp;*" in html

    charm = _charm("c1", present_features=set(), all_features=["thing"])
    assert "ops&nbsp;" not in render({charm["name"]: charm}, feats)
