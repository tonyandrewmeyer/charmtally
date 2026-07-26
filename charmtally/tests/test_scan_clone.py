"""Tests for clone freshness and commit-SHA capture.

These shell out to real git — the behaviour under test *is* the git
interaction, so mocking it would test nothing. Kept to a handful of tiny
local repos so the suite stays fast.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from ..corpus import CharmRef
from ..scan import ensure_clone, head_sha, refresh_clone, scan_charm

if TYPE_CHECKING:
    from pathlib import Path

# Identity supplied per-invocation rather than via `git config` so repo setup
# stays at three subprocesses — these tests are the slowest in the suite and
# every saved `git` exec is ~50ms.
_IDENTITY = ["-c", "user.email=test@example.com", "-c", "user.name=test"]


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(path: Path, filename: str, body: str) -> str:
    """Write `filename`, commit it, and return the resulting SHA."""
    (path / filename).write_text(body)
    _git(["add", "-A"], path)
    return _git([*_IDENTITY, "commit", "-qm", f"add {filename}", "--no-gpg-sign"], path) or _git(
        ["rev-parse", "HEAD"], path
    )


def _init_repo(path: Path) -> str:
    """Create a one-commit git repo at `path`, returning the commit SHA."""
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q", "-b", "main"], path)
    return _commit(path, "charmcraft.yaml", "type: charm\nname: x\n")


def _ref(url: Path, branch: str | None = None) -> CharmRef:
    return CharmRef(
        team="t", name="n", repo_url=str(url), key_charm=False, branch=branch, notes=""
    )


class TestHeadSha:
    def test_resolves_from_root_and_subdirectory(self, tmp_path: Path) -> None:
        """Monorepo sub-charms are scanned from a subdirectory of the clone;
        git walks upwards, so both must report the same commit."""
        repo = tmp_path / "repo"
        sha = _init_repo(repo)
        sub = repo / "charms" / "foo"
        sub.mkdir(parents=True)
        assert head_sha(repo) == sha
        assert head_sha(sub) == sha

    def test_returns_none_outside_a_checkout(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        # `git rev-parse` walks upwards, so only assert the no-repo case when
        # tmp_path genuinely isn't inside one.
        if head_sha(tmp_path) is None:
            assert head_sha(plain) is None


class TestEnsureClone:
    def test_clones_when_absent(self, tmp_path: Path) -> None:
        origin = tmp_path / "origin"
        sha = _init_repo(origin)
        dest = ensure_clone(_ref(origin), tmp_path / "work")
        assert dest is not None
        assert head_sha(dest) == sha

    def test_refreshes_an_existing_clone(self, tmp_path: Path) -> None:
        """The scan workdir is cached across CI runs; a reused clone must
        still pick up commits landed since it was first cloned."""
        origin = tmp_path / "origin"
        _init_repo(origin)
        work = tmp_path / "work"
        first = ensure_clone(_ref(origin), work)
        assert first is not None

        new_sha = _commit(origin, "src.py", "import ops\n")

        second = ensure_clone(_ref(origin), work)
        assert second == first
        assert head_sha(second) == new_sha
        assert (second / "src.py").is_file()

    def test_returns_none_when_clone_fails(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist"
        assert ensure_clone(_ref(missing), tmp_path / "work") is None


class TestRefreshClone:
    def test_picks_up_a_newly_pinned_branch(self, tmp_path: Path) -> None:
        """A branch_overrides entry added after the first clone must take
        effect, not be ignored because the directory already exists."""
        origin = tmp_path / "origin"
        _init_repo(origin)
        work = tmp_path / "work"
        dest = ensure_clone(_ref(origin), work)
        assert dest is not None

        _git(["checkout", "-qb", "release"], origin)
        branch_sha = _commit(origin, "release.py", "# release\n")
        _git(["checkout", "-q", "main"], origin)

        assert refresh_clone(dest, _ref(origin, branch="release")) is True
        assert head_sha(dest) == branch_sha

    def test_failed_refresh_keeps_the_stale_checkout(self, tmp_path: Path) -> None:
        origin = tmp_path / "origin"
        _init_repo(origin)
        work = tmp_path / "work"
        dest = ensure_clone(_ref(origin), work)
        assert dest is not None
        stale = head_sha(dest)

        assert refresh_clone(dest, _ref(origin, branch="no-such-branch")) is False
        assert head_sha(dest) == stale
        assert (dest / "charmcraft.yaml").is_file()


class TestScanRecordsSha:
    def test_meta_carries_the_scanned_commit(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        sha = _init_repo(repo)
        meta = scan_charm(repo, [])["__meta__"]
        assert meta["repo_sha"] == sha

    def test_meta_sha_is_none_outside_a_checkout(self, tmp_path: Path) -> None:
        charm = tmp_path / "charm"
        charm.mkdir()
        (charm / "charmcraft.yaml").write_text("type: charm\nname: x\n")
        if head_sha(tmp_path) is None:
            assert scan_charm(charm, [])["__meta__"]["repo_sha"] is None
