"""Tests for charmtally.rocks: the CSV filter, the raw-URL builder, and the
rockcraft.yaml facts the rootless metric reads. No network — every test
injects its own fetcher."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .. import rocks

if TYPE_CHECKING:
    from pathlib import Path

_HEADER = (
    "Team,Rock Name,Repository,Branch (if not the default),Source,"
    "Rockcraft Path,Stars,Last Push,Archived,Fork,Notes\n"
)


def _csv(tmp_path: Path, *rows: str) -> Path:
    path = tmp_path / "rocks.csv"
    path.write_text(_HEADER + "".join(row + "\n" for row in rows))
    return path


def test_load_csv_reads_a_row(tmp_path: Path) -> None:
    path = _csv(
        tmp_path,
        "canonical,foo,https://github.com/canonical/foo,,github-code-search,"
        "rockcraft.yaml,3,2026-01-01,FALSE,FALSE,",
    )

    refs = rocks.load_csv(path)

    assert [(r.name, r.path, r.branch) for r in refs] == [("foo", "rockcraft.yaml", "")]


def test_load_csv_drops_archived_and_forks(tmp_path: Path) -> None:
    path = _csv(
        tmp_path,
        "c,live,https://github.com/c/live,,s,rockcraft.yaml,0,2026-01-01,FALSE,FALSE,",
        "c,dead,https://github.com/c/dead,,s,rockcraft.yaml,0,2020-01-01,TRUE,FALSE,",
        "c,forked,https://github.com/c/forked,,s,rockcraft.yaml,0,2026-01-01,FALSE,TRUE,",
    )

    assert [r.name for r in rocks.load_csv(path)] == ["live"]


def test_load_csv_drops_rows_without_a_repo_or_path(tmp_path: Path) -> None:
    path = _csv(
        tmp_path,
        "c,norepo,,,s,rockcraft.yaml,0,2026-01-01,FALSE,FALSE,",
        "c,nopath,https://github.com/c/nopath,,s,,0,2026-01-01,FALSE,FALSE,",
    )

    assert rocks.load_csv(path) == []


def test_raw_url_uses_head_when_no_branch_is_named() -> None:
    ref = rocks.RockRef(
        name="foo", repo_url="https://github.com/canonical/foo", path="rockcraft.yaml"
    )

    assert (
        rocks.raw_url(ref) == "https://raw.githubusercontent.com/canonical/foo/HEAD/rockcraft.yaml"
    )


def test_raw_url_honours_a_named_branch_and_nested_path() -> None:
    ref = rocks.RockRef(
        name="bar",
        repo_url="https://github.com/canonical/bar.git",
        path="images/bar/rockcraft.yaml",
        branch="3.6/stable",
    )

    assert rocks.raw_url(ref) == (
        "https://raw.githubusercontent.com/canonical/bar/3.6/stable/images/bar/rockcraft.yaml"
    )


def test_slug_distinguishes_rocks_in_one_repo() -> None:
    repo = "https://github.com/canonical/mono"
    a = rocks.RockRef(name="x", repo_url=repo, path="a/rockcraft.yaml")
    b = rocks.RockRef(name="x", repo_url=repo, path="b/rockcraft.yaml")

    assert a.slug != b.slug


def test_facts_read_run_user() -> None:
    facts = rocks.facts_from_rockcraft("name: foo\nrun-user: _daemon_\n")

    assert facts == {"run_user": "_daemon_", "name": "foo"}


def test_facts_report_an_absent_run_user_as_none() -> None:
    """Absent is rockcraft's own default (root) — a reading, not a gap."""
    facts = rocks.facts_from_rockcraft("name: foo\nservices:\n  x:\n    command: /bin/x\n")

    assert facts is not None
    assert facts["run_user"] is None


def test_facts_return_none_for_unparseable_yaml() -> None:
    assert rocks.facts_from_rockcraft("name: [unclosed\n") is None
    assert rocks.facts_from_rockcraft("just a string") is None


def test_scan_rocks_marks_unfetchable_rocks_unreadable() -> None:
    refs = [
        rocks.RockRef(name="ok", repo_url="https://github.com/c/ok", path="rockcraft.yaml"),
        rocks.RockRef(name="gone", repo_url="https://github.com/c/gone", path="rockcraft.yaml"),
    ]

    def fetch(url: str) -> str | None:
        return "run-user: _daemon_\n" if "/c/ok/" in url else None

    records = rocks.scan_rocks(refs, fetch=fetch, workers=2)

    assert records["c/ok:rockcraft.yaml"] == {
        "name": "ok",
        "repo_url": "https://github.com/c/ok",
        "path": "rockcraft.yaml",
        "team": "",
        "readable": True,
        "run_user": "_daemon_",
    }
    gone = records["c/gone:rockcraft.yaml"]
    assert gone["readable"] is False
    assert gone["run_user"] is None


def test_scan_rocks_of_nothing_is_empty() -> None:
    assert rocks.scan_rocks([], fetch=lambda _url: None) == {}
