"""Tests for charm-tree metadata extraction."""
from __future__ import annotations

from pathlib import Path

from ..metadata import read


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
        "type: charm\n"
        "name: c\n"
        "parts:\n"
        "  charm:\n"
        "    plugin: uv\n"
        "    source: .\n"
        "  thing:\n"
        "    plugin: python\n"
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
        "type: charm\nname: c\n"
        "bases:\n"
        "  - name: ubuntu\n"
        "    channel: '22.04'\n"
        "  - name: ubuntu\n"
        "    channel: '24.04'\n"
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
        "type: charm\nname: c\n"
        "assumes:\n"
        "  - any-of: ['juju >= 3.6', 'juju >= 3.4']\n"
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
