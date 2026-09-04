"""Detector dispatch: one registry per group, and the package's entry point.

`detect_feature` walks a feature's detectors exactly once. A file-independent
kind runs there and then; a per-file kind is *prepared* into a runner — a
closure holding whatever that kind wants precompiled — and every runner is
then called once per file in scope.

Preparation is what keeps the per-file loop free of per-kind bookkeeping.
`observe-event` compiles its patterns, `regex` compiles its pattern, and
`import` decides whether the charm is its own library's provider, all once
per feature rather than once per file; a factory that returns None disables
its detector for this charm. Adding a kind is one entry in one registry.
"""

from __future__ import annotations

import ast
import re
from typing import TYPE_CHECKING

from . import _ast as _ast_kinds
from ._config import (
    _detect_pytest_config_key,
    _detect_relation_count,
    _detect_requires_interface,
    _detect_yaml_key,
)
from ._files import CharmSource, Evidence, _charm_provides_lib

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from ..catalogue import Detectable
    from ._files import SourceFile

    # A prepared per-file detector, and the factory that prepares one. A
    # factory returning None means "this detector does not apply to this
    # charm at all", which saves the runner loop the check.
    _Runner = Callable[[SourceFile], Iterator[Evidence]]
    _Factory = Callable[[str, dict, CharmSource], _Runner | None]


# Evidence snippets are shown in the dashboard's hover text, not read as code.
_SNIPPET = 120


def _lineno(text: str, offset: int) -> int:
    """Return the 1-indexed line number containing character `offset` of `text`."""
    return text.count("\n", 0, offset) + 1


# ── text-matching kinds ─────────────────────────────────────────────────────────


_CAMEL_RE = re.compile(r"(?<!^)(?=[A-Z])")


def _event_to_on_attr(event_class: str) -> str:
    name = event_class.removesuffix("Event")
    return _CAMEL_RE.sub("_", name).lower()


def _observe_patterns(events: list[str]) -> list[re.Pattern[str]]:
    pats: list[re.Pattern[str]] = []
    for ev in events:
        attr = re.escape(_event_to_on_attr(ev))
        pats.append(re.compile(rf"observe\s*\([^)]*\bon\.{attr}\b"))
        pats.append(re.compile(rf"observe\s*\([^)]*\bon\[\s*['\"]{attr}['\"]\s*\]"))
    return pats


def _prepare_observe_event(kind: str, config: dict, source: CharmSource) -> _Runner:
    pats = _observe_patterns(config["events"])

    def run(src: SourceFile) -> Iterator[Evidence]:
        for pat in pats:
            for m in pat.finditer(src.text):
                yield Evidence(src.rel, _lineno(src.text, m.start()), kind, m.group(0)[:_SNIPPET])

    return run


def _prepare_regex(kind: str, config: dict, source: CharmSource) -> _Runner:
    pat = re.compile(config["pattern"], flags=re.MULTILINE)

    def run(src: SourceFile) -> Iterator[Evidence]:
        for m in pat.finditer(src.text):
            snippet = m.group(0).strip()[:_SNIPPET]
            yield Evidence(src.rel, _lineno(src.text, m.start()), kind, snippet)

    return run


# ── AST kinds ───────────────────────────────────────────────────────────────────


def _prepare_import(kind: str, config: dict, source: CharmSource) -> _Runner | None:
    # Skip import detectors that target a lib the charm itself provides —
    # importing charms.X from src/ when you ARE charm X is self-referential,
    # not a consumer signal. That is a fact about the charm, so it is settled
    # here rather than re-asked for every file.
    if _charm_provides_lib(source.charm_root, config.get("module", ""), source.charm_name):
        return None

    def run(src: SourceFile) -> Iterator[Evidence]:
        if src.tree is None:
            return
        for imp in _ast_kinds._detect_import(src, config):
            line = ast.get_source_segment(src.text, imp) or ""
            yield Evidence(src.rel, imp.lineno, kind, line.splitlines()[0][:_SNIPPET])

    return run


def _from_nodes(walker: Callable[[SourceFile, dict], Iterator[ast.stmt | ast.expr]]) -> _Factory:
    """Adapt a node-yielding AST walker into a factory.

    Every AST kind but `import` reports the same way — one Evidence per node,
    snippet taken from the node's own source line — so the walker is the only
    thing that differs between them.
    """

    def factory(kind: str, config: dict, source: CharmSource) -> _Runner:
        def run(src: SourceFile) -> Iterator[Evidence]:
            if src.tree is None:
                return
            for node in walker(src, config):
                yield Evidence(src.rel, node.lineno, kind, src.line(node.lineno)[:_SNIPPET])

        return run

    return factory


# ── the registries ──────────────────────────────────────────────────────────────


# Run once per charm, reading the charm root directly: these are the kinds
# that can see files `_select_files` never returns.
_CHARM_KINDS: dict[str, Callable[[CharmSource, dict], list[Evidence]]] = {
    "pytest-config-key": _detect_pytest_config_key,
    "requires-interface": _detect_requires_interface,
    "relation-count": _detect_relation_count,
    "yaml-key": _detect_yaml_key,
}

# Prepared once per feature, then run once per Python file in scope.
_PER_FILE_KINDS: dict[str, _Factory] = {
    "import": _prepare_import,
    "call": _from_nodes(_ast_kinds._detect_call),
    "call-kwarg": _from_nodes(_ast_kinds._detect_call_kwarg),
    "observe-event": _prepare_observe_event,
    "regex": _prepare_regex,
    "ast-init-call": _from_nodes(_ast_kinds._detect_ast_init_call),
    "ast-observe-shared-handler": _from_nodes(_ast_kinds._detect_ast_observe_shared_handler),
    "ast-shared-method": _from_nodes(_ast_kinds._detect_ast_shared_method),
    "ast-subclass-module": _from_nodes(_ast_kinds._detect_ast_subclass_of),
}


# ── public entry point ──────────────────────────────────────────────────────────


def detect_feature(
    charm_root: Path, feature: Detectable, source: CharmSource | None = None
) -> list[Evidence]:
    """Evidence for `feature` in the charm at `charm_root`.

    Pass `source` to share one read-and-parse pass across every feature in
    the catalogue; omitting it builds a throwaway cache for this call alone.
    """
    if source is None:
        source = CharmSource(charm_root)
    evidence: list[Evidence] = []

    runners: list[_Runner] = []
    for det in feature.detectors:
        charm_kind = _CHARM_KINDS.get(det.kind)
        if charm_kind is not None:
            evidence.extend(charm_kind(source, det.config))
            continue
        factory = _PER_FILE_KINDS.get(det.kind)
        if factory is not None:
            runner = factory(det.kind, det.config, source)
            if runner is not None:
                runners.append(runner)

    # A feature whose detectors are all file-independent never asks for the
    # file list, so it never pays for selecting or parsing one.
    if runners:
        for src in source.files(feature.scope):
            for run in runners:
                evidence.extend(run(src))

    return evidence
