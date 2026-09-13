"""Assert the architecture detectors still agree with the calibration ledger.

Usage:
    uv run python -m charmtally.tools.calibration_check
    uv run python -m charmtally.tools.calibration_check --strict
    uv run python -m charmtally.tools.calibration_check --format json

Not part of the pipeline. `calibration-ledger.yaml` transcribes 48 rounds of
hand adjudication out of `CALIBRATION.md`; this reads it back against the
committed scan output and fails when a detector no longer says what the
adjudicator saw. Without it, a detector change that silently re-breaks a charm
an earlier round fixed ships, and is only noticed the next time somebody reads
the prose.

Scope: the two architecture buckets, `reconcile` and `delta`. That is where the
ledger has enough rows (149 and 92) for a failure to mean something. The next
bucket down is 44 rows and the rest are in the teens or single digits, thin
enough that a failure would be as likely to mean the extraction missed a row as
that a detector changed — see LEDGER-EXTRACTION.md, "What was deliberately not
done".

What it runs against
--------------------
The committed `results.json`, not a fresh scan. Re-cloning ~344 charm repos per
CI run is not viable, and it turns out not to be needed: `results.json` carries
`features.__meta__.architecture` per charm, which *is* the detectors' raw
output for these two buckets, and 236 of the ledger's 241 (slug, bucket) rows
in scope resolve to a charm in it. The five that do not are corpus departures,
reported as skips rather than failures.

Bucket membership
-----------------
Membership is read off the raw `architecture` list, NOT off `dashboard`'s
`_primary_arch`. The two disagree, and the raw list is the one the ledger
adjudicated: `_primary_arch` picks a single label per charm by priority, so
folding `paas_charm` into `component-graph` (CALIBRATION #42) moved charms like
`github-profiles-automator` out of the displayed `reconcile` bucket while the
`reconcile` detector went on matching them exactly as before. Comparing against
the displayed label reports three such charms as regressions when no detector
changed. `delta` has no detector of its own — it is the residual, "no
architecture pattern matched" — so membership there is an empty list.

That reading also reproduces LEDGER-EXTRACTION.md's live-bucket figure: it
leaves exactly 13 `reconcile` rows recorded FP and still matching, and the
document's census is 120 TP / 133, i.e. 13 surviving false positives.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

#: The buckets this check covers. See the module docstring for why it stops here.
BUCKETS = ("reconcile", "delta")

#: Every architecture pattern `features.yaml` can emit. `delta` is not among
#: them: it is the residual, so a charm is in the `delta` bucket when this list
#: comes back empty. Kept as a constant so a *new* pattern landing in
#: `features.yaml` without being added here is caught by a test rather than
#: silently widening the residual and clearing half the `delta` rows.
ARCH_PATTERNS = (
    "reconcile-all",
    "reconcile",
    "part-reconcile",
    "unconditional-init",
    "component-graph",
)

DEFAULT_LEDGER = Path("./calibration-ledger.yaml")
DEFAULT_RESULTS = Path("./results.json")
DEFAULT_EXCEPTIONS = Path("./calibration-exceptions.yaml")

# Outcome classes. The first three decide the exit code; the rest are reported
# so that a run says what it did not look at as well as what it did.
AGREE = "agree"
REGRESSION = "regression"
UNFIXED_FP = "unfixed-fp"
ACCEPTED = "accepted"
SKIPPED_VERDICT = "skipped-verdict"
SKIPPED_INHERITED = "skipped-inherited"
SKIPPED_ABSENT = "skipped-absent"

_FAILING = (REGRESSION, UNFIXED_FP)


@dataclass(frozen=True)
class Outcome:
    """One ledger row, evaluated against the current detector output."""

    slug: str
    bucket: str
    kind: str
    verdict: str | None = None
    round: Any = None
    date: str | None = None
    reason: str | None = None
    source_line: int | None = None
    verdict_as_written: str | None = None
    in_bucket: bool | None = None
    architecture: list[str] | None = None
    repo_sha: str | None = None
    note: str | None = None
    accepted_by: dict[str, Any] | None = None


@dataclass
class Report:
    """The result of one run: per-row outcomes plus exception bookkeeping."""

    outcomes: list[Outcome] = field(default_factory=list)
    stale_exceptions: list[dict[str, Any]] = field(default_factory=list)
    invalid_exceptions: list[tuple[dict[str, Any], str]] = field(default_factory=list)

    def of_kind(self, *kinds: str) -> list[Outcome]:
        """Every outcome whose kind is one of `kinds`, in ledger order."""
        return [o for o in self.outcomes if o.kind in kinds]

    def failures(self) -> list[Outcome]:
        """Divergences that nothing has accepted."""
        return self.of_kind(*_FAILING)

    def ok(self, *, strict: bool = False) -> bool:
        """Whether this run passes. `strict` also rejects stale exceptions."""
        if self.failures() or self.invalid_exceptions:
            return False
        return not (strict and self.stale_exceptions)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_yaml(path: Path) -> dict:
    """Load a YAML mapping from `path`, or return {} if it does not exist."""
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_results(path: Path) -> dict:
    """Load a scan-output JSON file."""
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def ledger_rows(ledger: dict, buckets: Iterable[str] = BUCKETS) -> Iterator[dict]:
    """Yield the ledger records that fall in `buckets`, in file order."""
    wanted = set(buckets)
    for record in ledger.get("records") or []:
        if record.get("bucket") in wanted:
            yield record


# ---------------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------------


def in_bucket(bucket: str, meta: dict) -> bool:
    """Whether the detectors currently place this charm in `bucket`.

    Read off `__meta__.architecture`, the detectors' own output. `delta` is the
    residual rather than a pattern, so it is "nothing matched". See the module
    docstring for why this is not `dashboard._primary_arch`.
    """
    archs = meta.get("architecture") or []
    if bucket == "delta":
        return not archs
    return bucket in archs


def evaluate_row(record: dict, results: dict) -> Outcome:
    """Classify one ledger record against the current scan output."""
    slug = record["slug"]
    bucket = record["bucket"]
    common = {
        "slug": slug,
        "bucket": bucket,
        "verdict": record.get("verdict"),
        "round": record.get("round"),
        "date": record.get("date"),
        "reason": record.get("reason"),
        "source_line": record.get("source_line"),
        "verdict_as_written": record.get("verdict_as_written"),
    }

    # `history` holds the superseded events; the top-level fields are the
    # current verdict, so the current verdict is all that is checked.
    verdict = record.get("verdict")
    if verdict not in ("TP", "FP"):
        # NA, `other` and `unadjudicated` are not mechanically checkable.
        # `other` in particular covers round-specific labels that were
        # deliberately not folded into TP or FP (LEDGER-EXTRACTION.md), and
        # folding them here would be re-adjudicating by the back door.
        return Outcome(kind=SKIPPED_VERDICT, note=f"verdict {verdict!r}", **common)

    # An inherited-twin verdict was never independently formed for this slug:
    # #28's Launchpad/GitHub mirrors and #29's `secops-fork-opencti-operator`
    # copy a verdict across from a byte-identical tree. Asserting it adds no
    # evidence the twin's own row does not already carry, and it breaks for a
    # reason that has nothing to do with a detector when the two trees drift.
    #
    # Note this is deliberately NOT a `counts_toward_precision: false` filter,
    # which is the wider flag it sits under. That flag governs precision
    # arithmetic — do not double-count a re-confirmation, do not count an
    # inherited twin, do not count a shape-sweep hit — and this check is not
    # computing precision. Honouring it here would drop two genuine verdicts
    # for no gain: `superset-k8s-operator` (a real TP, flagged only because
    # #47 re-confirmed a verdict round 31 had already counted — and the
    # worked example the whole ledger exists for) and `postgresql-test-app`
    # ("Kept as a genuine TP", flagged only because it arrived through a shape
    # sweep). Every *other* row the flag excludes in these two buckets already
    # falls out above as `unadjudicated`, so the flag would decide nothing
    # except to discard those two.
    if record.get("inherited_from"):
        return Outcome(
            kind=SKIPPED_INHERITED,
            note=f"verdict inherited from {record['inherited_from']}",
            **common,
        )

    charm = results.get(slug)
    if charm is None:
        skipped = (results.get("__skipped__") or {}).get(slug)
        note = f"not in the scan output ({skipped})" if skipped else "not in the scan output"
        return Outcome(kind=SKIPPED_ABSENT, note=note, **common)

    meta = charm.get("features", {}).get("__meta__") or {}
    present = in_bucket(bucket, meta)
    detail = {
        "in_bucket": present,
        "architecture": list(meta.get("architecture") or []),
        "repo_sha": meta.get("repo_sha"),
    }

    if present == (verdict == "TP"):
        return Outcome(kind=AGREE, **common, **detail)
    kind = REGRESSION if verdict == "TP" else UNFIXED_FP
    return Outcome(kind=kind, **common, **detail)


def _exception_key(entry: dict) -> tuple[str, str]:
    return (entry.get("slug", ""), entry.get("bucket", ""))


def evaluate(results: dict, ledger: dict, exceptions: dict | None = None) -> Report:
    """Evaluate every in-scope ledger row and apply the accepted divergences."""
    entries = list((exceptions or {}).get("exceptions") or [])
    by_key = {_exception_key(e): e for e in entries}
    report = Report()
    matched: set[tuple[str, str]] = set()

    for record in ledger_rows(ledger):
        outcome = evaluate_row(record, results)
        if outcome.kind in _FAILING:
            key = (outcome.slug, outcome.bucket)
            entry = by_key.get(key)
            if entry is not None:
                matched.add(key)
                # An exception names the verdict it was written against, so
                # editing the ledger row — the other way to accept a change —
                # invalidates it instead of silently carrying it over.
                if entry.get("ledger_verdict") != outcome.verdict:
                    report.invalid_exceptions.append((
                        entry,
                        f"ledger_verdict is {entry.get('ledger_verdict')!r} but the ledger "
                        f"now records {outcome.verdict!r}",
                    ))
                else:
                    outcome = replace(outcome, kind=ACCEPTED, accepted_by=entry)
        report.outcomes.append(outcome)

    in_scope = {(r["slug"], r["bucket"]) for r in ledger_rows(ledger)}
    for entry in entries:
        key = _exception_key(entry)
        if key not in in_scope:
            report.invalid_exceptions.append((entry, "no such (slug, bucket) row in the ledger"))
        elif key not in matched:
            report.stale_exceptions.append(entry)
    return report


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

_ACCEPT_HELP = """\
To accept one of these, pick whichever of the three it actually is:

  * the detector regressed        -> fix the detector; that is what this
                                     check is for.
  * the ledger row is now stale   -> update the row in calibration-ledger.yaml:
                                     move the current top-level event into its
                                     `history` list and write the new verdict,
                                     `round`, `date`, `reason` and
                                     `source_line` in its place. Any exception
                                     naming the old verdict then fails as
                                     invalid rather than carrying over.
  * the charm changed upstream,
    or a later round moved it
    between buckets               -> add an entry to calibration-exceptions.yaml
                                     with `slug`, `bucket`, `ledger_verdict`,
                                     `accepted` (a date), `reason` and a
                                     `source` citation.

Do not delete ledger rows to make this pass. The ledger is a transcription of
CALIBRATION.md; a row that is wrong about what the prose says is a
transcription bug and should be corrected against the prose, with the change
noted in the commit."""


def _fmt_round(outcome: Outcome) -> str:
    bits = [f"round {outcome.round}"]
    if outcome.date:
        bits.append(str(outcome.date))
    bits.append(f"CALIBRATION.md:{outcome.source_line}")
    return ", ".join(bits)


def _fmt_now(outcome: Outcome) -> str:
    archs = outcome.architecture or []
    shown = ", ".join(archs) if archs else "no pattern matched"
    verb = "still in" if outcome.in_bucket else "not in"
    return f"{verb} `{outcome.bucket}` — architecture: [{shown}]"


def render_outcome(outcome: Outcome) -> str:
    """Render one divergence in enough detail to triage it without the prose."""
    written = outcome.verdict_as_written
    as_written = f' (written "{written}")' if written and written != outcome.verdict else ""
    lines = [
        f"{outcome.kind.upper()}  {outcome.slug}  [{outcome.bucket}]",
        f"    ledger: {outcome.verdict}{as_written} — {_fmt_round(outcome)}",
        f"            {outcome.reason}",
        f"    now:    {_fmt_now(outcome)}",
        f"            scanned at repo_sha {outcome.repo_sha}",
    ]
    if outcome.accepted_by:
        entry = outcome.accepted_by
        lines.append(f"    accepted {entry.get('accepted')}: {entry.get('reason')}")
        if entry.get("source"):
            lines.append(f"            source: {entry['source']}")
    return "\n".join(lines)


def render(report: Report, *, strict: bool = False) -> str:
    """Render the whole run as text."""
    out: list[str] = ["Calibration regression check — buckets: " + ", ".join(BUCKETS), ""]
    counts = {
        "agree": len(report.of_kind(AGREE)),
        "regressions": len(report.of_kind(REGRESSION)),
        "unfixed FPs": len(report.of_kind(UNFIXED_FP)),
        "accepted divergences": len(report.of_kind(ACCEPTED)),
        "skipped (not TP/FP)": len(report.of_kind(SKIPPED_VERDICT)),
        "skipped (inherited verdict)": len(report.of_kind(SKIPPED_INHERITED)),
        "skipped (charm not scanned)": len(report.of_kind(SKIPPED_ABSENT)),
    }
    width = max(len(k) for k in counts)
    for key, value in counts.items():
        out.append(f"  {key.rjust(width)}: {value}")
    out.append(f"  {'total rows'.rjust(width)}: {len(report.outcomes)}")
    out.append("")

    # Skips are listed, not just counted: a row silently dropping out of the
    # checked set is the failure mode this whole file exists to prevent.
    for kind, heading in (
        (SKIPPED_ABSENT, "Skipped — charm not in the scan output"),
        (SKIPPED_INHERITED, "Skipped — verdict inherited from a twin, never read for this slug"),
    ):
        rows = report.of_kind(kind)
        if rows:
            out.append(f"{heading} ({len(rows)}):")
            out += [f"  {o.slug} [{o.bucket}] — {o.note}" for o in rows]
            out.append("")

    skipped_verdicts = report.of_kind(SKIPPED_VERDICT)
    if skipped_verdicts:
        by_verdict: dict[str, int] = {}
        for o in skipped_verdicts:
            by_verdict[str(o.verdict)] = by_verdict.get(str(o.verdict), 0) + 1
        tally = ", ".join(f"{v}: {n}" for v, n in sorted(by_verdict.items()))
        out += [
            f"Skipped — not mechanically checkable ({len(skipped_verdicts)}): {tally}.",
            "  Only TP and FP state something a detector can be held to; `other` covers",
            "  round-specific labels that were deliberately not folded into either.",
            "",
        ]

    accepted = report.of_kind(ACCEPTED)
    if accepted:
        out.append(f"Accepted divergences ({len(accepted)}) — calibration-exceptions.yaml:")
        out += [render_outcome(o) + "\n" for o in accepted]

    if report.stale_exceptions:
        out.append(f"STALE exceptions ({len(report.stale_exceptions)}) — the ledger and the")
        out.append("detectors now agree, so these entries no longer accept anything.")
        out.append("Delete them from calibration-exceptions.yaml.")
        out += [
            f"  {e.get('slug')} [{e.get('bucket')}] — accepted {e.get('accepted')}"
            for e in report.stale_exceptions
        ]
        out.append("  (not a failure unless --strict)" if not strict else "  (--strict: failing)")
        out.append("")

    if report.invalid_exceptions:
        out.append(f"INVALID exceptions ({len(report.invalid_exceptions)}):")
        out += [
            f"  {e.get('slug')} [{e.get('bucket')}] — {why}"
            for e, why in report.invalid_exceptions
        ]
        out.append("")

    failures = report.failures()
    if failures:
        out.append(f"FAILURES ({len(failures)}):")
        out.append("")
        out += [render_outcome(o) + "\n" for o in failures]
        out.append(_ACCEPT_HELP)
        out.append("")

    out.append("PASS" if report.ok(strict=strict) else "FAIL")
    return "\n".join(out)


def to_json(report: Report) -> dict:
    """Render the same run as JSON, for anything that wants to post-process it."""
    return {
        "buckets": list(BUCKETS),
        "outcomes": [
            {k: v for k, v in o.__dict__.items() if v is not None} for o in report.outcomes
        ],
        "stale_exceptions": report.stale_exceptions,
        "invalid_exceptions": [{"entry": e, "why": w} for e, w in report.invalid_exceptions],
        "ok": report.ok(),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns 0 when the detectors still agree with the ledger."""
    p = argparse.ArgumentParser(
        prog="python -m charmtally.tools.calibration_check",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER, help="Path to the ledger.")
    p.add_argument(
        "--results", type=Path, default=DEFAULT_RESULTS, help="Path to the scan output."
    )
    p.add_argument(
        "--exceptions",
        type=Path,
        default=DEFAULT_EXCEPTIONS,
        help="Path to the accepted-divergence list.",
    )
    p.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Also fail on stale exceptions. Off by default: an exception going stale is good "
            "news (a divergence cleared), and the weekly scan rewrites results.json, so an "
            "upstream charm change could otherwise fail CI on a PR that touched nothing."
        ),
    )
    p.add_argument("--format", choices=("text", "json"), default="text", help="Output format.")
    args = p.parse_args(argv)

    for path in (args.ledger, args.results):
        if not path.exists():
            print(f"no such file: {path}", file=sys.stderr)
            return 2

    report = evaluate(
        load_results(args.results), load_yaml(args.ledger), load_yaml(args.exceptions)
    )
    if args.format == "json":
        json.dump(to_json(report), sys.stdout, indent=1)
        sys.stdout.write("\n")
    else:
        print(render(report, strict=args.strict))
    return 0 if report.ok(strict=args.strict) else 1


if __name__ == "__main__":
    sys.exit(main())
