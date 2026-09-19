"""Tests for charmtally.charmhub: reading listing status out of the store's
answer, and keeping "we couldn't ask" distinct from "there is no such charm"."""

from __future__ import annotations

import json

from .. import charmhub


def _answer(status: int, body: object = None) -> object:
    def get(_url: str) -> tuple[int, str]:
        return status, "" if body is None else json.dumps(body)

    return get


def test_a_listed_charm_reads_as_listed() -> None:
    verdict = charmhub.lookup("x", get=_answer(200, {"result": {"unlisted": False}}))

    assert verdict == charmhub.LISTED


def test_an_unlisted_charm_reads_as_unlisted() -> None:
    verdict = charmhub.lookup("x", get=_answer(200, {"result": {"unlisted": True}}))

    assert verdict == charmhub.UNLISTED


def test_a_404_reads_as_absent() -> None:
    """The store knows its own names, so this one is final."""
    assert charmhub.lookup("x", get=_answer(404)) == charmhub.ABSENT


def test_a_404_is_not_retried() -> None:
    calls = []

    def get(url: str) -> tuple[int, str]:
        calls.append(url)
        return 404, ""

    charmhub.lookup("x", get=get)

    assert len(calls) == 1


def test_a_server_error_is_retried_then_gives_up_as_unknown() -> None:
    """None, not ABSENT: an outage is not a finding about the charm."""
    calls = []

    def get(url: str) -> tuple[int, str]:
        calls.append(url)
        return 503, ""

    verdict = charmhub.lookup("x", get=get, attempts=2)

    assert verdict is None
    assert len(calls) == 2


def test_a_200_without_the_field_is_not_an_answer() -> None:
    """Reading a payload that never mentioned listing as `listed` invents one."""
    assert charmhub.lookup("x", get=_answer(200, {"result": {}})) is None
    assert charmhub.lookup("x", get=_answer(200, {"name": "x"})) is None


def test_unparseable_json_is_not_an_answer() -> None:
    def get(_url: str) -> tuple[int, str]:
        return 200, "<html>gateway</html>"

    assert charmhub.lookup("x", get=get) is None


def test_listings_gives_every_name_a_key_including_the_failures() -> None:
    """Key presence is the `we looked` signal downstream depends on."""
    verdicts = {"a": charmhub.LISTED, "b": None}

    out = charmhub.listings(["b", "a", "a"], lookup_one=lambda n: verdicts[n], workers=2)

    assert out == {"a": charmhub.LISTED, "b": None}


def test_listings_asks_once_per_distinct_name() -> None:
    """A monorepo publishing one charm from several roots is one request."""
    asked = []

    out = charmhub.listings(
        ["a", "a", "a"], lookup_one=lambda n: asked.append(n) or charmhub.LISTED, workers=2
    )

    assert asked == ["a"]
    assert out == {"a": charmhub.LISTED}
