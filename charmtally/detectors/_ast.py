"""The detector kinds that read a file's parsed AST.

Two groups, in the order the package docstring lists them: the per-file
kinds (`import`, `call`, `call-kwarg`) and the holistic patterns backing the
architecture and component-graph axes (`ast-*`). Each is a generator over
matching nodes; turning a node into `Evidence` is `_registry`'s job.

`observe-event` and `regex` match text rather than the tree and live in
`_registry` with the other text kinds.
"""

from __future__ import annotations

import ast
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ._files import SourceFile


def _attr_chain(node: ast.AST) -> list[str] | None:
    """Return the dotted-name chain of an Attribute expression, or None."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return list(reversed(parts))
    return None


# ── detector kinds ──────────────────────────────────────────────────────────────


def _detect_import(src: SourceFile, cfg: dict) -> Iterator[ast.Import | ast.ImportFrom]:
    module = cfg["module"]
    wanted_names = set(cfg.get("names") or [])
    for node in src.imports:
        if isinstance(node, ast.Import):
            # `names:` filters which symbols are imported FROM a module; it
            # can't be checked on a bare `import X` without symbol resolution.
            if wanted_names:
                continue
            for alias in node.names:
                if alias.name == module or alias.name.startswith(module + "."):
                    yield node
                    break
        elif isinstance(node, ast.ImportFrom):
            if not node.module:
                continue
            if node.module == module or node.module.startswith(module + "."):
                if not wanted_names:
                    yield node
                else:
                    for alias in node.names:
                        if alias.name in wanted_names:
                            yield node
                            break


def _detect_call(src: SourceFile, cfg: dict) -> Iterator[ast.Call]:
    """Match calls whose attribute chain ends with the configured dotted suffix.

    e.g. attr = "unit.open_port" matches `self.unit.open_port(80)` and
    `charm.unit.open_port(...)`. Leading underscores on attr-chain segments
    are stripped before comparison, so `self._model.get_secret(...)` matches
    a suffix of `model.get_secret` — helper modules commonly cache
    `self._model = charm.model` and call methods on the alias.
    """
    suffix = cfg["attr"].split(".")
    for node in src.calls:
        func = node.func
        if isinstance(func, ast.Attribute):
            chain = _attr_chain(func)
            if chain and len(chain) >= len(suffix):
                tail = [seg.lstrip("_") for seg in chain[-len(suffix) :]]
                if tail == suffix:
                    yield node
        elif isinstance(func, ast.Name) and len(suffix) == 1 and func.id.lstrip("_") == suffix[0]:
            yield node


# ── AST: holistic-pattern detectors (architecture axis) ────────────────────


def _self_attr_calls(method_body: list[ast.stmt], attrs: set[str]) -> Iterator[ast.Call]:
    """Yield `self.X(...)` calls inside `method_body` where X is in `attrs`."""
    for node in ast.walk(ast.Module(body=method_body, type_ignores=[])):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "self"
            and func.attr in attrs
        ):
            yield node


def _detect_ast_init_call(src: SourceFile, cfg: dict) -> Iterator[ast.Call]:
    """Match charms whose __init__ body contains a `self.X(...)` call where.

    X is one of `cfg["attrs"]`. Signal for the `unconditional-init` pattern:
    reconcile runs on every charm invocation by virtue of being in __init__.

    Walks classes that look like ops.CharmBase subclasses (any class whose
    __init__ takes >=1 positional arg beyond self — a soft heuristic, but
    good enough on charm source where __init__ is almost exclusively the
    charm class).
    """
    tree = src.tree
    if tree is None:
        return
    attrs = set(cfg["attrs"])
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for item in node.body:
            if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                yield from _self_attr_calls(item.body, attrs)


_RELATION_LIFECYCLE_SUFFIXES = (
    "relation_created",
    "relation_joined",
    "relation_changed",
    "relation_departed",
    "relation_broken",
)

_SYMMETRIC_RESOURCE_SUFFIXES = (
    "storage_attached",
    "storage_detaching",
    "pebble_ready",
    "pebble_custom_notice",
    "pebble_check_failed",
    "pebble_check_recovered",
)


def _relation_prefix(event: str) -> str | None:
    """Return the relation-name prefix if `event` is a standard relation.

    lifecycle event (`<relation>_relation_{created,joined,changed,departed,broken}`),
    else None.

    A bare event with no prefix at all (`self.on[relation_name].relation_created`,
    the dynamic/reusable-relation-name idiom) resolves to the trailing attribute
    name alone -- e.g. `relation_created` -- with nothing to strip. CALIBRATION
    #22 follow-up #7: treat that bare form as its own single relation endpoint
    (prefix `""`) rather than falling through to `None`, which previously let
    it dodge the relation-scoped classification entirely.
    """
    for suffix in _RELATION_LIFECYCLE_SUFFIXES:
        if event == suffix:
            return ""
        marker = "_" + suffix
        if event.endswith(marker) and len(event) > len(marker):
            return event[: -len(marker)]
    return None


def _is_relation_scoped_binding(events: set[str]) -> bool:
    """CALIBRATION #21 cut #1: every qualifying event is a standard.

    relation-lifecycle event, and they come from at most 2 distinct relation
    endpoints — relation-scoped plumbing, not charm-wide convergence.

    Covers both the single-relation shape (`chopsticks`, `discourse-k8s-operator`
    — one relation's own lifecycle) and the mirrored-relation shape
    (`loki-k8s-operator` — two relation endpoints, each contributing only
    standard lifecycle events).
    """
    prefixes: set[str] = set()
    for event in events:
        prefix = _relation_prefix(event)
        if prefix is None:
            return False
        prefixes.add(prefix)
    return len(prefixes) <= 2


def _symmetric_resource_split(event: str) -> tuple[str, str] | None:
    """Split `event` into (instance-prefix, suffix) if it ends with a known.

    per-instance resource event suffix (storage/pebble events ops generates
    once per storage mount or container). Returns None otherwise.
    """
    for suffix in _SYMMETRIC_RESOURCE_SUFFIXES:
        marker = "_" + suffix
        if event.endswith(marker) and len(event) > len(marker):
            return event[: -len(marker)], suffix
    return None


def _is_symmetric_resource_fanout(events: set[str]) -> bool:
    """CALIBRATION #21 cut #2: every qualifying event shares one common.

    non-relation suffix (e.g. `*_storage_detaching`, `*_pebble_ready`) across
    what look like N distinct resource instances — symmetric-resource
    fan-out, not reconcile. Covers the `mysql-operators` shape (4 storage
    mounts' `*_storage_detaching` events into one handler).
    """
    suffixes: set[str] = set()
    prefixes: set[str] = set()
    for event in events:
        split = _symmetric_resource_split(event)
        if split is None:
            return False
        prefix, suffix = split
        prefixes.add(prefix)
        suffixes.add(suffix)
    return len(suffixes) == 1 and len(prefixes) == len(events)


# The six baseline, non-relational Juju lifecycle hooks every charm
# eventually observes regardless of architecture. A shared handler bound
# only to a subset of these is idempotent-reconfiguration boilerplate, not
# a reconcile-shaped convergence signal -- CALIBRATION.md #22 follow-up #6,
# confirmed by a third real-world instance in #24 (charmed-linstor's
# linstor-controller, alongside #22's auditd-operator and
# chrony-client-operator).
_BASELINE_LIFECYCLE_EVENTS = frozenset({
    "install",
    "config_changed",
    "upgrade_charm",
    "update_status",
    "start",
    "stop",
})


def _is_baseline_lifecycle_only(events: set[str]) -> bool:
    """Return true when every event in `events` is one of the six baseline Juju.

    lifecycle hooks (`install`/`config_changed`/`upgrade_charm`, optionally
    plus `update_status`/`start`/`stop`) and nothing else -- no relation,
    leader, secret, cert, pebble, or custom-library event is present.
    """
    return bool(events) and events <= _BASELINE_LIFECYCLE_EVENTS


def _build_parent_map(tree: ast.Module) -> dict[ast.AST, ast.AST]:
    parent_map: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parent_map[child] = parent
    return parent_map


def _enclosing_function(
    node: ast.AST, parent_map: dict[ast.AST, ast.AST]
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    cur = node
    while cur in parent_map:
        cur = parent_map[cur]
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur
    return None


def _enclosing_for_with_target(
    node: ast.AST, parent_map: dict[ast.AST, ast.AST], var_name: str
) -> ast.For | None:
    """Walk up from `node` to the nearest enclosing `for <var_name> in ...`.

    loop, stopping at the first function boundary (a same-named loop
    variable in an *outer* method is not this call's loop).
    """
    cur = node
    while cur in parent_map:
        cur = parent_map[cur]
        if (
            isinstance(cur, ast.For)
            and isinstance(cur.target, ast.Name)
            and cur.target.id == var_name
        ):
            return cur
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return None
    return None


def _resolve_observe_aliases(
    tree: ast.Module, parent_map: dict[ast.AST, ast.AST]
) -> dict[tuple[int, str], None]:
    """Return `(enclosing_function_id, name)` keys for local variables bound.

    to `<x>.framework.observe` (any `x` — `self`, a constructor parameter,
    etc.). CALIBRATION #34 follow-up #14 shape 1: `observe =
    self.framework.observe; observe(event, handler)` presents as a bare
    `ast.Name` call, invisible to the `.observe` attribute-access check a
    direct `self.framework.observe(...)` call satisfies. Scoped per
    enclosing function so an unrelated same-named local elsewhere in the
    file can't accidentally alias into this.
    """
    aliases: dict[tuple[int, str], None] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        target = node.targets[0]
        value = node.value
        if not isinstance(target, ast.Name):
            continue
        if (
            isinstance(value, ast.Attribute)
            and value.attr == "observe"
            and isinstance(value.value, ast.Attribute)
            and value.value.attr == "framework"
        ):
            func = _enclosing_function(node, parent_map)
            aliases[id(func), target.id] = None
    return aliases


def _resolve_relation_names(tree: ast.Module) -> dict[int, dict[str, str]]:
    """Map each `ClassDef`'s id to `{name: literal_value}`, resolved from.

    class-body literal assignments (`prometheus_relation_name = "prometheus-config"`)
    and `__init__`'s own `self.<attr> = "literal"` assignments. CALIBRATION #36:
    powers resolving `self.on[self.<attr>]` — the dynamic-relation-name
    subscript idiom — back to the actual relation name it's bound to, so
    that a shared handler observing several *different* relations' events
    through this idiom isn't collapsed to a single bare event string (see
    `_event_name`). Deliberately narrow: only a same-class, statically
    literal binding resolves. Three shapes stay unresolved on purpose, per
    CALIBRATION #36's corpus sweep:

    - A constructor parameter (the relation name supplied by the *caller*,
      e.g. `cos-coordinated-workers`' reusable `relation_name` argument —
      CALIBRATION #22 follow-up #7, and `data-integrator`'s
      `_setup_database_requirer(relation_name)` called once per entry of a
      `DATABASES` list) is runtime data, not a literal binding.
    - A bare `Name` key resolved against a *module*-level constant (found
      twice in the corpus — `kubeflow-tensorboards-operator`'s
      `tensorboard-controller` and `istio-beacon-k8s-operator`, both
      already `reconcile` via other bindings) is deliberately not chased:
      unlike `self.<attr>`, a bare name can be shadowed by a same-named
      local or parameter in the very scope doing the subscripting, and
      resolving it without first ruling that out risks merging two
      genuinely different keys.
    - A cross-module import (`data-integrator`'s `from literals import
      CASSANDRA, KAFKA, ...`) is out of this function's reach entirely —
      it only ever sees one file's tree.
    """
    result: dict[int, dict[str, str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        mapping: dict[str, str] = {}
        for item in node.body:
            if (
                isinstance(item, ast.Assign)
                and len(item.targets) == 1
                and isinstance(item.targets[0], ast.Name)
                and isinstance(item.value, ast.Constant)
                and isinstance(item.value.value, str)
            ):
                mapping[item.targets[0].id] = item.value.value
        for item in node.body:
            is_init = isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            if not (is_init and item.name == "__init__"):
                continue
            for sub in ast.walk(item):
                if (
                    isinstance(sub, ast.Assign)
                    and len(sub.targets) == 1
                    and isinstance(sub.targets[0], ast.Attribute)
                    and isinstance(sub.targets[0].value, ast.Name)
                    and sub.targets[0].value.id == "self"
                    and isinstance(sub.value, ast.Constant)
                    and isinstance(sub.value.value, str)
                ):
                    # Class-body literal wins if both exist for the same name.
                    mapping.setdefault(sub.targets[0].attr, sub.value.value)
        result[id(node)] = mapping
    return result


def _enclosing_class(node: ast.AST, parent_map: dict[ast.AST, ast.AST]) -> ast.ClassDef | None:
    cur = node
    while cur in parent_map:
        cur = parent_map[cur]
        if isinstance(cur, ast.ClassDef):
            return cur
    return None


def _event_name(node: ast.expr, relation_names: dict[str, str] | None = None) -> str | None:
    """Trailing attribute name of an event expression.

    Handles `self.on.<x>`, `self.on['c'].<x>`, `<lib>.on.<x>`, etc. — any
    expression ending in a plain attribute access. Bare names (the
    `reconcile-all` loop variable over `.values()`) return None.

    CALIBRATION #36: when the base is a dynamic-relation-name subscript
    (`self.on[<key>]`), try to resolve `<key>` to the actual relation
    name — a string literal (`self.on['db']`) or a `self.<attr>` reference
    resolved via `relation_names` (a same-class literal binding, see
    `_resolve_relation_names`) — and fold it into the returned name as
    `<relation>_<attr>`, so `_relation_prefix` can recover it as a normal
    named-relation event. Unresolvable keys (e.g. a constructor parameter)
    fall back to the bare trailing attribute name, exactly as before.
    """
    if not isinstance(node, ast.Attribute):
        return None
    base = node.value
    if isinstance(base, ast.Subscript):
        key = base.slice
        resolved: str | None = None
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            resolved = key.value
        elif (
            relation_names
            and isinstance(key, ast.Attribute)
            and isinstance(key.value, ast.Name)
            and key.value.id == "self"
        ):
            resolved = relation_names.get(key.attr)
        if resolved is not None:
            return f"{resolved}_{node.attr}"
    return node.attr


# Action events (`self.on.<x>_action`) are Juju user-triggered commands, not
# state-convergence triggers — a shared dispatcher fanning out several
# actions to one handler method is a routing convenience, not a reconcile
# signal. Only matters for loop-resolved bindings (CALIBRATION #35): a
# direct, explicit `observe(self.on.foo_action, handler)` call is rare
# enough in practice that it's never been observed to cross the threshold,
# but a hand-built loop like `charm-kubernetes-control-plane`'s
# `action_events = [...]; for action in action_events: observe(action, ...)`
# is a real, verified-negative shape this exclusion is required to catch.
_LOOP_ACTION_SUFFIX = "_action"


def _detect_ast_observe_shared_handler(src: SourceFile, cfg: dict) -> Iterator[ast.Call]:
    """Match the holistic `reconcile` pattern: a single handler method is.

    bound to >= `min_events` distinct events via `framework.observe(...)`.

    Bind-count is the discriminating signal, not the handler's name — a
    `_reconcile_state` / `_handle` / `_update` method that receives three
    or more event types is reconcile-shaped. Two shared bindings (e.g.
    `leader_elected + leader_settings_changed -> _on_leader`) is a
    single-responsibility pattern, not reconcile. A binding whose events
    are entirely drawn from the six baseline Juju lifecycle hooks (see
    `_is_baseline_lifecycle_only`) is excluded too — see CALIBRATION.md
    #22 follow-up #6 / #24.

    Event identifier: the trailing attribute name of `args[0]` (works for
    `self.on.<x>`, `self.on['c'].<x>`, etc.). Handler identifier: trailing
    attribute name of `args[1]`. Events matching any suffix in
    `exclude_suffixes` (default: `_error`) are filtered out before
    counting.

    Two further exclusions (CALIBRATION #21 follow-ups #1/#2) declassify
    otherwise-qualifying bindings that are relation-scoped plumbing or
    symmetric-resource fan-out rather than charm-wide convergence — see
    `_is_relation_scoped_binding` / `_is_symmetric_resource_fanout`.

    CALIBRATION #35 (follow-up #14): the call site's callee may also be a
    local alias of `self.framework.observe` (see `_resolve_observe_aliases`),
    and the event argument may be a loop variable bound over a *literal*
    `List`/`Tuple` of events — either written inline in the `for` statement,
    or assigned to a same-function variable first (the
    `mediawiki-k8s-operator` shape: `reconciliation_events = [...]; for
    event in reconciliation_events: observe(event, handler)`). The
    already-excluded `reconcile-all` idiom (`self.on.events().values()`) is
    a `Call`, not a literal, and stays unresolved — it still doesn't
    qualify.

    CALIBRATION #36: `_event_name` also resolves the dynamic-relation-name
    subscript idiom (`self.on[<key>].relation_joined`) back to a
    per-relation event name when `<key>` is statically determinable (a
    string literal, or a `self.<attr>` reference to a same-class literal
    binding — see `_resolve_relation_names`), instead of collapsing every
    such call to the bare trailing attribute name regardless of which
    relation it's for. A runtime-supplied key (e.g. a constructor
    parameter) stays unresolved, same as before.
    """
    tree = src.tree
    if tree is None:
        return
    min_events = int(cfg.get("min_events", 3))
    exclude_suffixes = tuple(cfg.get("exclude_suffixes", ["_error"]))

    parent_map = _build_parent_map(tree)
    aliases = _resolve_observe_aliases(tree, parent_map)
    relation_names_by_class = _resolve_relation_names(tree)

    # Same-function `NAME = [literal, ...]` assignments, for resolving a
    # for-loop whose `.iter` is a bare Name rather than an inline literal.
    list_assigns: dict[tuple[int, str], ast.List | ast.Tuple] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, (ast.List, ast.Tuple))
        ):
            func = _enclosing_function(node, parent_map)
            list_assigns[id(func), node.targets[0].id] = node.value

    # Per for-loop (keyed by node id, not variable name, so two loops using
    # the same variable name in different scopes don't collide) — the
    # resolved literal element list, inline or one hop through a variable.
    loop_elements: dict[int, list[ast.expr]] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.For) and isinstance(node.target, ast.Name)):
            continue
        iter_node = node.iter
        if isinstance(iter_node, ast.Name):
            func = _enclosing_function(node, parent_map)
            iter_node = list_assigns.get((id(func), iter_node.id))
        if isinstance(iter_node, (ast.List, ast.Tuple)):
            loop_elements[id(node)] = list(iter_node.elts)

    per_handler_events: dict[str, set[str]] = {}
    per_handler_calls: dict[str, list[ast.Call]] = {}

    def _record(event: str | None, handler: str | None, call_node: ast.Call) -> None:
        if event is None or handler is None:
            return
        if event.endswith(exclude_suffixes):
            return
        per_handler_events.setdefault(handler, set()).add(event)
        per_handler_calls.setdefault(handler, []).append(call_node)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_observe = (isinstance(func, ast.Attribute) and func.attr == "observe") or (
            isinstance(func, ast.Name)
            and (id(_enclosing_function(node, parent_map)), func.id) in aliases
        )
        if not is_observe:
            continue
        if len(node.args) < 2:
            continue
        handler = node.args[1].attr if isinstance(node.args[1], ast.Attribute) else None
        relation_names = relation_names_by_class.get(id(_enclosing_class(node, parent_map)), {})
        first_arg = node.args[0]
        direct_event = _event_name(first_arg, relation_names)
        if direct_event is not None:
            _record(direct_event, handler, node)
            continue
        if not isinstance(first_arg, ast.Name):
            continue
        enclosing_for = _enclosing_for_with_target(node, parent_map, first_arg.id)
        if enclosing_for is None:
            continue
        for element in loop_elements.get(id(enclosing_for), []):
            ev = _event_name(element, relation_names)
            if ev is None or ev.endswith(_LOOP_ACTION_SUFFIX):
                continue
            _record(ev, handler, node)

    for handler, events in per_handler_events.items():
        if len(events) < min_events:
            continue
        if _is_relation_scoped_binding(events):
            continue
        if _is_symmetric_resource_fanout(events):
            continue
        if _is_baseline_lifecycle_only(events):
            continue
        yield from per_handler_calls[handler]


def _detect_ast_shared_method(src: SourceFile, cfg: dict) -> Iterator[ast.Call]:
    """Match charms with the `part-reconcile` pattern: per-event `_on_*`.

    handler methods that each delegate into a shared reconcile method.

    Fires when at least `cfg["min_callers"]` distinct `_on_*` (or otherwise
    named) handler methods inside a single class body contain a call to
    `self.X(...)` where X is in `cfg["attrs"]`. Yields one ast.Call node
    per qualifying caller so evidence lines are reported per handler.
    """
    tree = src.tree
    if tree is None:
        return
    attrs = set(cfg["attrs"])
    min_callers = int(cfg.get("min_callers", 2))
    handler_re = re.compile(cfg.get("handler_re", r"^_on_"))

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        callers: list[ast.Call] = []
        for item in node.body:
            if not isinstance(item, ast.FunctionDef):
                continue
            if not handler_re.match(item.name):
                continue
            first_call = next(_self_attr_calls(item.body, attrs), None)
            if first_call is not None:
                callers.append(first_call)
        if len(callers) >= min_callers:
            yield from callers


# ── AST: base-class-through-import-table detector (component-graph axis) ───


def _resolve_import_table(tree: ast.Module) -> dict[str, str]:
    """Map each name this file binds via import to its full dotted source path.

    `import a.b.c` binds the name `a` (not `a.b.c`, unless aliased) to `a`
    itself — the root package, which a later `a.b.c.X` attribute chain walks
    from. `import a.b.c as x` binds `x` -> `a.b.c`. `from a.b import c` binds
    `c` -> `a.b.c`; `from a.b import c as x` binds `x` -> `a.b.c`. A relative
    import (`from . import c`, `from .x import c`) has no absolute dotted
    path to record and is skipped.
    """
    table: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    table[alias.asname] = alias.name
                else:
                    table[alias.name.split(".")[0]] = alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            if not node.module or node.level:
                continue
            for alias in node.names:
                table[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return table


def _resolve_base_dotted_path(node: ast.expr, import_table: dict[str, str]) -> str | None:
    """Resolve a class base expression to its fully-dotted source path.

    `chain[0]` — the base's own leading Name — is looked up in the file's
    import table; the rest of the attribute chain is appended onto whatever
    that name resolves to. Returns None when the base isn't a Name/Attribute
    chain at all, or its leading name isn't one this file imports.
    """
    chain = _attr_chain(node)
    if not chain:
        return None
    head = import_table.get(chain[0])
    if head is None:
        return None
    return head if len(chain) == 1 else f"{head}.{'.'.join(chain[1:])}"


def _detect_ast_subclass_of(src: SourceFile, cfg: dict) -> Iterator[ast.ClassDef]:
    """Match a ClassDef with a base that resolves to a dotted path under `cfg["module"]`.

    Signal for `component-graph`'s `paas_charm.*` member (CALIBRATION #42):
    the PaaS framework ships one `Charm` subclass per language module
    (`paas_charm.flask.Charm`, `paas_charm.go.Charm`, ...) and the module
    list keeps growing upstream, so matching against an enumerated list of
    modules needs maintaining forever. Matching against the `paas_charm`
    root itself, resolved through each file's own import table rather than
    string-matching the base's source text, does not — whichever submodule
    a charm subclasses, its base resolves to a dotted path under the
    configured root and this fires once, exactly the method #42's
    corpus-wide sweep used and validated.

    Yields the ClassDef once per matching base, not once per class — a
    class with two bases both resolving under the root (CALIBRATION #42's
    `open-graph-images-generator/charm`, which subclasses both
    `paas_charm.go.Charm` and `paas_charm.app.App`) still only makes the
    pattern present, since presence is a boolean over all evidence.
    """
    tree = src.tree
    if tree is None:
        return
    module = cfg["module"]
    import_table = _resolve_import_table(tree)
    if not import_table:
        return
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for base in node.bases:
            resolved = _resolve_base_dotted_path(base, import_table)
            if resolved is not None and (resolved == module or resolved.startswith(module + ".")):
                yield node
                break


def _detect_call_kwarg(src: SourceFile, cfg: dict) -> Iterator[ast.Call]:
    """Match `call`-style calls that pass a keyword argument with a listed value.

    Config is `call`'s `attr`, plus `keyword` and `equals` (a list of
    literals). Used where the call itself is not the signal — the argument
    that decides what the call *does* is: `load_config(C, errors='blocked')`
    handles a bad config, `load_config(C)` lets it reach the hook boundary,
    and both are the same call.

    A keyword passed as anything but a literal — a variable, or splatted in
    from a dict — is deliberately not yielded. Its value isn't visible
    without resolution a scan doesn't do, and counting it either way would
    be a guess; absent evidence reads as "not shown to do this", which is
    what it is.
    """
    keyword = cfg["keyword"]
    wanted = cfg["equals"]
    for call in _detect_call(src, cfg):
        for kw in call.keywords:
            if kw.arg != keyword or not isinstance(kw.value, ast.Constant):
                continue
            if any(kw.value.value == want for want in wanted):
                yield call
                break
