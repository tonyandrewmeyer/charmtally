"""Calibration audit tool — branch-override-aware caching edition.

Usage:
    uv run python -m charmtally.tools.audit run <feature> <bucket>
    uv run python -m charmtally.tools.audit prune
    uv run python -m charmtally.tools.audit cache-info

The ``run`` subcommand samples records from ``scored.json`` for the given
feature / scoring bucket, clones each charm at the branch-override-aware ref
(honouring ``corpus-overrides.yaml`` branch_overrides), and emits a markdown
audit document with per-record evidence blocks and checkbox trays.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from ..corpus import load_overrides

_ENV_CACHE_DIR = "CHARMTALLY_CACHE_DIR"
_ENV_AUDIT_TS = "AUDIT_TS"
_PRUNE_AGE_DAYS = 30
_DEFAULT_N = 30

BUCKETS = frozenset({"clear-gap", "worth-considering", "present"})


def _default_cache_root() -> Path:
    env = os.environ.get(_ENV_CACHE_DIR)
    if env:
        return Path(env)
    return Path.home() / ".cache" / "charmtally" / "clones"


# ---------------------------------------------------------------------------
# CloneCache
# ---------------------------------------------------------------------------


class Cloner(Protocol):
    """Injectable clone callable — receives (repo_url, ref_or_None, dest_dir)."""

    def __call__(self, repo_url: str, ref: str | None, dest: Path) -> None: ...


class CloneCache:
    """Persistent, branch-override-aware, concurrent-safe clone cache.

    Cache root: ``~/.cache/charmtally/clones/`` (or ``$CHARMTALLY_CACHE_DIR``).
    Cache key:  ``sha256(repo_url + '::' + ref)[:16]``.
    Concurrency: ``fcntl.flock`` exclusive lock per entry directory.
    """

    def __init__(
        self,
        root: Path | None = None,
        *,
        force: bool = False,
    ) -> None:
        self.root = root if root is not None else _default_cache_root()
        self.force = force

    def _entry_dir(self, repo_url: str, ref: str) -> Path:
        key = hashlib.sha256(f"{repo_url}::{ref}".encode()).hexdigest()[:16]
        return self.root / key

    @contextmanager
    def _locked(self, entry_dir: Path) -> Iterator[None]:
        entry_dir.mkdir(parents=True, exist_ok=True)
        with (entry_dir / ".lock").open("w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def _head_sha(self, repo_dir: Path) -> str | None:
        try:
            r = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            return r.stdout.strip()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            return None

    def _remote_sha(self, repo_url: str, ref: str | None) -> str | None:
        """Ask the remote for the current tip SHA of ref without cloning."""
        cmd = ["git", "ls-remote", repo_url]
        if ref:
            cmd.append(ref)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
        except (subprocess.TimeoutExpired, OSError):
            return None
        if r.returncode != 0:
            return None
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            refname = parts[1]
            if not ref or refname in (f"refs/heads/{ref}", f"refs/tags/{ref}", "HEAD"):
                return parts[0]
        return None

    def _do_clone(self, repo_url: str, ref: str | None, dest: Path) -> None:
        if dest.exists():
            shutil.rmtree(dest)
        cmd = ["git", "clone", "--depth", "50"]
        if ref:
            cmd += ["--branch", ref]
        cmd += [repo_url, str(dest)]
        subprocess.run(cmd, check=True, capture_output=True, timeout=300)

    def checkout(
        self,
        repo_url: str,
        ref: str | None,
        *,
        cloner: Cloner | None = None,
        sha_resolver: Callable[[str, str | None], str | None] | None = None,
        head_sha_reader: Callable[[Path], str | None] | None = None,
    ) -> Path:
        """Return path to a cloned repo directory, using the cache when valid.

        Cache key is ``(repo_url, ref)``; two different refs for the same repo
        produce independent cache entries.  On a cache hit, the entry is
        validated by comparing the local HEAD SHA against the remote tip; if
        they differ the entry is re-cloned.

        ``cloner``, ``sha_resolver``, and ``head_sha_reader`` are injectable
        for testing (all default to the real implementations).
        """
        cache_ref = ref if ref is not None else "DEFAULT"
        entry = self._entry_dir(repo_url, cache_ref)
        clone_fn = cloner or self._do_clone
        resolve_fn = sha_resolver or self._remote_sha
        read_head = head_sha_reader or self._head_sha
        repo_dir = entry / "repo"

        with self._locked(entry):
            if self.force:
                clone_fn(repo_url, ref, repo_dir)
                entry.touch()
            elif repo_dir.exists():
                head = read_head(repo_dir)
                remote = resolve_fn(repo_url, ref)
                if remote is not None and head != remote:
                    clone_fn(repo_url, ref, repo_dir)
                    entry.touch()
                # else: valid hit — nothing to do
            else:
                clone_fn(repo_url, ref, repo_dir)
                entry.touch()

        return repo_dir

    def prune(self, max_age_days: int = _PRUNE_AGE_DAYS) -> int:
        """Remove cache entries older than ``max_age_days``. Returns count removed."""
        if not self.root.exists():
            return 0
        cutoff = time.time() - max_age_days * 86400
        removed = 0
        for entry in self.root.iterdir():
            if not entry.is_dir():
                continue
            if entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
        return removed

    def info(self) -> dict:
        """Return cache statistics as a dict."""
        if not self.root.exists():
            return {"entries": 0, "total_bytes": 0, "oldest": None, "newest": None}
        entries = [e for e in self.root.iterdir() if e.is_dir()]
        if not entries:
            return {"entries": 0, "total_bytes": 0, "oldest": None, "newest": None}
        mtimes = [e.stat().st_mtime for e in entries]
        total = sum(f.stat().st_size for e in entries for f in e.rglob("*") if f.is_file())

        def _fmt(ts: float) -> str:
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

        return {
            "entries": len(entries),
            "total_bytes": total,
            "oldest": _fmt(min(mtimes)),
            "newest": _fmt(max(mtimes)),
        }


# ---------------------------------------------------------------------------
# Sampling helpers
# ---------------------------------------------------------------------------


def _deterministic_seed(feature: str, bucket: str, suffix: str) -> int:
    raw = f"{bucket}::{feature}{suffix}"
    return int(hashlib.sha256(raw.encode()).hexdigest(), 16) % (2**32)


def _build_pool(
    scored: dict,
    feature: str,
    bucket: str,
    team: str | None,
) -> list[tuple[str, dict]]:
    """Return all (slug, record) pairs matching feature / bucket / team."""
    pool: list[tuple[str, dict]] = []
    for slug, rec in scored.items():
        if slug.startswith("__"):
            continue
        feat_rec = rec.get("features", {}).get(feature)
        if feat_rec is None:
            continue
        if feat_rec.get("score") != bucket:
            continue
        if team and rec.get("team", "").lower() != team.lower():
            continue
        pool.append((slug, rec))
    return pool


def _sample(
    pool: list[tuple[str, dict]],
    n: int,
    seed_suffix: str,
    feature: str,
    bucket: str,
) -> list[tuple[str, dict]]:
    """Deterministically sample up to n items from pool."""
    if len(pool) <= n:
        return list(pool)
    seed = _deterministic_seed(feature, bucket, seed_suffix)
    return random.Random(seed).sample(pool, n)


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _read_context(repo_dir: Path, evidence_file: str, line: int, context: int = 3) -> list[str]:
    """Return lines around evidence_file:line from the cloned repo."""
    try:
        text = (repo_dir / evidence_file).read_text(encoding="utf-8", errors="replace")
        all_lines = text.splitlines()
        start = max(0, line - 1 - context)
        end = min(len(all_lines), line + context)
        out = []
        for i, ln in enumerate(all_lines[start:end], start + 1):
            prefix = "→ " if i == line else "  "
            out.append(f"{prefix}{i:4d}  {ln}")
        return out
    except OSError:
        return []


def _render_record(
    slug: str,
    rec: dict,
    feature: str,
    ref: str | None,
    repo_dir: Path | None,
) -> str:
    """Render one audit record as a markdown section."""
    feat_rec = rec["features"][feature]
    evidence_list: list[dict] = feat_rec.get("evidence") or []
    team = rec.get("team", "") or "—"
    repo_url = rec.get("repo_url", "")
    score = feat_rec.get("score", "")
    rationale = feat_rec.get("rationale", "")

    parts = [
        f"## {slug}",
        "",
        f"- **Team:** {team}",
        f"- **Repo:** {repo_url}",
        f"- **Ref:** {ref or 'default'}",
        f"- **Score:** {score}",
        f"- **Rationale:** {rationale}",
    ]

    if evidence_list:
        parts.append("- **Evidence:**")
        for ev in evidence_list[:3]:
            ev_file = ev.get("file", "")
            ev_line = ev.get("line", 0)
            snippet = ev.get("snippet", "")
            parts.append(f"  - `{ev_file}:{ev_line}` — `{snippet}`")
            if repo_dir is not None:
                ctx = _read_context(repo_dir, ev_file, ev_line)
                if ctx:
                    parts.append("    ```python")
                    parts.extend(f"    {ln}" for ln in ctx)
                    parts.append("    ```")
    else:
        parts.append("- **Evidence:** none recorded")

    parts += [
        "",
        "```",
        "[ ] verified  [ ] FP  [ ] FN-candidate",
        "```",
        "",
        "---",
        "",
    ]
    return "\n".join(parts)


def _render_summary(
    feature: str,
    bucket: str,
    team: str | None,
    n: int,
    pool_size: int,
    records: list[tuple[str, dict]],
    record_blocks: list[str],
    ts: str,
) -> str:
    header = [
        f"# Audit: {feature} / {bucket}",
        "",
        f"**Generated:** {ts}",
        f"**Sample:** {len(records)} / {pool_size} records",
        f"**Team filter:** {team or 'all'}",
        f"**Requested n:** {n}",
        "",
        "---",
        "",
    ]
    return "\n".join(header) + "\n".join(record_blocks)


# ---------------------------------------------------------------------------
# CLI subcommands
# ---------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    scored_path: Path = args.scored
    if not scored_path.exists():
        print(f"error: {scored_path} not found", file=sys.stderr)
        return 1

    scored: dict = json.loads(scored_path.read_text())

    overrides_path: Path = args.overrides
    overrides = load_overrides(overrides_path) if overrides_path.exists() else None
    branch_overrides: dict[str, str] = overrides.branch_overrides if overrides else {}

    feature: str = args.feature
    bucket: str = args.bucket
    if bucket not in BUCKETS:
        print(f"error: bucket must be one of {sorted(BUCKETS)}", file=sys.stderr)
        return 1

    team: str | None = args.team
    n: int = args.n
    seed_suffix: str = args.seed_suffix

    pool = _build_pool(scored, feature, bucket, team)
    records = _sample(pool, n, seed_suffix, feature, bucket)

    cache = CloneCache(force=args.no_cache)

    ts_env = os.environ.get(_ENV_AUDIT_TS)
    ts = ts_env if ts_env else datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    record_blocks: list[str] = []
    for slug, rec in records:
        repo_url = rec.get("repo_url", "")
        ref = branch_overrides.get(repo_url)

        print(f"… {slug} ({repo_url})" + (f" [branch={ref}]" if ref else ""), file=sys.stderr)

        repo_dir: Path | None
        try:
            repo_dir = cache.checkout(repo_url, ref)
        except Exception as exc:  # noqa: BLE001
            print(f"  clone failed: {exc}", file=sys.stderr)
            repo_dir = None

        block = _render_record(slug, rec, feature, ref, repo_dir)
        record_blocks.append(block)
        print(block)

    safe_feature = re.sub(r"[^a-zA-Z0-9_-]", "-", feature)
    safe_bucket = re.sub(r"[^a-zA-Z0-9_-]", "-", bucket)
    out_name = f"audit-{safe_feature}-{safe_bucket}-{ts}.md"
    out_path = Path("tools") / out_name

    summary = _render_summary(feature, bucket, team, n, len(pool), records, record_blocks, ts)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(summary)
    print(f"\nwrote {out_path}", file=sys.stderr)
    return 0


def cmd_prune(args: argparse.Namespace) -> int:  # noqa: ARG001
    cache = CloneCache()
    removed = cache.prune()
    print(f"pruned {removed} cache entries older than {_PRUNE_AGE_DAYS} days")
    return 0


def cmd_cache_info(args: argparse.Namespace) -> int:  # noqa: ARG001
    cache = CloneCache()
    info = cache.info()
    print(f"Cache root:   {cache.root}")
    print(f"Entries:      {info['entries']}")
    print(f"Total size:   {info['total_bytes']:,} bytes")
    print(f"Oldest entry: {info['oldest'] or '—'}")
    print(f"Newest entry: {info['newest'] or '—'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m charmtally.tools.audit",
        description="Calibration audit tool for charmtally.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="Run an audit pass and emit markdown.")
    p_run.add_argument("feature", help="Feature name, e.g. ops.collect-status.")
    p_run.add_argument("bucket", help="Score bucket: clear-gap / worth-considering / present.")
    p_run.add_argument("--n", type=int, default=_DEFAULT_N, help=f"Sample size (default {_DEFAULT_N}).")
    p_run.add_argument("--team", default=None, help="Filter to this team (case-insensitive).")
    p_run.add_argument("--seed-suffix", default="", dest="seed_suffix", help="Appended to the seed string for reruns.")
    p_run.add_argument("--scored", type=Path, default=Path("./scored.json"), help="Path to scored.json.")
    p_run.add_argument(
        "--overrides",
        type=Path,
        default=Path("./corpus-overrides.yaml"),
        help="Path to corpus-overrides.yaml.",
    )
    p_run.add_argument("--no-cache", action="store_true", dest="no_cache", help="Force re-clone regardless of cache.")
    p_run.set_defaults(func=cmd_run)

    p_prune = sub.add_parser("prune", help=f"Remove cache entries older than {_PRUNE_AGE_DAYS} days.")
    p_prune.set_defaults(func=cmd_prune)

    p_info = sub.add_parser("cache-info", help="Print cache statistics.")
    p_info.set_defaults(func=cmd_cache_info)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
