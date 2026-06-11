"""Tests for monorepo fan-out and corpus overrides added for step 10."""
from __future__ import annotations

from pathlib import Path

import pytest

from ..corpus import CharmRef, CorpusOverrides, load_overrides
from ..scan import find_charm_roots


def _make_charm(d: Path, *, charmcraft: bool = True, metadata: bool = False) -> None:
    d.mkdir(parents=True, exist_ok=True)
    if charmcraft:
        (d / "charmcraft.yaml").write_text("type: charm\nname: x\n")
    if metadata:
        (d / "metadata.yaml").write_text("name: x\n")


class TestFindCharmRoots:
    def test_no_charm_files_anywhere(self, tmp_path: Path) -> None:
        (tmp_path / "README.md").write_text("just docs")
        assert find_charm_roots(tmp_path) == []

    def test_single_charm_at_root_charmcraft(self, tmp_path: Path) -> None:
        _make_charm(tmp_path)
        assert find_charm_roots(tmp_path) == [tmp_path]

    def test_single_charm_at_root_metadata_only(self, tmp_path: Path) -> None:
        _make_charm(tmp_path, charmcraft=False, metadata=True)
        assert find_charm_roots(tmp_path) == [tmp_path]

    def test_monorepo_two_sub_charms(self, tmp_path: Path) -> None:
        _make_charm(tmp_path / "charms" / "a")
        _make_charm(tmp_path / "charms" / "b")
        roots = sorted(find_charm_roots(tmp_path))
        assert roots == [tmp_path / "charms" / "a", tmp_path / "charms" / "b"]

    def test_monorepo_nested_layers(self, tmp_path: Path) -> None:
        """Deeply nested charm layouts (e.g. bigtop's layer-* tree) fan out correctly."""
        _make_charm(tmp_path / "pkg" / "src" / "charm" / "giraph" / "layer-giraph",
                    charmcraft=False, metadata=True)
        _make_charm(tmp_path / "pkg" / "src" / "charm" / "hadoop" / "layer-hadoop-plugin",
                    charmcraft=False, metadata=True)
        roots = sorted(find_charm_roots(tmp_path))
        assert len(roots) == 2

    def test_skips_vendored_libs(self, tmp_path: Path) -> None:
        """A vendored charm lib's charmcraft.yaml should not count as a sub-charm."""
        _make_charm(tmp_path / "charms" / "real-charm")
        # vendored lib lives under a 'lib' directory — must not be picked up.
        _make_charm(tmp_path / "charms" / "real-charm" / "lib" / "charms" / "tempo" / "v0")
        roots = find_charm_roots(tmp_path)
        assert roots == [tmp_path / "charms" / "real-charm"]

    def test_skips_tests_directory(self, tmp_path: Path) -> None:
        _make_charm(tmp_path / "charms" / "real")
        _make_charm(tmp_path / "tests" / "fixtures" / "fake-charm",
                    charmcraft=False, metadata=True)
        roots = find_charm_roots(tmp_path)
        assert roots == [tmp_path / "charms" / "real"]

    def test_root_charm_wins_over_nested(self, tmp_path: Path) -> None:
        """If the root has a charm file, treat it as a single-charm repo."""
        _make_charm(tmp_path)
        _make_charm(tmp_path / "subdir")  # shouldn't matter; root wins
        assert find_charm_roots(tmp_path) == [tmp_path]

    def test_root_bundle_descends_to_sub_charms(self, tmp_path: Path) -> None:
        """A root charmcraft.yaml with type: bundle is not a charm — descend.

        Real-world: argo-operators, kfp-operators, istio-operators all ship
        a top-level bundle charmcraft.yaml alongside real charms in
        charms/<sub>/. Without descending we'd miss every sub-charm.
        """
        (tmp_path / "charmcraft.yaml").write_text("type: bundle\nname: my-bundle\n")
        _make_charm(tmp_path / "charms" / "controller")
        _make_charm(tmp_path / "charms" / "ui")
        roots = sorted(find_charm_roots(tmp_path))
        assert roots == [
            tmp_path / "charms" / "controller",
            tmp_path / "charms" / "ui",
        ]

    def test_root_bundle_with_no_subcharms_returns_empty(self, tmp_path: Path) -> None:
        """A bundle repo with no sub-charms isn't a scannable charm at all."""
        (tmp_path / "charmcraft.yaml").write_text("type: bundle\nname: empty\n")
        assert find_charm_roots(tmp_path) == []

    def test_root_bundle_alongside_root_metadata_still_charm(self, tmp_path: Path) -> None:
        """Unusual but valid: type: bundle in charmcraft.yaml + a real
        metadata.yaml at root. Treat as a single-charm repo at the root."""
        (tmp_path / "charmcraft.yaml").write_text("type: bundle\nname: x\n")
        (tmp_path / "metadata.yaml").write_text("name: x\n")
        assert find_charm_roots(tmp_path) == [tmp_path]


def _ref(url: str, *, branch: str | None = None) -> CharmRef:
    return CharmRef(
        team="x", name="charm", repo_url=url, key_charm=False,
        branch=branch, notes="",
    )


class TestCorpusOverrides:
    def test_empty_overrides_is_passthrough(self) -> None:
        ov = CorpusOverrides.empty()
        r = _ref("https://example.com/repo")
        adjusted, reason = ov.apply(r)
        assert adjusted is r
        assert reason is None

    def test_exclude_returns_reason(self) -> None:
        ov = CorpusOverrides(
            exclude={"https://example.com/bad": "not-a-charm — explanation"},
            branch_overrides={},
        )
        adjusted, reason = ov.apply(_ref("https://example.com/bad"))
        assert adjusted is None
        assert reason == "not-a-charm — explanation"

    def test_branch_override_replaces_branch(self) -> None:
        ov = CorpusOverrides(
            exclude={},
            branch_overrides={"https://example.com/mysql": "main"},
        )
        adjusted, reason = ov.apply(_ref("https://example.com/mysql"))
        assert reason is None
        assert adjusted is not None
        assert adjusted.branch == "main"
        assert adjusted.repo_url == "https://example.com/mysql"

    def test_branch_override_overrides_csv_branch(self) -> None:
        ov = CorpusOverrides(
            exclude={},
            branch_overrides={"https://example.com/x": "main"},
        )
        adjusted, _ = ov.apply(_ref("https://example.com/x", branch="readme"))
        assert adjusted is not None
        assert adjusted.branch == "main"

    def test_no_op_when_branch_matches(self) -> None:
        ov = CorpusOverrides(
            exclude={},
            branch_overrides={"https://example.com/x": "main"},
        )
        adjusted, _ = ov.apply(_ref("https://example.com/x", branch="main"))
        # Same branch, so no replacement needed — original ref returned.
        assert adjusted is not None
        assert adjusted.branch == "main"

    def test_exclude_wins_over_branch_override(self) -> None:
        ov = CorpusOverrides(
            exclude={"https://example.com/x": "skip me"},
            branch_overrides={"https://example.com/x": "main"},
        )
        adjusted, reason = ov.apply(_ref("https://example.com/x"))
        assert adjusted is None
        assert reason == "skip me"

    def test_sub_charm_skip_reason_matches(self) -> None:
        ov = CorpusOverrides(
            exclude={},
            branch_overrides={},
            sub_charm_excludes={
                "https://example.com/repo/": {
                    "fixtures/dummy": "test fixture",
                    "fixtures/empty": "test fixture",
                }
            },
        )
        assert ov.sub_charm_skip_reason("https://example.com/repo/", "fixtures/dummy") == "test fixture"
        assert ov.sub_charm_skip_reason("https://example.com/repo/", "charms/real") is None
        assert ov.sub_charm_skip_reason("https://example.com/other/", "fixtures/dummy") is None


class TestLoadOverrides:
    def test_missing_file_yields_empty(self, tmp_path: Path) -> None:
        ov = load_overrides(tmp_path / "does-not-exist.yaml")
        assert ov.exclude == {}
        assert ov.branch_overrides == {}

    def test_loads_exclude_and_branch_lists(self, tmp_path: Path) -> None:
        p = tmp_path / "overrides.yaml"
        p.write_text(
            "exclude:\n"
            "  - {url: 'https://example.com/a', reason: 'not-a-charm'}\n"
            "branch_overrides:\n"
            "  - {url: 'https://example.com/b', branch: 'main'}\n"
        )
        ov = load_overrides(p)
        assert ov.exclude == {"https://example.com/a": "not-a-charm"}
        assert ov.branch_overrides == {"https://example.com/b": "main"}

    def test_loads_real_overrides_file(self) -> None:
        """The committed corpus-overrides.yaml parses and covers the expected rows."""
        root = Path(__file__).resolve().parent.parent.parent
        ov = load_overrides(root / "corpus-overrides.yaml")
        # 4 mysql branch overrides per CORPUS-TRIAGE.
        mysql_overrides = [u for u in ov.branch_overrides if "mysql" in u]
        assert len(mysql_overrides) == 4
        assert all(ov.branch_overrides[u] == "main" for u in mysql_overrides)
        # At least the not-a-charm + unreachable rows from CORPUS-TRIAGE are present.
        assert "https://github.com/kubernetes/kubernetes" in ov.exclude
        assert "https://github.com/canonical/etcd-operator" in ov.exclude
        assert "https://git.launchpad.net/landscape-client" in ov.exclude
        assert "https://github.com/canonical/bundle-jupyter/issues/" in ov.exclude
        # CALIBRATION §6: test-charms fixtures excluded from fan-out.
        test_charms = ov.sub_charm_excludes.get("https://github.com/benhoyt/test-charms/", {})
        assert set(test_charms) == {"statustest", "noticestest", "database"}

    def test_loads_sub_charm_excludes(self, tmp_path: Path) -> None:
        p = tmp_path / "overrides.yaml"
        p.write_text(
            "sub_charm_excludes:\n"
            "  - url: 'https://example.com/repo/'\n"
            "    sub_paths: [fixtures/a, fixtures/b]\n"
            "    reason: 'demo fixtures'\n"
        )
        ov = load_overrides(p)
        assert ov.sub_charm_excludes == {
            "https://example.com/repo/": {
                "fixtures/a": "demo fixtures",
                "fixtures/b": "demo fixtures",
            }
        }


# ── feature_excludes (CALIBRATION #9 follow-up) ──────────────────────────────


class TestFeatureExcludes:
    def test_feature_skip_reason_matches_root_charm(self) -> None:
        ov = CorpusOverrides(
            exclude={},
            branch_overrides={},
            feature_excludes={
                ("https://example.com/shim-charm", ""): {
                    "ops.pebble-ready": "shim — delegates upstream",
                },
            },
        )
        assert ov.feature_skip_reason("https://example.com/shim-charm", "", "ops.pebble-ready") \
            == "shim — delegates upstream"
        assert ov.feature_skip_reason("https://example.com/shim-charm", "", "ops.secrets") is None
        assert ov.feature_skip_reason("https://example.com/other", "", "ops.pebble-ready") is None

    def test_feature_skip_reason_matches_monorepo_sub_path(self) -> None:
        ov = CorpusOverrides(
            exclude={},
            branch_overrides={},
            feature_excludes={
                ("https://example.com/monorepo", "kubernetes"): {
                    "ops.pebble-ready": "reconcile-all in sibling package",
                },
            },
        )
        assert ov.feature_skip_reason("https://example.com/monorepo", "kubernetes", "ops.pebble-ready") \
            == "reconcile-all in sibling package"
        # different sub_path: no match
        assert ov.feature_skip_reason("https://example.com/monorepo", "machines", "ops.pebble-ready") is None
        # root-level lookup: no match
        assert ov.feature_skip_reason("https://example.com/monorepo", "", "ops.pebble-ready") is None

    def test_load_overrides_parses_feature_excludes(self, tmp_path: Path) -> None:
        p = tmp_path / "ov.yaml"
        p.write_text(
            "feature_excludes:\n"
            "  - url: https://example.com/shim\n"
            "    features: [ops.pebble-ready, ops.collect-status]\n"
            "    reason: shim charm\n"
            "  - url: https://example.com/mono\n"
            "    sub_path: kubernetes\n"
            "    features: [ops.pebble-ready]\n"
            "    reason: reconcile-all sibling pkg\n"
        )
        ov = load_overrides(p)
        assert ov.feature_excludes == {
            ("https://example.com/shim", ""): {
                "ops.pebble-ready": "shim charm",
                "ops.collect-status": "shim charm",
            },
            ("https://example.com/mono", "kubernetes"): {
                "ops.pebble-ready": "reconcile-all sibling pkg",
            },
        }

    def test_real_overrides_file_has_known_shim_entries(self) -> None:
        root = Path(__file__).resolve().parent.parent.parent
        ov = load_overrides(root / "corpus-overrides.yaml")
        # mongodb-k8s and mysql-router-operators/kubernetes are the two known
        # shim FPs per CALIBRATION #9 follow-up.
        assert ov.feature_skip_reason(
            "https://github.com/canonical/mongodb-k8s-operator", "", "ops.pebble-ready"
        )
        assert ov.feature_skip_reason(
            "https://github.com/canonical/mysql-router-operators",
            "kubernetes", "ops.pebble-ready"
        )
