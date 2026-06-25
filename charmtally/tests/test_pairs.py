"""Tests for the k8s/machine pair detector (charmtally.pairs)."""

from __future__ import annotations

from ..pairs import Pair, find_pairs


def _record(name: str, *, has_containers: bool, repo_url: str = "", libs: list[str] | None = None) -> dict:
    return {
        "name": name,
        "repo_url": repo_url or f"https://github.com/x/{name}",
        "features": {
            "__meta__": {
                "has_containers": has_containers,
                "library_names": libs or [],
            }
        },
    }


def _corpus(*recs: tuple[str, dict]) -> dict:
    return {slug: rec for slug, rec in recs}


def test_no_pairs_in_single_substrate_corpus() -> None:
    corpus = _corpus(
        ("a", _record("a", has_containers=True)),
        ("b", _record("b", has_containers=True)),
    )
    assert find_pairs(corpus) == []


def test_high_confidence_pair_basic() -> None:
    corpus = _corpus(
        ("postgresql", _record("postgresql", has_containers=False)),
        ("postgresql-k8s", _record("postgresql-k8s", has_containers=True)),
    )
    pairs = find_pairs(corpus)
    assert len(pairs) == 1
    p = pairs[0]
    assert p.root == "postgresql"
    assert p.k8s_name == "postgresql-k8s"
    assert p.machine_name == "postgresql"
    assert p.confidence == "high"
    assert p.same_repo is False  # different repos in this fixture


def test_high_confidence_with_operator_suffixes() -> None:
    """`foo-k8s-operator` should pair with `foo-operator`."""
    corpus = _corpus(
        ("postgresql-operator", _record("postgresql-operator", has_containers=False)),
        ("postgresql-k8s-operator", _record("postgresql-k8s-operator", has_containers=True)),
    )
    pairs = find_pairs(corpus)
    assert len(pairs) == 1
    assert pairs[0].root == "postgresql"
    assert pairs[0].confidence == "high"


def test_same_repo_detected_when_repo_urls_match() -> None:
    """A true monorepo: both members share repo_url (modulo .git)."""
    same = "https://github.com/x/charm-operators"
    corpus = _corpus(
        ("a/foo-operator", _record("foo-operator", has_containers=False, repo_url=same + ".git")),
        ("a/foo-k8s-operator", _record("foo-k8s-operator", has_containers=True, repo_url=same)),
    )
    pairs = find_pairs(corpus)
    assert len(pairs) == 1
    assert pairs[0].same_repo is True


def test_medium_confidence_pair_with_typo() -> None:
    """One typo apart (edit-distance 1) → medium confidence."""
    corpus = _corpus(
        ("postgresqlx", _record("postgresqlx", has_containers=False)),
        ("postgresql-k8s", _record("postgresql-k8s", has_containers=True)),
    )
    pairs = find_pairs(corpus)
    assert len(pairs) == 1
    assert pairs[0].confidence == "medium"


def test_no_pair_when_edit_distance_too_large() -> None:
    """Three+ characters apart → no pair (false-positive guard)."""
    corpus = _corpus(
        ("postgresqlxyz", _record("postgresqlxyz", has_containers=False)),
        ("postgresql-k8s", _record("postgresql-k8s", has_containers=True)),
    )
    pairs = find_pairs(corpus)
    assert pairs == []


def test_ambiguous_prefix_does_not_falsely_pair() -> None:
    """`prometheus-scrape-config-k8s` must not pair with `prometheus-operator`
    — their roots are far apart."""
    corpus = _corpus(
        ("prom", _record("prometheus-operator", has_containers=False)),
        ("psck8s", _record("prometheus-scrape-config-k8s", has_containers=True)),
    )
    pairs = find_pairs(corpus)
    assert pairs == []


def test_shares_charmlib_when_machine_charm_vendors_k8s_lib() -> None:
    corpus = _corpus(
        (
            "machine",
            _record("foo", has_containers=False, libs=["foo_k8s"]),
        ),
        ("k8s", _record("foo-k8s", has_containers=True)),
    )
    pairs = find_pairs(corpus)
    assert len(pairs) == 1
    assert pairs[0].shares_charmlib is True


def test_shares_charmlib_when_k8s_charm_vendors_machine_lib() -> None:
    corpus = _corpus(
        ("machine", _record("foo", has_containers=False)),
        (
            "k8s",
            _record("foo-k8s", has_containers=True, libs=["foo"]),
        ),
    )
    pairs = find_pairs(corpus)
    assert len(pairs) == 1
    assert pairs[0].shares_charmlib is True


def test_shares_charmlib_false_when_no_overlap() -> None:
    corpus = _corpus(
        ("machine", _record("foo", has_containers=False, libs=["unrelated"])),
        ("k8s", _record("foo-k8s", has_containers=True, libs=["another"])),
    )
    pairs = find_pairs(corpus)
    assert pairs[0].shares_charmlib is False


def test_pairs_skip_double_underscore_meta_rows() -> None:
    """`__skipped__`-style rows in results.json must not become pair candidates."""
    corpus = {
        "__skipped__": {"foo": "not a charm record"},
        "postgresql": _record("postgresql", has_containers=False),
        "postgresql-k8s": _record("postgresql-k8s", has_containers=True),
    }
    pairs = find_pairs(corpus)
    assert len(pairs) == 1


def test_pairs_sorted_high_before_medium_then_by_root() -> None:
    corpus = _corpus(
        ("alpha-k8s", _record("alpha-k8s", has_containers=True)),
        ("alpha", _record("alpha", has_containers=False)),
        ("beta-k8s", _record("beta-k8s", has_containers=True)),
        ("betta", _record("betta", has_containers=False)),  # edit-distance 1 -> medium
    )
    pairs = find_pairs(corpus)
    assert [p.confidence for p in pairs] == ["high", "medium"]
    assert pairs[0].root == "alpha"


def test_pair_record_is_a_frozen_dataclass() -> None:
    """Sanity check: callers can hash/serialise reliably."""
    p = Pair(
        root="r",
        k8s_name="r-k8s",
        machine_name="r",
        k8s_repo_url="",
        machine_repo_url="",
        confidence="high",
        same_repo=False,
        shares_charmlib=False,
    )
    # frozen dataclass instances are hashable
    assert hash(p) == hash(p)
