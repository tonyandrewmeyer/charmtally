"""Tests for corpus CSV fetching — retry, fallback, and the load/override pair."""

from __future__ import annotations

import urllib.error
from typing import TYPE_CHECKING

import pytest

from .. import corpus

if TYPE_CHECKING:
    from pathlib import Path

_CSV = "Team,Charm Name,Repository\nx,foo,https://github.com/canonical/foo\n"


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _patch_urlopen(monkeypatch: pytest.MonkeyPatch, outcomes: list[object]) -> list[str]:
    """Make urlopen return/raise `outcomes` in order. Returns the call log."""
    calls: list[str] = []
    remaining = list(outcomes)

    def fake_urlopen(url: str, timeout: int = 0) -> _FakeResponse:
        calls.append(url)
        outcome = remaining.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, bytes)
        return _FakeResponse(outcome)

    monkeypatch.setattr(corpus.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(corpus.time, "sleep", lambda _: None)
    return calls


def test_fetch_to_writes_the_body(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_urlopen(monkeypatch, [_CSV.encode()])
    dest = corpus.fetch_to("https://example/charms.csv", tmp_path / "sub" / "corpus.csv")

    assert dest.read_text() == _CSV


def test_fetch_to_retries_a_transient_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One GitHub blip used to abandon the whole weekly scan."""
    calls = _patch_urlopen(monkeypatch, [urllib.error.URLError("boom"), _CSV.encode()])
    dest = corpus.fetch_to("https://example/charms.csv", tmp_path / "corpus.csv")

    assert len(calls) == 2
    assert dest.read_text() == _CSV


def test_fetch_to_falls_back_to_the_cached_copy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CI workdir is cached across runs, so `dest` is usually last week's
    copy of the same file. Scanning a stale corpus beats scanning nothing —
    but it must say so."""
    dest = tmp_path / "corpus.csv"
    dest.write_text(_CSV)
    _patch_urlopen(monkeypatch, [urllib.error.URLError("boom")] * 3)

    assert corpus.fetch_to("https://example/charms.csv", dest) == dest
    assert dest.read_text() == _CSV
    assert "falling back to the cached copy" in capsys.readouterr().err


def test_fetch_to_raises_when_there_is_nothing_to_fall_back_to(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_urlopen(monkeypatch, [urllib.error.URLError("boom")] * 3)

    with pytest.raises(urllib.error.URLError):
        corpus.fetch_to("https://example/charms.csv", tmp_path / "corpus.csv")


def test_fetch_to_leaves_the_cached_copy_intact_on_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed fetch must not truncate `dest`, or the next run's fallback
    would pick up a corrupt corpus."""
    dest = tmp_path / "corpus.csv"
    dest.write_text(_CSV)
    _patch_urlopen(monkeypatch, [urllib.error.URLError("boom")] * 3)

    corpus.fetch_to("https://example/charms.csv", dest)

    assert dest.read_text() == _CSV
