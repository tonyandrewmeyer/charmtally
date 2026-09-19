"""Tests for charmtally.charmlib_pairs: the curated equivalence table and the
identity lookup the library census is built on."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from .. import charmlib_pairs

if TYPE_CHECKING:
    from pathlib import Path


def _table(path: Path, body: str) -> charmlib_pairs.PairTable:
    path.write_text(body, encoding="utf-8")
    return charmlib_pairs.load(path)


def test_identity_collapses_both_names_of_a_paired_library() -> None:
    table = charmlib_pairs.default_table()

    # The dependency-line spelling and the import spelling of one charmlib.
    assert table.identity(charmlib="interfaces.tls") == "tls-certificates"
    assert table.identity(charmlib="interfaces.tls_certificates") == "tls-certificates"
    assert table.identity(charmhub="tls_certificates_interface") == "tls-certificates"


def test_an_unpaired_name_is_its_own_identity() -> None:
    """The table lists only pairs, so everything else counts under its own name."""
    table = charmlib_pairs.default_table()

    assert table.identity(charmhub="loki_k8s") == "loki_k8s"
    assert table.identity(charmlib="pathops") == "pathops"


def test_a_name_claimed_by_two_rows_is_an_error(tmp_path: Path) -> None:
    """Otherwise the census would depend on which row happened to load last."""
    with pytest.raises(ValueError, match="oathkeeper"):
        _table(
            tmp_path / "pairs.yaml",
            "pairs:\n"
            "  - id: auth-proxy\n"
            "    charmlibs: [interfaces.auth_proxy]\n"
            "    charmhub: [oathkeeper]\n"
            "  - id: forward-auth\n"
            "    charmlibs: [interfaces.forward_auth]\n"
            "    charmhub: [oathkeeper]\n",
        )


def test_shipped_table_loads_and_pairs_both_sides() -> None:
    """Every row must name at least one name on each side to be a pair at all."""
    table = charmlib_pairs.default_table()

    assert table.pairs
    for pair in table.pairs:
        assert pair.charmlibs, pair.id
        assert pair.charmhub, pair.id
