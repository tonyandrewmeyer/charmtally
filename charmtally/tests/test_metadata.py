"""Tests for charm-tree metadata extraction."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..metadata import CharmMeta, Relation, read

if TYPE_CHECKING:
    from pathlib import Path


def _ops_charm(d: Path) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / "charmcraft.yaml").write_text("type: charm\nname: x\n")
    return d


def test_no_reactive_files_means_not_reactive(tmp_path: Path) -> None:
    _ops_charm(tmp_path)
    assert read(tmp_path).is_reactive is False


def test_canonical_reactive_layout_detected(tmp_path: Path) -> None:
    _ops_charm(tmp_path)
    (tmp_path / "layer.yaml").write_text("includes: []\n")
    reactive = tmp_path / "reactive"
    reactive.mkdir()
    (reactive / "handlers.py").write_text("# reactive handlers\n")
    assert read(tmp_path).is_reactive is True


def test_reactive_python_package_layout_detected(tmp_path: Path) -> None:
    """Cassandra-style: reactive/<name>/handlers.py (Python package)."""
    _ops_charm(tmp_path)
    (tmp_path / "layer.yaml").write_text("includes: []\n")
    pkg = tmp_path / "reactive" / "cassandra"
    pkg.mkdir(parents=True)
    (pkg / "client.py").write_text("# handler\n")
    assert read(tmp_path).is_reactive is True


def test_openstack_reactive_layout_detected(tmp_path: Path) -> None:
    """OpenStack charm-* family: osci.yaml + src/reactive/*.py, no layer.yaml."""
    _ops_charm(tmp_path)
    (tmp_path / "osci.yaml").write_text("# OSCI tooling\n")
    src_reactive = tmp_path / "src" / "reactive"
    src_reactive.mkdir(parents=True)
    (src_reactive / "aodh_handlers.py").write_text("# OpenStack reactive handler\n")
    assert read(tmp_path).is_reactive is True


def test_reactive_dir_without_indicator_is_not_reactive(tmp_path: Path) -> None:
    """A bare reactive/ directory without layer.yaml or osci.yaml shouldn't
    flip a charm to reactive — could be an unrelated dir name."""
    _ops_charm(tmp_path)
    reactive = tmp_path / "reactive"
    reactive.mkdir()
    (reactive / "stub.py").write_text("# not reactive\n")
    assert read(tmp_path).is_reactive is False


def test_indicator_without_handlers_is_not_reactive(tmp_path: Path) -> None:
    """layer.yaml alone (no handlers) shouldn't flip — could be a stub."""
    _ops_charm(tmp_path)
    (tmp_path / "layer.yaml").write_text("includes: []\n")
    assert read(tmp_path).is_reactive is False


def test_src_nested_layer_yaml_layout_detected(tmp_path: Path) -> None:
    """charmed-kubernetes layer-* family: layer.yaml + reactive/ both under
    src/, no root-level indicator at all. CALIBRATION #38/#39 follow-up #15
    (e.g. charm-flannel, charm-containerd, charm-easyrsa)."""
    _ops_charm(tmp_path)
    src = tmp_path / "src"
    (src / "layer.yaml").parent.mkdir(parents=True, exist_ok=True)
    (src / "layer.yaml").write_text("includes: []\n")
    src_reactive = src / "reactive"
    src_reactive.mkdir()
    (src_reactive / "handlers.py").write_text("# reactive handlers\n")
    assert read(tmp_path).is_reactive is True


def test_src_nested_osci_yaml_layout_detected(tmp_path: Path) -> None:
    """osci.yaml nested under src/ alongside src/reactive/, mirroring the
    src/layer.yaml case. CALIBRATION #39 follow-up #15."""
    _ops_charm(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    (src / "osci.yaml").write_text("# OSCI tooling\n")
    src_reactive = src / "reactive"
    src_reactive.mkdir()
    (src_reactive / "handlers.py").write_text("# reactive handlers\n")
    assert read(tmp_path).is_reactive is True


def test_src_nested_indicator_without_handlers_is_not_reactive(tmp_path: Path) -> None:
    """src/layer.yaml alone (no reactive/ handlers anywhere) shouldn't
    flip — same must-not-regress shape as the root-level indicator-only
    case above, extended to the src/-nested indicator."""
    _ops_charm(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    (src / "layer.yaml").write_text("includes: []\n")
    assert read(tmp_path).is_reactive is False


def test_type_secret_config_extracted(tmp_path: Path) -> None:
    """Config options declared `type: secret` populate secret_typed_config."""
    d = tmp_path
    d.mkdir(parents=True, exist_ok=True)
    (d / "charmcraft.yaml").write_text(
        "type: charm\n"
        "name: x\n"
        "config:\n"
        "  options:\n"
        "    san-password:\n"
        "      type: secret\n"
        "    plain-token:\n"
        "      type: string\n"
        "    san-login:\n"
        "      type: secret\n"
    )
    meta = read(d)
    assert set(meta.config_keys) == {"san-password", "plain-token", "san-login"}
    assert set(meta.secret_typed_config) == {"san-password", "san-login"}


# ── descriptive metadata facts (brainstorm batch) ────────────────────────────


def test_charm_name_extracted(tmp_path: Path) -> None:
    _ops_charm(tmp_path)
    # _ops_charm wrote `name: x`
    assert read(tmp_path).charm_name == "x"


def test_charmcraft_plugins_extracted(tmp_path: Path) -> None:
    (tmp_path / "charmcraft.yaml").write_text(
        "type: charm\nname: c\nparts:\n  charm:\n    plugin: uv\n    source: .\n  thing:\n    plugin: python\n"
    )
    meta = read(tmp_path)
    assert set(meta.charmcraft_plugins) == {"uv", "python"}


def test_bases_extracted_v2(tmp_path: Path) -> None:
    (tmp_path / "charmcraft.yaml").write_text(
        "type: charm\nname: c\nbase: ubuntu@22.04\nbuild-base: ubuntu@24.04\n"
    )
    meta = read(tmp_path)
    assert meta.bases == ("ubuntu@22.04", "ubuntu@24.04")


def test_bases_extracted_v1(tmp_path: Path) -> None:
    (tmp_path / "charmcraft.yaml").write_text(
        "type: charm\nname: c\nbases:\n  - name: ubuntu\n    channel: '22.04'\n  - name: ubuntu\n    channel: '24.04'\n"
    )
    meta = read(tmp_path)
    assert meta.bases == ("ubuntu@22.04", "ubuntu@24.04")


def test_min_juju_version_from_assumes_list(tmp_path: Path) -> None:
    (tmp_path / "charmcraft.yaml").write_text(
        "type: charm\nname: c\nassumes: ['juju >= 3.4', 'k8s-api']\n"
    )
    assert read(tmp_path).min_juju_version == "3.4"


def test_min_juju_version_from_assumes_nested(tmp_path: Path) -> None:
    (tmp_path / "charmcraft.yaml").write_text(
        "type: charm\nname: c\nassumes:\n  - any-of: ['juju >= 3.6', 'juju >= 3.4']\n"
    )
    # min returns the lowest mentioned
    assert read(tmp_path).min_juju_version == "3.4"


def test_min_juju_version_absent(tmp_path: Path) -> None:
    _ops_charm(tmp_path)
    assert read(tmp_path).min_juju_version is None


def test_library_count(tmp_path: Path) -> None:
    _ops_charm(tmp_path)
    for lib in ("grafana_k8s", "loki_k8s", "tempo_coordinator_k8s"):
        d = tmp_path / "lib" / "charms" / lib / "v0"
        d.mkdir(parents=True)
        (d / f"{lib}.py").write_text("# vendored lib\n")
    meta = read(tmp_path)
    assert meta.library_count == 3


def test_library_count_excludes_the_charms_own_published_lib(tmp_path: Path) -> None:
    """`lib/charms/<own_name>/` is a lib this charm PUBLISHES, not one it uses."""
    (tmp_path / "charmcraft.yaml").write_text("type: charm\nname: my-charm\n")
    for lib in ("my_charm", "grafana_k8s"):
        d = tmp_path / "lib" / "charms" / lib / "v0"
        d.mkdir(parents=True)
        (d / f"{lib}.py").write_text("# lib\n")

    meta = read(tmp_path)

    assert meta.library_names == ("grafana_k8s",)
    assert meta.library_count == 1
    assert meta.provides_own_library is True


def test_provides_own_library_true(tmp_path: Path) -> None:
    (tmp_path / "charmcraft.yaml").write_text("type: charm\nname: my-charm\n")
    own = tmp_path / "lib" / "charms" / "my_charm" / "v0"
    own.mkdir(parents=True)
    (own / "my_charm.py").write_text("# own lib\n")
    assert read(tmp_path).provides_own_library is True


def test_provides_own_library_false(tmp_path: Path) -> None:
    _ops_charm(tmp_path)
    other = tmp_path / "lib" / "charms" / "someone_else" / "v0"
    other.mkdir(parents=True)
    (other / "x.py").write_text("# consumer lib\n")
    assert read(tmp_path).provides_own_library is False


def test_charm_user_is_read_from_charmcraft_yaml(tmp_path: Path) -> None:
    (tmp_path / "charmcraft.yaml").write_text(
        "type: charm\nname: my-charm\ncharm-user: non-root\n"
    )

    assert read(tmp_path).charm_user == "non-root"


def test_charm_user_is_none_when_unset(tmp_path: Path) -> None:
    """Unset is Juju's default (root) — recorded as None, not as "root"."""
    _ops_charm(tmp_path)

    assert read(tmp_path).charm_user is None


def test_has_terraform_module(tmp_path: Path) -> None:
    _ops_charm(tmp_path)
    (tmp_path / "terraform").mkdir()
    assert read(tmp_path).has_terraform_module is True


def test_tooling_combinations(tmp_path: Path) -> None:
    _ops_charm(tmp_path)
    (tmp_path / "tox.ini").write_text("[tox]\n")
    (tmp_path / "Makefile").write_text("all:\n")
    meta = read(tmp_path)
    assert set(meta.tooling) == {"tox", "make"}


def test_modern_ops_charm_is_not_legacy_classic(tmp_path: Path) -> None:
    _ops_charm(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    (src / "charm.py").write_text("# modern ops charm\n")
    assert read(tmp_path).is_legacy_classic is False


def test_legacy_hooks_layout_detected(tmp_path: Path) -> None:
    """Pre-ops openstack-charmers / IS-team legacy charms: hooks/ + no src/charm.py."""
    _ops_charm(tmp_path)
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "install").write_text("#!/bin/sh\necho install\n")
    (hooks / "config-changed").write_text("#!/bin/sh\necho changed\n")
    assert read(tmp_path).is_legacy_classic is True


def test_empty_hooks_dir_is_not_legacy(tmp_path: Path) -> None:
    """Bare empty hooks/ shouldn't flip — could be scaffolding."""
    _ops_charm(tmp_path)
    (tmp_path / "hooks").mkdir()
    assert read(tmp_path).is_legacy_classic is False


def test_hooks_with_modern_entry_is_not_legacy(tmp_path: Path) -> None:
    """Some ops charms keep a small hooks/ shim for migration. With src/charm.py
    present, the modern entry wins."""
    _ops_charm(tmp_path)
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "install").write_text("#!/bin/sh\necho install\n")
    src = tmp_path / "src"
    src.mkdir()
    (src / "charm.py").write_text("# modern ops charm\n")
    assert read(tmp_path).is_legacy_classic is False


def test_reactive_charm_does_not_double_classify_as_legacy(tmp_path: Path) -> None:
    """is_reactive wins over is_legacy_classic for the small overlap case."""
    _ops_charm(tmp_path)
    (tmp_path / "osci.yaml").write_text("# OSCI tooling\n")
    src_reactive = tmp_path / "src" / "reactive"
    src_reactive.mkdir(parents=True)
    (src_reactive / "h.py").write_text("# handler\n")
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "install").write_text("#!/bin/sh\n")
    m = read(tmp_path)
    assert m.is_reactive is True
    assert m.is_legacy_classic is False


def test_charm_without_subordinate_key_is_not_subordinate(tmp_path: Path) -> None:
    _ops_charm(tmp_path)
    assert read(tmp_path).is_subordinate is False


def test_subordinate_true_in_charmcraft_yaml(tmp_path: Path) -> None:
    (tmp_path / "charmcraft.yaml").write_text("type: charm\nname: x\nsubordinate: true\n")
    assert read(tmp_path).is_subordinate is True


def test_subordinate_true_in_metadata_yaml(tmp_path: Path) -> None:
    (tmp_path / "metadata.yaml").write_text("name: x\nsubordinate: true\n")
    assert read(tmp_path).is_subordinate is True


def test_subordinate_string_true_tolerated(tmp_path: Path) -> None:
    """Hand-edited metadata.yaml with the string \"true\" still counts."""
    (tmp_path / "metadata.yaml").write_text('name: x\nsubordinate: "true"\n')
    assert read(tmp_path).is_subordinate is True


def test_subordinate_false_explicit(tmp_path: Path) -> None:
    (tmp_path / "charmcraft.yaml").write_text("type: charm\nname: x\nsubordinate: false\n")
    assert read(tmp_path).is_subordinate is False


# workload-less


def test_workload_less_bare_charm(tmp_path: Path) -> None:
    """Charm with no containers, no pebble, no juju-info — workload-less."""
    _ops_charm(tmp_path)
    assert read(tmp_path).is_workload_less is True


def test_workload_less_false_when_containers_present(tmp_path: Path) -> None:
    (tmp_path / "charmcraft.yaml").write_text(
        "type: charm\nname: x\ncontainers:\n  workload:\n    resource: workload-image\n"
    )
    assert read(tmp_path).is_workload_less is False


def test_workload_less_false_with_pebble_layer_call_in_src(tmp_path: Path) -> None:
    _ops_charm(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    (src / "charm.py").write_text(
        "from ops import pebble\nlayer = pebble.Layer({'services': {}})\n"
    )
    assert read(tmp_path).is_workload_less is False


def test_workload_less_false_with_layer_yaml(tmp_path: Path) -> None:
    _ops_charm(tmp_path)
    (tmp_path / "layer.yaml").write_text("services: {}\n")
    assert read(tmp_path).is_workload_less is False


def test_workload_less_false_with_juju_info_requires(tmp_path: Path) -> None:
    """A subordinate (juju-info requires) is not workload-less in James's sense."""
    (tmp_path / "charmcraft.yaml").write_text(
        "type: charm\nname: x\nrequires:\n  general-info:\n    interface: juju-info\n    scope: container\n"
    )
    assert read(tmp_path).is_workload_less is False


def test_workload_less_ignores_layer_yaml_under_tests(tmp_path: Path) -> None:
    """A `layer.yaml` fixture under tests/ shouldn't flip the chip."""
    _ops_charm(tmp_path)
    tests = tmp_path / "tests" / "fixtures"
    tests.mkdir(parents=True)
    (tests / "layer.yaml").write_text("services: {}\n")
    assert read(tmp_path).is_workload_less is True


def test_workload_less_other_requires_interfaces_dont_disqualify(tmp_path: Path) -> None:
    """`requires:` bindings to non-juju-info interfaces are fine."""
    (tmp_path / "charmcraft.yaml").write_text(
        "type: charm\nname: x\nrequires:\n  db:\n    interface: postgresql_client\n"
    )
    assert read(tmp_path).is_workload_less is True


def test_charm_meta_dict_round_trip() -> None:
    """Every field survives to_dict() -> from_dict() unchanged."""
    meta = CharmMeta(
        has_containers=True,
        relations=(Relation("db", "requires", "postgresql_client"),),
        config_keys=("a", "b"),
        secret_like_config=("api-key",),
        secret_typed_config=("api-key",),
        has_integration_tests=True,
        is_reactive=False,
        is_legacy_classic=False,
        is_subordinate=True,
        is_workload_less=True,
        charm_name="demo",
        charmcraft_plugins=("uv",),
        bases=("ubuntu@24.04",),
        min_juju_version="3.6",
        library_count=2,
        library_names=("foo", "bar"),
        provides_own_library=True,
        has_terraform_module=True,
        tooling=("tox", "just"),
        repo_sha="0123456789abcdef",
    )
    assert CharmMeta.from_dict(meta.to_dict()) == meta


def test_charm_meta_from_dict_tolerates_missing_keys() -> None:
    """Snapshots written by older scans still load, falling back to defaults."""
    meta = CharmMeta.from_dict({})
    assert meta == CharmMeta(
        has_containers=False,
        relations=(),
        config_keys=(),
        secret_like_config=(),
        secret_typed_config=(),
        has_integration_tests=False,
        is_reactive=False,
    )
    assert meta.repo_sha is None


def test_charm_meta_from_dict_ignores_unknown_keys() -> None:
    """`architecture` rides alongside __meta__ but isn't a CharmMeta field."""
    meta = CharmMeta.from_dict({"architecture": ["reconcile"], "has_containers": True})
    assert meta.has_containers


# --- charmlibs namespace packages -------------------------------------------


def _src(d: Path, body: str) -> None:
    src = d / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "charm.py").write_text(body)


def test_no_charmlibs_means_empty(tmp_path: Path) -> None:
    _ops_charm(tmp_path)
    _src(tmp_path, "import ops\nfrom charms.data_platform_libs.v0 import data_interfaces\n")
    meta = read(tmp_path)
    assert meta.charmlibs_count == 0
    assert meta.charmlibs_names == ()


def test_charmlibs_imports_collected_and_deduped(tmp_path: Path) -> None:
    _ops_charm(tmp_path)
    _src(
        tmp_path,
        "from charmlibs import pathops\n"
        "from charmlibs.pathops import ContainerPath\n"
        "import charmlibs.apt\n"
        "from charmlibs import (\n    snap,\n    systemd as sysd,\n)\n",
    )
    meta = read(tmp_path)
    assert meta.charmlibs_names == ("apt", "pathops", "snap", "systemd")
    assert meta.charmlibs_count == 4


def test_charmlibs_interfaces_keep_their_second_segment(tmp_path: Path) -> None:
    """Two interface libs must not collapse into a single `interfaces` entry."""
    _ops_charm(tmp_path)
    _src(
        tmp_path,
        "from charmlibs.interfaces.tls_certificates.v1 import Requirer\n"
        "from charmlibs.interfaces.ingress import Provider\n",
    )
    assert read(tmp_path).charmlibs_names == ("interfaces.ingress", "interfaces.tls_certificates")


def test_charmlibs_requirements_count_without_an_import(tmp_path: Path) -> None:
    _ops_charm(tmp_path)
    (tmp_path / "requirements.txt").write_text(
        "ops>=2.17\ncharmlibs-pathops>=1.0\ncharmlibs_interfaces_ingress==0.3\n"
    )
    assert read(tmp_path).charmlibs_names == ("interfaces.ingress", "pathops")


def test_vendored_libs_do_not_contribute_charmlibs(tmp_path: Path) -> None:
    """A vendored Charmhub lib importing charmlibs is not this charm's adoption."""
    _ops_charm(tmp_path)
    vendored = tmp_path / "lib" / "charms" / "other_charm" / "v0"
    vendored.mkdir(parents=True)
    (vendored / "iface.py").write_text("from charmlibs import pathops\n")
    assert read(tmp_path).charmlibs_names == ()


def test_charmlibs_round_trips_through_meta_dict(tmp_path: Path) -> None:
    _ops_charm(tmp_path)
    _src(tmp_path, "from charmlibs import pathops\n")
    meta = read(tmp_path)
    assert CharmMeta.from_dict(meta.to_dict()).charmlibs_names == ("pathops",)


def test_ops_requirement_read_from_requirements_txt(tmp_path: Path) -> None:
    _ops_charm(tmp_path)
    (tmp_path / "requirements.txt").write_text("# deps\nops >= 2.15\npyyaml\n")
    meta = read(tmp_path)
    assert meta.ops_requirement == ">= 2.15"
    assert meta.ops_requirement_source == "requirements.txt"
    assert meta.ops_min_version == "2.15"


def test_ops_requirement_with_extras_and_upper_bound(tmp_path: Path) -> None:
    _ops_charm(tmp_path)
    (tmp_path / "requirements.txt").write_text("ops[testing]>=2.17,<3\n")
    meta = read(tmp_path)
    assert meta.ops_requirement == ">=2.17,<3"
    assert meta.ops_min_version == "2.17"


def test_unpinned_ops_is_its_own_category(tmp_path: Path) -> None:
    """A bare `ops` asks for something; a charm with no ops line does not."""
    _ops_charm(tmp_path)
    (tmp_path / "requirements.txt").write_text("ops\n")
    meta = read(tmp_path)
    assert meta.ops_requirement == ""
    assert meta.ops_requirement_source == "requirements.txt"
    assert meta.ops_min_version is None


def test_no_ops_requirement_anywhere(tmp_path: Path) -> None:
    _ops_charm(tmp_path)
    (tmp_path / "requirements.txt").write_text("pyyaml\n")
    meta = read(tmp_path)
    assert meta.ops_requirement is None
    assert meta.ops_requirement_source is None
    assert meta.ops_min_version is None


def test_ops_scenario_is_not_ops(tmp_path: Path) -> None:
    _ops_charm(tmp_path)
    (tmp_path / "requirements.txt").write_text("ops-scenario>=7\nopslib-openstack\n")
    assert read(tmp_path).ops_requirement is None


def test_ops_requirement_ignores_environment_markers_and_comments(tmp_path: Path) -> None:
    _ops_charm(tmp_path)
    (tmp_path / "requirements.txt").write_text(
        "-r other.txt\nops~=2.4 ; python_version >= '3.10'  # the framework\n"
    )
    meta = read(tmp_path)
    assert meta.ops_requirement == "~=2.4"
    assert meta.ops_min_version == "2.4"


def test_ops_requirement_from_pep621_pyproject(tmp_path: Path) -> None:
    _ops_charm(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndependencies = ["pyyaml", "ops==2.4.1"]\n'
    )
    meta = read(tmp_path)
    assert meta.ops_requirement == "==2.4.1"
    assert meta.ops_requirement_source == "pyproject.toml"
    assert meta.ops_min_version == "2.4.1"


def test_ops_requirement_from_poetry_pyproject(tmp_path: Path) -> None:
    _ops_charm(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.poetry.dependencies]\npython = "^3.10"\nops = "^2.9"\n'
    )
    meta = read(tmp_path)
    assert meta.ops_requirement == "^2.9"
    assert meta.ops_min_version == "2.9"


def test_uv_lock_is_the_last_resort(tmp_path: Path) -> None:
    """A resolved version answers a different question, so it loses to a declared one."""
    _ops_charm(tmp_path)
    (tmp_path / "uv.lock").write_text('[[package]]\nname = "ops"\nversion = "2.20.0"\n')
    meta = read(tmp_path)
    assert meta.ops_requirement == "==2.20.0"
    assert meta.ops_requirement_source == "uv.lock"
    assert meta.ops_min_version == "2.20.0"

    (tmp_path / "requirements.txt").write_text("ops>=2.15\n")
    meta = read(tmp_path)
    assert meta.ops_requirement == ">=2.15"
    assert meta.ops_requirement_source == "requirements.txt"


def test_unreadable_pyproject_does_not_break_the_read(tmp_path: Path) -> None:
    _ops_charm(tmp_path)
    (tmp_path / "pyproject.toml").write_text("this is not toml =\n")
    assert read(tmp_path).ops_requirement is None


def test_ops_requirement_round_trips_through_meta_dict(tmp_path: Path) -> None:
    _ops_charm(tmp_path)
    (tmp_path / "requirements.txt").write_text("ops\n")
    meta = CharmMeta.from_dict(read(tmp_path).to_dict())
    # Empty string must survive: "unpinned" is not "unknown".
    assert meta.ops_requirement == ""
    assert meta.ops_min_version is None
