"""Tests for the inherited-Juju-ceiling join (#73)."""

from __future__ import annotations

from ..ceilings import derive, interface_ceilings


def _charm(name: str, *, provides=(), requires=(), ceiling: str | None = None) -> tuple[str, dict]:
    relations = [{"name": i, "role": "provides", "interface": i} for i in provides]
    relations += [{"name": i, "role": "requires", "interface": i} for i in requires]
    return name, {
        "name": name,
        "features": {"__meta__": {"relations": relations, "max_juju_version": ceiling}},
    }


def _corpus(*charms: tuple[str, dict]) -> dict[str, dict]:
    return dict(charms)


def test_ceiling_inherited_when_every_provider_is_capped() -> None:
    corpus = _corpus(
        _charm("db", provides=["sql"], ceiling="4"),
        _charm("db-k8s", provides=["sql"], ceiling="4.0.1"),
        _charm("app", requires=["sql"]),
    )
    assert interface_ceilings(corpus)["sql"] == "4"
    app = derive(corpus)["app"]
    assert app.version == "4"
    assert app.via == ("sql",)
    assert not app.unknown


def test_one_uncapped_provider_silences_the_interface() -> None:
    """The under-claiming half of the unanimity rule: an alternative backend
    with no ceiling is a backend the charm could deploy against instead."""
    corpus = _corpus(
        _charm("db", provides=["sql"], ceiling="4"),
        _charm("proxy", provides=["sql"]),
        _charm("app", requires=["sql"]),
    )
    assert interface_ceilings(corpus)["sql"] is None
    app = derive(corpus)["app"]
    assert app.version is None
    assert not app.unknown  # looked, and found no ceiling that binds


def test_test_fixture_charms_are_not_providers() -> None:
    """`any-charm` provides 263 interfaces and is nobody's deployment choice."""
    corpus = _corpus(
        _charm("db", provides=["sql"], ceiling="4"),
        _charm("any-charm", provides=["sql"]),
        _charm("app", requires=["sql"]),
    )
    assert derive(corpus)["app"].version == "4"


def test_interface_nobody_provides_reads_as_unknown() -> None:
    corpus = _corpus(_charm("app", requires=["ingress"]))
    app = derive(corpus)["app"]
    assert app.version is None
    assert app.unresolved == ("ingress",)
    assert app.unknown


def test_a_derived_ceiling_outranks_an_unresolved_endpoint() -> None:
    """An unresolvable partner means the real ceiling might be lower, not
    that the one we did derive stops being true."""
    corpus = _corpus(
        _charm("db", provides=["sql"], ceiling="4"),
        _charm("app", requires=["sql", "ingress"]),
    )
    app = derive(corpus)["app"]
    assert app.version == "4"
    assert app.unresolved == ("ingress",)
    assert not app.unknown


def test_lowest_ceiling_leads_and_via_is_ordered_by_it() -> None:
    corpus = _corpus(
        _charm("db", provides=["sql"], ceiling="4"),
        _charm("hub", provides=["spark"], ceiling="3.6.10"),
        _charm("app", requires=["sql", "spark"]),
    )
    app = derive(corpus)["app"]
    assert app.version == "3.6.10"
    assert app.via == ("spark", "sql")


def test_a_charm_does_not_inherit_from_itself() -> None:
    """Postgres requires the `postgresql_async` it also provides, for
    replication; reflecting its own assertion back would be circular."""
    corpus = _corpus(_charm("db", provides=["async"], requires=["async"], ceiling="4"))
    db = derive(corpus)["db"]
    assert db.version is None
    assert db.unresolved == ()


def test_every_charm_gets_an_entry() -> None:
    corpus = _corpus(_charm("app"), _charm("other", provides=["sql"]))
    assert set(derive(corpus)) == {"app", "other"}
    assert derive(corpus)["app"].version is None
