"""Tests for charmtally.tools.audit."""

from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path

from ..tools.audit import (
    CloneCache,
    _build_pool,
    _deterministic_seed,
    _render_record,
    _render_summary,
    _sample,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_scored(charms: list[dict]) -> dict:
    """Build a minimal scored.json dict from a list of charm specs."""
    out: dict = {}
    for c in charms:
        slug = c["slug"]
        feature = c.get("feature", "ops.collect-status")
        out[slug] = {
            "name": c.get("name", slug),
            "team": c.get("team", ""),
            "repo_url": c.get("repo_url", f"https://github.com/example/{slug}"),
            "features": {
                feature: {
                    "present": c.get("present", False),
                    "evidence": c.get("evidence", []),
                    "score": c.get("score", "clear-gap"),
                    "rationale": c.get("rationale", ""),
                }
            },
        }
    return out


def _fake_clone(calls: list) -> object:
    """Return a cloner that records calls and creates the dest directory."""

    def _clone(repo_url: str, ref: str | None, dest: Path) -> None:
        calls.append((repo_url, ref))
        dest.mkdir(parents=True, exist_ok=True)

    return _clone


# ---------------------------------------------------------------------------
# Deterministic sampling
# ---------------------------------------------------------------------------


class TestDeterministicSampling:
    def test_same_inputs_same_picks(self) -> None:
        charms = [{"slug": f"charm-{i}", "score": "clear-gap"} for i in range(50)]
        scored = _make_scored(charms)
        pool = _build_pool(scored, "ops.collect-status", "clear-gap", None)
        s1 = _sample(pool, 10, "", "ops.collect-status", "clear-gap")
        s2 = _sample(pool, 10, "", "ops.collect-status", "clear-gap")
        assert [slug for slug, _ in s1] == [slug for slug, _ in s2]

    def test_different_seed_suffix_gives_different_picks(self) -> None:
        charms = [{"slug": f"charm-{i}", "score": "clear-gap"} for i in range(50)]
        scored = _make_scored(charms)
        pool = _build_pool(scored, "ops.collect-status", "clear-gap", None)
        s1 = _sample(pool, 10, "", "ops.collect-status", "clear-gap")
        s2 = _sample(pool, 10, "round2", "ops.collect-status", "clear-gap")
        assert [slug for slug, _ in s1] != [slug for slug, _ in s2]

    def test_pool_smaller_than_n_returns_full_pool(self) -> None:
        charms = [{"slug": f"charm-{i}", "score": "clear-gap"} for i in range(5)]
        scored = _make_scored(charms)
        pool = _build_pool(scored, "ops.collect-status", "clear-gap", None)
        result = _sample(pool, 30, "", "ops.collect-status", "clear-gap")
        assert len(result) == 5

    def test_team_filter_applied(self) -> None:
        charms = [
            {"slug": "charm-obs", "score": "clear-gap", "team": "Observability"},
            {"slug": "charm-data", "score": "clear-gap", "team": "Data"},
        ]
        scored = _make_scored(charms)
        pool = _build_pool(scored, "ops.collect-status", "clear-gap", "observability")
        assert len(pool) == 1
        assert pool[0][0] == "charm-obs"

    def test_team_filter_is_case_insensitive(self) -> None:
        charms = [{"slug": "charm-a", "score": "clear-gap", "team": "Charm-Tech"}]
        scored = _make_scored(charms)
        assert len(_build_pool(scored, "ops.collect-status", "clear-gap", "charm-tech")) == 1
        assert len(_build_pool(scored, "ops.collect-status", "clear-gap", "CHARM-TECH")) == 1

    def test_bucket_filter_applied(self) -> None:
        charms = [
            {"slug": "charm-gap", "score": "clear-gap"},
            {"slug": "charm-worth", "score": "worth-considering"},
            {"slug": "charm-present", "score": "present"},
        ]
        scored = _make_scored(charms)
        pool = _build_pool(scored, "ops.collect-status", "worth-considering", None)
        assert len(pool) == 1
        assert pool[0][0] == "charm-worth"

    def test_skipped_meta_keys_excluded(self) -> None:
        scored = _make_scored([{"slug": "real-charm", "score": "clear-gap"}])
        scored["__skipped__"] = {"some-charm": "clone failed"}
        pool = _build_pool(scored, "ops.collect-status", "clear-gap", None)
        assert all(slug == "real-charm" for slug, _ in pool)

    def test_seed_is_deterministic_across_calls(self) -> None:
        s1 = _deterministic_seed("ops.collect-status", "clear-gap", "")
        s2 = _deterministic_seed("ops.collect-status", "clear-gap", "")
        assert s1 == s2

    def test_different_features_different_seeds(self) -> None:
        s1 = _deterministic_seed("ops.collect-status", "clear-gap", "")
        s2 = _deterministic_seed("python.pydantic", "clear-gap", "")
        assert s1 != s2


# ---------------------------------------------------------------------------
# CloneCache — cache key / hit / miss / stale
# ---------------------------------------------------------------------------


class TestCloneCacheKey:
    def test_different_refs_different_cache_dirs(self, tmp_path: Path) -> None:
        cache = CloneCache(root=tmp_path / "cache")
        url = "https://github.com/canonical/mysql-k8s-operator"
        d_main = cache._entry_dir(url, "main")
        d_readme = cache._entry_dir(url, "readme")
        assert d_main != d_readme

    def test_same_ref_same_cache_dir(self, tmp_path: Path) -> None:
        cache = CloneCache(root=tmp_path / "cache")
        url = "https://github.com/canonical/mysql-k8s-operator"
        assert cache._entry_dir(url, "main") == cache._entry_dir(url, "main")

    def test_none_ref_uses_default_key(self, tmp_path: Path) -> None:
        cache = CloneCache(root=tmp_path / "cache")
        url = "https://github.com/example/repo"
        # checkout(url, None) must use "DEFAULT" as the cache_ref
        import hashlib

        expected_key = hashlib.sha256(f"{url}::DEFAULT".encode()).hexdigest()[:16]
        assert cache._entry_dir(url, "DEFAULT").name == expected_key

    def test_different_urls_different_cache_dirs(self, tmp_path: Path) -> None:
        cache = CloneCache(root=tmp_path / "cache")
        d1 = cache._entry_dir("https://github.com/canonical/foo", "main")
        d2 = cache._entry_dir("https://github.com/canonical/bar", "main")
        assert d1 != d2


class TestCloneCacheMiss:
    def test_missing_entry_triggers_clone(self, tmp_path: Path) -> None:
        calls: list = []
        cache = CloneCache(root=tmp_path / "cache")
        cache.checkout(
            "https://github.com/example/repo",
            "main",
            cloner=_fake_clone(calls),
            sha_resolver=lambda u, r: None,
            head_sha_reader=lambda p: None,
        )
        assert len(calls) == 1
        assert calls[0] == ("https://github.com/example/repo", "main")

    def test_returns_repo_subdir(self, tmp_path: Path) -> None:
        calls: list = []
        cache = CloneCache(root=tmp_path / "cache")
        path = cache.checkout(
            "https://github.com/example/repo",
            "main",
            cloner=_fake_clone(calls),
            sha_resolver=lambda u, r: None,
            head_sha_reader=lambda p: None,
        )
        assert path.name == "repo"
        assert path.exists()


class TestCloneCacheHit:
    def test_valid_hit_does_not_reclone(self, tmp_path: Path) -> None:
        calls: list = []
        clone_fn = _fake_clone(calls)
        cache = CloneCache(root=tmp_path / "cache")
        url = "https://github.com/example/repo"

        # First call — populates cache
        cache.checkout(
            url,
            "main",
            cloner=clone_fn,
            sha_resolver=lambda u, r: "abc123",
            head_sha_reader=lambda p: "abc123",
        )
        # Second call — SHA matches → no re-clone
        cache.checkout(
            url,
            "main",
            cloner=clone_fn,
            sha_resolver=lambda u, r: "abc123",
            head_sha_reader=lambda p: "abc123",
        )
        assert len(calls) == 1

    def test_stale_sha_triggers_reclone(self, tmp_path: Path) -> None:
        calls: list = []
        clone_fn = _fake_clone(calls)
        cache = CloneCache(root=tmp_path / "cache")
        url = "https://github.com/example/repo"

        # First call — populates cache
        cache.checkout(
            url,
            "main",
            cloner=clone_fn,
            sha_resolver=lambda u, r: "abc123",
            head_sha_reader=lambda p: "abc123",
        )
        # Second call — remote moved on → must re-clone
        cache.checkout(
            url,
            "main",
            cloner=clone_fn,
            sha_resolver=lambda u, r: "new_sha",
            head_sha_reader=lambda p: "abc123",
        )
        assert len(calls) == 2

    def test_unresolvable_remote_sha_keeps_cache(self, tmp_path: Path) -> None:
        calls: list = []
        clone_fn = _fake_clone(calls)
        cache = CloneCache(root=tmp_path / "cache")
        url = "https://github.com/example/repo"

        cache.checkout(url, "main", cloner=clone_fn, sha_resolver=lambda u, r: None, head_sha_reader=lambda p: None)
        cache.checkout(url, "main", cloner=clone_fn, sha_resolver=lambda u, r: None, head_sha_reader=lambda p: None)
        # When remote SHA can't be resolved, we trust the cache
        assert len(calls) == 1

    def test_force_always_reclones(self, tmp_path: Path) -> None:
        calls: list = []
        clone_fn = _fake_clone(calls)
        cache = CloneCache(root=tmp_path / "cache", force=True)
        url = "https://github.com/example/repo"

        cache.checkout(url, "main", cloner=clone_fn, sha_resolver=lambda u, r: "abc", head_sha_reader=lambda p: "abc")
        cache.checkout(url, "main", cloner=clone_fn, sha_resolver=lambda u, r: "abc", head_sha_reader=lambda p: "abc")
        assert len(calls) == 2

    def test_branch_override_aware_separate_entries(self, tmp_path: Path) -> None:
        """mysql-k8s-operator scenario: readme vs main produce independent entries."""
        calls: list = []
        clone_fn = _fake_clone(calls)
        cache = CloneCache(root=tmp_path / "cache")
        url = "https://github.com/canonical/mysql-k8s-operator"

        path_readme = cache.checkout(
            url, "readme", cloner=clone_fn, sha_resolver=lambda u, r: None, head_sha_reader=lambda p: None
        )
        path_main = cache.checkout(
            url, "main", cloner=clone_fn, sha_resolver=lambda u, r: None, head_sha_reader=lambda p: None
        )
        assert len(calls) == 2
        assert path_readme != path_main


# ---------------------------------------------------------------------------
# CloneCache — prune
# ---------------------------------------------------------------------------


class TestCachePrune:
    def test_prune_removes_old_entries(self, tmp_path: Path) -> None:
        cache = CloneCache(root=tmp_path)
        old = tmp_path / "old_entry"
        fresh = tmp_path / "fresh_entry"
        old.mkdir()
        fresh.mkdir()

        old_ts = time.time() - 31 * 86400
        os.utime(old, (old_ts, old_ts))

        removed = cache.prune(max_age_days=30)
        assert removed == 1
        assert not old.exists()
        assert fresh.exists()

    def test_prune_keeps_recent_entries(self, tmp_path: Path) -> None:
        cache = CloneCache(root=tmp_path)
        entry = tmp_path / "recent"
        entry.mkdir()
        removed = cache.prune(max_age_days=30)
        assert removed == 0
        assert entry.exists()

    def test_prune_nonexistent_root_returns_zero(self, tmp_path: Path) -> None:
        cache = CloneCache(root=tmp_path / "no-such-dir")
        assert cache.prune() == 0

    def test_prune_all_old(self, tmp_path: Path) -> None:
        cache = CloneCache(root=tmp_path)
        for name in ("a", "b", "c"):
            d = tmp_path / name
            d.mkdir()
            old_ts = time.time() - 40 * 86400
            os.utime(d, (old_ts, old_ts))
        assert cache.prune(max_age_days=30) == 3
        assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# CloneCache — lockfile concurrent-call safety (smoke test)
# ---------------------------------------------------------------------------


class TestLockfileSafety:
    def test_concurrent_checkouts_serialize_without_deadlock(self, tmp_path: Path) -> None:
        """Two threads targeting the same cache entry must not deadlock or corrupt."""
        call_count = 0
        count_lock = threading.Lock()

        def fake_clone(repo_url: str, ref: str | None, dest: Path) -> None:
            nonlocal call_count
            with count_lock:
                call_count += 1
            time.sleep(0.02)
            dest.mkdir(parents=True, exist_ok=True)

        cache = CloneCache(root=tmp_path / "cache")
        url = "https://github.com/example/concurrent-repo"
        errors: list[Exception] = []

        def run() -> None:
            try:
                cache.checkout(
                    url,
                    "main",
                    cloner=fake_clone,
                    sha_resolver=lambda u, r: None,
                    head_sha_reader=lambda p: None,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        t1 = threading.Thread(target=run)
        t2 = threading.Thread(target=run)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert not t1.is_alive(), "thread 1 timed out"
        assert not t2.is_alive(), "thread 2 timed out"
        assert not errors
        # Exactly one clone: the second thread sees the populated repo dir
        assert call_count == 1


# ---------------------------------------------------------------------------
# Markdown emitter shape
# ---------------------------------------------------------------------------


class TestMarkdownEmitter:
    def _minimal_rec(self, slug: str, score: str = "clear-gap", evidence: list | None = None) -> dict:
        return {
            "name": slug,
            "team": "Data",
            "repo_url": f"https://github.com/canonical/{slug}",
            "features": {
                "ops.collect-status": {
                    "present": False,
                    "evidence": evidence or [],
                    "score": score,
                    "rationale": "charm sets status directly",
                }
            },
        }

    def test_record_has_h2_slug_anchor(self) -> None:
        block = _render_record("my-charm", self._minimal_rec("my-charm"), "ops.collect-status", "main", None)
        assert re.search(r"^## my-charm$", block, re.MULTILINE)

    def test_record_contains_score(self) -> None:
        block = _render_record("my-charm", self._minimal_rec("my-charm"), "ops.collect-status", None, None)
        assert "clear-gap" in block

    def test_record_contains_rationale(self) -> None:
        block = _render_record("my-charm", self._minimal_rec("my-charm"), "ops.collect-status", None, None)
        assert "sets status directly" in block

    def test_record_has_checkbox_tray(self) -> None:
        block = _render_record("my-charm", self._minimal_rec("my-charm"), "ops.collect-status", None, None)
        assert "[ ] verified" in block
        assert "[ ] FP" in block
        assert "[ ] FN-candidate" in block

    def test_record_shows_evidence_file_and_line(self) -> None:
        evidence = [{"file": "src/charm.py", "line": 42, "snippet": ".unit.status =", "detector_kind": "regex"}]
        block = _render_record(
            "my-charm", self._minimal_rec("my-charm", evidence=evidence), "ops.collect-status", None, None
        )
        assert "src/charm.py:42" in block
        assert ".unit.status =" in block

    def test_record_shows_ref(self) -> None:
        block = _render_record("my-charm", self._minimal_rec("my-charm"), "ops.collect-status", "main", None)
        assert "main" in block

    def test_summary_has_h1_anchor(self) -> None:
        records = [("charm-a", self._minimal_rec("charm-a"))]
        blocks = [_render_record(s, r, "ops.collect-status", None, None) for s, r in records]
        summary = _render_summary("ops.collect-status", "clear-gap", None, 30, 1, records, blocks, "20260615T120000Z")
        assert re.search(r"^# Audit: ops\.collect-status / clear-gap$", summary, re.MULTILINE)

    def test_summary_contains_timestamp(self) -> None:
        records = [("charm-a", self._minimal_rec("charm-a"))]
        blocks = [_render_record(s, r, "ops.collect-status", None, None) for s, r in records]
        summary = _render_summary("ops.collect-status", "clear-gap", None, 30, 1, records, blocks, "20260615T120000Z")
        assert "20260615T120000Z" in summary

    def test_summary_contains_record_sections(self) -> None:
        records = [("charm-a", self._minimal_rec("charm-a")), ("charm-b", self._minimal_rec("charm-b"))]
        blocks = [_render_record(s, r, "ops.collect-status", None, None) for s, r in records]
        summary = _render_summary("ops.collect-status", "clear-gap", None, 30, 2, records, blocks, "20260615T120000Z")
        assert "## charm-a" in summary
        assert "## charm-b" in summary

    def test_summary_shows_team_filter(self) -> None:
        records: list = []
        summary = _render_summary(
            "ops.collect-status", "clear-gap", "observability", 30, 0, records, [], "20260615T120000Z"
        )
        assert "observability" in summary

    def test_summary_shows_all_when_no_team_filter(self) -> None:
        records: list = []
        summary = _render_summary("ops.collect-status", "clear-gap", None, 30, 0, records, [], "20260615T120000Z")
        assert "all" in summary

    def test_evidence_context_from_repo_dir(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        charm_py = src / "charm.py"
        charm_py.write_text("\n".join(f"line {i}" for i in range(1, 20)))
        evidence = [{"file": "src/charm.py", "line": 5, "snippet": "line 5", "detector_kind": "regex"}]
        block = _render_record(
            "my-charm", self._minimal_rec("my-charm", evidence=evidence), "ops.collect-status", None, tmp_path
        )
        assert "line 5" in block
        # Context window (±3 lines) should appear
        assert "```python" in block
