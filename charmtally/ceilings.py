"""Derive each charm's *inherited* Juju ceiling from its relation partners (#73).

A charm's own ``assumes:`` does not tell you what Juju versions it can
actually run on. Every channel of ``postgresql-k8s`` declares
``assumes: juju < 4.0.0``, and because it is the de facto database for the
ecosystem that ceiling became every dependent's ceiling — each of them
discovering it the same way, with a failed deploy. A charm whose own
``assumes:`` says ``juju >= 3.0.2`` and nothing else is, as one review put
it, technically correct but misleading as a practical minimum.

Both halves are already scanned: ``__meta__.relations`` carries the
interface for each endpoint and ``__meta__.max_juju_version`` carries the
ceiling the charm asserts. So this is a join across the corpus rather than
anything new to collect, and it is done here — at render time, over the same
``__meta__`` blocks the snapshots keep whole — rather than written into
``__meta__`` by the scan. Two reasons: ``scan_charm`` sees one charm and this
question needs all of them, and a derived value in a snapshot is bytes that
stay in git forever to say something recomputable from the bytes next to it.

Three rules, and the reasoning for each matters more than the code:

* **Every provider must be capped.** An interface hands its ceiling to a
  requirer only when *all* of the interface's in-corpus providers declare
  one; the inherited value is then the lowest of them. Requiring only *one*
  capped provider over-reports badly: ``postgresql-k8s`` also provides
  ``grafana_dashboard``, ``prometheus_scrape`` and ``cos_agent``, which have
  100+ unconstrained providers each, so a single-provider rule caps a third
  of the corpus at Juju 4 for the sole reason that a scrape target happens
  to be a database. The cost of unanimity is that one uncapped provider
  silences the interface — ``postgresql_client`` is silenced by pgbouncer
  (a proxy that itself fronts postgres) and two dead legacy charms — but
  under-claiming is the right way to be wrong here: a ceiling this reports
  is one the charm cannot dodge by choosing a different backend.

* **Test fixtures are not providers.** ``any-charm`` provides 263
  interfaces. It exists to stand in for whatever an integration test needs
  a partner for, it is nobody's deployment choice, and left in the join it
  is the single uncapped provider that silences every interface that would
  otherwise be unanimous.

* **Out-of-corpus is ``unknown``, not "no ceiling".** An endpoint no charm
  in the corpus provides has not been shown to be unconstrained; it has not
  been looked at. Collapsing the two would report a charm we could not check
  identically to one we checked and cleared, which is the distinction the
  rest of the codebase is careful to preserve.

What this deliberately does not model: whether the charm actually *needs*
the relation. Optional endpoints, and charms that can take either of two
backends, are indistinguishable from mandatory ones in the metadata. The
claim a ``Ceiling`` makes is therefore about the interface rather than about
the deployment — "every charm in the corpus that provides ``mysql_client``
caps Juju below 4" — which holds whether or not the requirer can live
without it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from .metadata import juju_version_key

if TYPE_CHECKING:
    from collections.abc import Iterable

# Charms that exist to be a relation partner in someone else's integration
# test, rather than to be deployed. Excluded from the provider side of the
# join; see the module docstring. Keyed on the corpus charm name.
_TEST_FIXTURES = frozenset({"any-charm"})


class Ceiling(NamedTuple):
    """What the corpus can say about one charm's inherited Juju ceiling.

    ``version`` is the lowest ceiling the charm inherits, or None when it
    inherits none. ``via`` names the interfaces that imposed it, lowest
    first, for the tooltip. ``unresolved`` lists the required interfaces no
    in-corpus charm provides — the honest "we could not look" — and is what
    makes a charm with no ``version`` report as unknown rather than clear.
    """

    version: str | None
    via: tuple[str, ...]
    unresolved: tuple[str, ...]

    @property
    def unknown(self) -> bool:
        """True when nothing was derivable and something went unchecked."""
        return self.version is None and bool(self.unresolved)


def _endpoints(meta: dict, role: str) -> Iterable[str]:
    """Yield the interface of every relation endpoint of `meta` in `role`."""
    for rel in meta.get("relations") or []:
        if rel.get("role") == role and rel.get("interface"):
            yield rel["interface"]


def interface_ceilings(charms: dict[str, dict]) -> dict[str, str | None]:
    """Map each provided interface to the ceiling it imposes on a requirer.

    The value is the lowest ``max_juju_version`` across the interface's
    providers when every one of them declares a ceiling, and None when any
    provider is unconstrained. An interface absent from the mapping has no
    in-corpus provider at all, which is a different answer again — see
    `Ceiling.unresolved`.

    `charms` is the ``__`` -stripped results/scored mapping: charm name to
    record, each record carrying ``features.__meta__``.
    """
    capped: dict[str, list[str]] = {}
    uncapped: set[str] = set()
    for name, record in charms.items():
        if name in _TEST_FIXTURES:
            continue
        meta = record.get("features", {}).get("__meta__", {})
        ceiling = meta.get("max_juju_version")
        for interface in _endpoints(meta, "provides"):
            if ceiling:
                capped.setdefault(interface, []).append(ceiling)
            else:
                uncapped.add(interface)
    return {
        interface: (None if interface in uncapped else min(versions, key=juju_version_key))
        for interface, versions in capped.items()
    }


def derive(charms: dict[str, dict]) -> dict[str, Ceiling]:
    """Derive the inherited Juju ceiling for every charm in `charms`.

    Every charm gets an entry, including the ones that inherit nothing, so a
    caller can tell "derived: no ceiling" from "not in the corpus".
    """
    imposed = interface_ceilings(charms)
    out: dict[str, Ceiling] = {}
    for name, record in charms.items():
        meta = record.get("features", {}).get("__meta__", {})
        # A charm providing the interface it requires (postgres replicating
        # to itself over `postgresql_async`) would otherwise inherit its own
        # assertion back, dressed up as a corpus finding.
        own = set(_endpoints(meta, "provides"))
        found: dict[str, str] = {}
        unresolved: list[str] = []
        for interface in _endpoints(meta, "requires"):
            if interface in own:
                continue
            if interface not in imposed:
                if interface not in unresolved:
                    unresolved.append(interface)
                continue
            ceiling = imposed[interface]
            if ceiling:
                found[interface] = ceiling
        via = sorted(found, key=lambda i: (juju_version_key(found[i]), i))
        out[name] = Ceiling(
            version=found[via[0]] if via else None,
            via=tuple(via),
            unresolved=tuple(unresolved),
        )
    return out
