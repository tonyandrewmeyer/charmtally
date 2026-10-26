"""Which Juju channels a repo's CI installs.

`assumes:` says what a charm *claims*; most charms claim nothing, so the
adoption page also asks what they are *tested* on. The answer lives in CI, and
reading it accurately means evaluating the workflows rather than grepping
them: the channel a test job installs is usually an input, defaulted three
files away, bound from a strategy matrix, or computed in a bash step and
handed on through `$GITHUB_OUTPUT`. A grep for `4.0/stable` finds a version a
workflow *mentions*; this finds the versions it *installs*.

`operator-workflows`' `integration_test.yaml` is why the difference matters.
Its `juju-channel` input defaults to `2.9/stable`, and nearly every caller
overrides it — so a text search that follows `uses:` reports every one of them
as testing on 2.9, which none of them do.

What is evaluated:

* Every workflow under `.github/workflows/` that something other than
  `workflow_call` triggers, with its `workflow_dispatch` input defaults bound.
* Jobs, once per strategy-matrix combination (`include` / `exclude` honoured),
  with workflow, job and step `env:` layered as Actions layers them.
* Reusable-workflow calls, same-repo or cross-repo, with the caller's `with:`
  bound over the callee's input defaults — transitively, and inside the repo
  the *callee* lives in, so a remote workflow's `./.github/...` is its own.
* Composite actions, local or remote, with their inputs bound the same way.
* What installs Juju: `charmed-kubernetes/actions-operator`'s `juju-channel`
  (its own default read from its `action.yaml`), `snap install juju`, and
  `concierge prepare` — by flag, by `CONCIERGE_JUJU_CHANNEL`, or by the config
  file it reads. Spread's `environment:` blocks carry the last as a variant
  list (`CONCIERGE_JUJU_CHANNEL/juju36: 3.6/stable`), so every `spread.yaml`
  in the repo — a monorepo keeps one per sub-charm — is read for it too, as
  is charm-ci's `CONCIERGE`, which names the config file.
* `$GITHUB_OUTPUT` written from bash, through an `if` / `elif` / `else`
  evaluator that knows `-n`, `-z`, `==` and `!=` over quoted strings and
  `$VAR` / `${VAR#prefix}`. That is the whole of what observability's shared
  workflow uses to pick a channel from the branch name.

What is not, and how it reads: an expression this cannot evaluate —
`vars.JUJU_VERSION`, `fromJSON(...)`, a ternary — leaves its install
*unresolved*, never guessed; an install that names no channel at all is
*unpinned*, since it gets whatever `latest/stable` was that day, which no
snapshot can recover. A bash condition this cannot evaluate keeps every
branch as a candidate, so it can over-report and never under-reports. Neither
`if:` on a job nor `continue-on-error` is consulted: the question is which
versions CI runs against, pass or fail.

The branch a pull request targets is taken to be the repo's default branch,
which is what a charm's PR CI runs against — and the only branch the
observability rule distinguishes is `track/1`, which no repo defaults to.

Remote files go through `workflows.fetch`, so nothing here touches the network
unless the run installed a cache, exactly like `repo-file`'s `follow_uses`.
"""

from __future__ import annotations

import contextlib
import itertools
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import workflows

#: How deep reusable-workflow and composite-action calls are followed. The
#: deepest chain in the corpus is three (charm → observability's
#: `charm-pull-request` → `_charm-quality-checks` → `concierge prepare`).
MAX_DEPTH = 5

#: Strategy-matrix combinations evaluated per job. The largest matrix in the
#: corpus is well under this; the cap is what stops a pathological one from
#: making a single charm take minutes.
MAX_COMBOS = 128

#: How far below the repo root a `spread.yaml` is looked for. Sub-charms sit
#: one or two levels down (`machines/`, `charms/foo/`); three leaves room.
SPREAD_DEPTH = 3

#: Alternatives one templated string may expand to. Alternatives come only
#: from bash outputs this could not narrow down, so this is generous.
MAX_ALTERNATIVES = 16

#: Node actions that install Juju, and the input that names the channel.
#: Composite actions need no entry: their steps are evaluated directly.
INSTALLING_ACTIONS = {"charmed-kubernetes/actions-operator": "juju-channel"}

_CHANNEL_RE = re.compile(r"^(?P<major>\d+)(?:\.(?P<minor>\d+))?(?:/[A-Za-z0-9_.-]+)?$")


def parse_channel(channel: str) -> tuple[int, int | None] | None:
    """`3.6/stable` → (3, 6); `4/edge` → (4, None); `latest/stable` → None."""
    match = _CHANNEL_RE.match(channel.strip().strip("'\""))
    if match is None:
        return None
    minor = match["minor"]
    return int(match["major"]), None if minor is None else int(minor)


# ── where files come from ───────────────────────────────────────────────────


@dataclass(frozen=True)
class Repo:
    """A repo whose files can be read: the checkout on disk, or a remote one."""

    root: Path | None = None  # local checkout
    owner: str = ""
    name: str = ""
    ref: str = ""

    @property
    def remote(self) -> bool:
        return self.root is None

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}@{self.ref}" if self.remote else "."

    def read(self, path: str) -> str | None:
        path = path.lstrip("./") if path.startswith("./") else path
        if self.root is not None:
            target = self.root / path
            try:
                return target.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return None
        return workflows.fetch(f"{workflows.RAW_BASE}/{self.owner}/{self.name}/{self.ref}/{path}")


def _load(text: str | None) -> dict | None:
    if not text:
        return None
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    return doc if isinstance(doc, dict) else None


def _input_defaults(spec: object) -> dict[str, str]:
    out: dict[str, str] = {}
    if isinstance(spec, dict):
        for name, body in spec.items():
            if isinstance(body, dict) and "default" in body:
                out[str(name)] = _str(body["default"])
    return out


def _str(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else str(value)


# ── expressions ─────────────────────────────────────────────────────────────

_EXPR_RE = re.compile(r"\$\{\{\s*(.*?)\s*\}\}", re.DOTALL)
_PATH_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*|\['[^']+'\])*$")


#: Marks a value this evaluator cannot know — `vars.*`, `fromJSON(...)`, an
#: output it could not compute. Carried through substitution rather than
#: raised, so an unknown that never reaches a Juju install costs nothing, and
#: one that does reaches it visibly: `Install.unresolved` names it.
UNKNOWN = "\x00"


def unknown(expr: str) -> str:
    """Wrap `expr` as an unknown value."""
    return f"{UNKNOWN}{expr}{UNKNOWN}"


def is_unknown(value: str) -> bool:
    """Whether `value` carries an unknown anywhere in it."""
    return UNKNOWN in value


def describe(value: str) -> str:
    """Turn the unknown markers in `value` back into readable `${{ }}`s."""
    return re.sub(f"{UNKNOWN}(.*?){UNKNOWN}", r"${{ \1 }}", value)


class Opaque(dict):
    """A context whose every key is unknown — a matrix built by `fromJSON`."""


def _lookup(path: str, ctx: dict) -> list[str]:
    """Resolve `inputs.x`, `matrix.juju.agent`, `needs.j.outputs.o`, ...

    A missing input, env var or matrix key reads as empty, as it does in
    Actions — inputs arrive with their defaults already merged in, so absence
    there is real. A missing step or job output is *unknown* instead: it is
    usually one this evaluator could not compute, and reading that as empty
    would send an `||` chain to its fallback on no evidence.
    """
    parts = [p.strip("'") for p in re.findall(r"[A-Za-z_][A-Za-z0-9_-]*|'[^']+'", path)]
    if not parts or parts[0] not in ctx:
        return [unknown(path)]
    value: object = ctx
    for part in parts:
        if isinstance(value, Opaque):
            return [unknown(path)]
        if not isinstance(value, dict) or part not in value:
            return [""] if parts[0] in ("inputs", "env", "matrix") else [unknown(path)]
        value = value[part]
    if isinstance(value, Opaque):
        return [unknown(path)]
    if isinstance(value, list):
        return [_str(v) for v in value] or [""]
    if isinstance(value, dict):
        return [unknown(path)]
    return [_str(value)]


def _evaluate(expr: str, ctx: dict) -> list[str]:
    """Evaluate one `${{ }}` body: literals, context paths, and `||` chains.

    An `||` chain whose first live term is unknown is unknown as a whole: the
    fallback is only its value if the unknown is empty, which is exactly what
    cannot be known — `vars.JUJU_VERSION || '3/stable'` may well be 4.0.
    """
    terms = [t.strip() for t in expr.split("||")]
    for index, term in enumerate(terms):
        if re.fullmatch(r"'(?:[^']|'')*'", term):
            values = [term[1:-1].replace("''", "'")]
        elif _PATH_RE.match(term):
            values = _lookup(term, ctx)
        else:
            return [unknown(expr)]
        if any(is_unknown(v) for v in values):
            return [unknown(expr)] if len(terms) > 1 else values
        truthy = [v for v in values if v not in ("", "false", "0")]
        rest = terms[index + 1 :]
        if not rest or (truthy and len(truthy) == len(values)):
            return values
        if truthy:
            # Some alternatives are empty and some are not: the chain's value
            # is each live alternative, or the rest of the chain for the others.
            return truthy + [v for v in _evaluate("||".join(rest), ctx) if v not in truthy]
    return [""]


def substitute(text: str, ctx: dict) -> list[str]:
    """Expand every `${{ }}` in `text`, returning its possible values."""
    pieces: list[list[str]] = []
    last = 0
    for match in _EXPR_RE.finditer(text):
        pieces.append([text[last : match.start()]])
        pieces.append(_evaluate(match.group(1), ctx))
        last = match.end()
    pieces.append([text[last:]])
    out: list[str] = []
    for combo in itertools.product(*pieces):
        joined = "".join(combo)
        if joined not in out:
            out.append(joined)
        if len(out) >= MAX_ALTERNATIVES:
            break
    return out


def _substitute_env(env: object, ctx: dict) -> dict[str, list[str]]:
    """Expand an `env:` block."""
    out: dict[str, list[str]] = {}
    if isinstance(env, dict):
        for key, value in env.items():
            out[str(key)] = substitute(_str(value), ctx)
    return out


# ── bash ────────────────────────────────────────────────────────────────────

_SHELL_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(#[^}]*)?\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def _expand_shell(text: str, env: dict[str, list[str]]) -> str | None:
    """Expand `$VAR`, `${VAR}` and `${VAR#prefix}`; None if a value is unknown."""
    missing = False

    def one(match: re.Match[str]) -> str:
        nonlocal missing
        name = match.group(1) or match.group(3)
        values = env.get(name)
        if not values or len(values) != 1:
            missing = True
            return ""
        value = values[0]
        strip = match.group(2)
        if strip:
            prefix = strip[1:]
            value = value.removeprefix(prefix)
        return value

    out = _SHELL_VAR_RE.sub(one, text)
    return None if missing else out


_TEST_RE = re.compile(r"^\s*(?:if|elif)\s+\[\[?\s*(?P<cond>.*?)\s*\]\]?\s*;?\s*(?:then)?\s*$")


def _condition(cond: str, env: dict[str, list[str]]) -> bool | None:
    """Evaluate a `[[ ]]` test, or None when it cannot be known."""
    words = re.findall(r'"[^"]*"|\'[^\']*\'|\S+', cond)
    expanded: list[str] = []
    for word in words:
        value = _expand_shell(word.strip("'\""), env) if not word.startswith("'") else word[1:-1]
        if value is None or is_unknown(value):
            return None
        expanded.append(value)
    if len(expanded) == 2 and expanded[0] in ("-n", "-z"):
        empty = expanded[1] == ""
        return empty if expanded[0] == "-z" else not empty
    if len(expanded) == 3 and expanded[1] in ("==", "=", "!="):
        same = expanded[0] == expanded[2]
        return not same if expanded[1] == "!=" else same
    return None


_OUTPUT_RE = re.compile(
    r"""echo\s+["']?(?P<name>[A-Za-z0-9_-]+)=(?P<value>[^"'>]*)["']?\s*>>\s*["']?\$\{?GITHUB_OUTPUT"""
)


def step_outputs(script: str, env: dict[str, list[str]]) -> dict[str, list[str]]:
    """Return the values a bash step may write to `$GITHUB_OUTPUT`, by output name.

    Branches whose condition is known to be false are skipped; once a branch
    is known to be taken, later `elif` / `else` arms are too. A condition
    that cannot be evaluated keeps its arm *and* the ones after it.
    """
    out: dict[str, list[str]] = {}
    # Each frame: (arm active?, arm certain?, some earlier arm certainly taken?)
    stack: list[list[bool | None]] = []

    def live() -> bool:
        return all(frame[0] is not False for frame in stack)

    for raw in script.splitlines():
        line = raw.strip()
        test = _TEST_RE.match(line)
        if test and line.startswith("if"):
            verdict = _condition(test["cond"], env)
            stack.append([verdict, verdict is True])
            continue
        if test and line.startswith("elif") and stack:
            frame = stack[-1]
            if frame[1]:
                frame[0] = False
            else:
                verdict = _condition(test["cond"], env)
                frame[0], frame[1] = verdict, verdict is True
            continue
        if line == "else" and stack:
            frame = stack[-1]
            frame[0] = not frame[1]
            continue
        if line == "fi" and stack:
            stack.pop()
            continue
        match = _OUTPUT_RE.search(line)
        if match and live():
            value = _expand_shell(match["value"].strip(), env)
            if value is not None:
                out.setdefault(match["name"], [])
                if value not in out[match["name"]]:
                    out[match["name"]].append(value)
    return out


_ASSIGN_RE = re.compile(r"^\s*(?:export\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>\S*)\s*$")
_SNAP_JUJU_RE = re.compile(r"\bsnap\s+install\b(?P<args>[^\n;&|]*)")
_VALUE = r"(?P<value>\"[^\"]*\"|'[^']*'|[^\s)]+)"
_CHANNEL_ARG_RE = re.compile(r"--channel(?:=|\s+)" + _VALUE)
_CONCIERGE_RE = re.compile(r"\bconcierge\s+prepare\b(?P<args>[^\n;&|]*)")
_JUJU_CHANNEL_ARG_RE = re.compile(r"--juju-channel(?:=|\s+)" + _VALUE)
_CONFIG_ARG_RE = re.compile(r"(?:-c|--config)(?:=|\s+)(?P<value>\S+)")


# ── what a reading is made of ───────────────────────────────────────────────


@dataclass(frozen=True)
class Install:
    """One Juju install a CI run makes.

    `channel` is None for an install this could not pin down: `unresolved`
    names the expression that stopped it, and is empty when the install
    simply named no channel. `path` is the repo-relative file the chain
    starts in, and `anchor` the text to cite a line of it by — the channel
    itself when that file spells it out, otherwise the `uses:` that led away.
    """

    path: str
    anchor: str
    channel: str | None
    via: tuple[str, ...] = ()
    unresolved: str = ""

    @property
    def version(self) -> tuple[int, int | None] | None:
        return parse_channel(self.channel) if self.channel else None

    def matches(self, major: int, minor: int | None = None) -> bool:
        """Whether this install is of Juju `major` (and `minor`, if given).

        A bare track (`4/stable`) names no minor, so it matches the major
        alone: which 4.x the store served that day is not the repo's to say.
        """
        version = self.version
        if version is None or version[0] != major:
            return False
        return minor is None or version[1] == minor


@dataclass
class _Walk:
    local: Repo
    branch: str
    follow: bool
    installs: list[Install] = field(default_factory=list)
    seen: set[tuple] = field(default_factory=set)

    def add(
        self,
        entry: str,
        cite: str | None,
        channel: str | None,
        via: tuple[str, ...],
        unresolved: str = "",
        fallback: str = "",
    ) -> None:
        if channel and is_unknown(channel):
            unresolved, channel = describe(channel), None
        anchor = cite or (channel if channel else fallback)
        install = Install(entry, anchor, channel, via, unresolved)
        if install not in self.installs:
            self.installs.append(install)


@dataclass(frozen=True)
class _Where:
    """Where the walk is: which repo, which local file it started in, how it got here."""

    repo: Repo
    entry: str
    via: tuple[str, ...] = ()
    cite: str | None = None  # the local `uses:` that left the repo, once one has

    def into(self, target: Repo, path: str, uses: str) -> _Where:
        if not target.remote:
            return _Where(target, self.entry, self.via, self.cite)
        return _Where(target, self.entry, (*self.via, f"{target.slug}:{path}"), self.cite or uses)


def _shell_value(raw: str, env: dict[str, list[str]]) -> str | None:
    return _expand_shell(raw.strip("'\""), env)


def _scan_script(walk: _Walk, script: str, env: dict[str, list[str]], at: _Where) -> None:
    """Record the Juju installs one shell script makes."""
    env = dict(env)
    for line in script.splitlines():
        assign = _ASSIGN_RE.match(line)
        if assign:
            value = _shell_value(assign["value"], env)
            env[assign["name"]] = [] if value is None else [value]
        for snap in _SNAP_JUJU_RE.finditer(line):
            args = snap["args"]
            if "juju" not in re.split(r"\s+", args.replace("=", " ").strip()):
                continue
            arg = _CHANNEL_ARG_RE.search(args)
            if arg is None:
                walk.add(at.entry, at.cite, None, at.via, fallback="snap install")
                continue
            value = _shell_value(arg["value"], env)
            walk.add(
                at.entry, at.cite, value, at.via, "" if value else arg["value"], "snap install"
            )
        for prep in _CONCIERGE_RE.finditer(line):
            args = prep["args"]
            if not _JUJU_CHANNEL_ARG_RE.search(args):
                # Flags assembled in a bash array first (`args=(-p k8s
                # --juju-channel="$JUJU_CHANNEL")`, then `concierge prepare
                # "${args[@]}"`) are not on the prepare line; any the script
                # sets are what it passes.
                args = " ".join(m.group(0) for m in _JUJU_CHANNEL_ARG_RE.finditer(script)) or args
            _concierge(walk, args, env, at)


def _concierge(walk: _Walk, args: str, env: dict[str, list[str]], at: _Where) -> None:
    """Record what `concierge prepare` installs: flag, then env, then config file."""
    flags = list(_JUJU_CHANNEL_ARG_RE.finditer(args))
    for flag in flags:
        value = _shell_value(flag["value"], env)
        walk.add(at.entry, at.cite, value, at.via, "" if value else flag["value"], "concierge")
    if flags:
        return
    values = env.get("CONCIERGE_JUJU_CHANNEL")
    if values is not None:
        if not values:
            walk.add(at.entry, at.cite, None, at.via, "CONCIERGE_JUJU_CHANNEL", "concierge")
            return
        if all(values):
            for value in values:
                walk.add(at.entry, at.cite, value, at.via)
            return
    config = _CONFIG_ARG_RE.search(args)
    name = config["value"].strip("'\"") if config else "concierge.yaml"
    doc = _load(at.repo.read(name))
    juju = doc.get("juju") if doc else None
    channel = juju.get("channel") if isinstance(juju, dict) else None
    walk.add(at.entry, at.cite, _str(channel) or None, at.via, fallback="concierge")


# ── the walk ────────────────────────────────────────────────────────────────


def _matrix(spec: object, ctx: dict) -> list[dict]:
    """Expand a strategy matrix to its combinations.

    A matrix this cannot expand — `${{ fromJSON(needs.x.outputs.tests) }}`,
    which is how a suite is split one runner per test file — evaluates the
    job once with an `Opaque` matrix, so the steps still run and only what
    actually reads `matrix.*` comes out unknown.
    """
    if spec is None:
        return [{}]
    if isinstance(spec, str):
        values = substitute(spec, ctx)
        loaded = _load(values[0]) if len(values) == 1 and not is_unknown(values[0]) else None
        if loaded is None:
            return [Opaque()]
        spec = loaded
    if not isinstance(spec, dict):
        return [Opaque()]
    axes: dict[str, list] = {}
    for key, values in spec.items():
        if key in ("include", "exclude"):
            continue
        if not isinstance(values, list):
            return [Opaque()]
        axes[str(key)] = values
    combos = (
        [dict(zip(axes, combo, strict=True)) for combo in itertools.product(*axes.values())]
        if axes
        else []
    )
    for entry in spec.get("exclude") or []:
        if isinstance(entry, dict):
            combos = [c for c in combos if any(c.get(k) != v for k, v in entry.items())]
    for entry in spec.get("include") or []:
        if not isinstance(entry, dict):
            continue
        original = {k: v for k, v in entry.items() if k in axes}
        matched = False
        for combo in combos:
            if original and all(combo.get(k) == v for k, v in original.items()):
                combo.update(entry)
                matched = True
        if not matched:
            combos.append(dict(entry))
    return (combos or [{}])[:MAX_COMBOS]


def _with(spec: object, ctx: dict) -> dict[str, list[str]]:
    """Expand a `with:` block."""
    if not isinstance(spec, dict):
        return {}
    return {str(key): substitute(_str(value), ctx) for key, value in spec.items()}


def _bindings(given: dict[str, list[str]], defaults: dict[str, str]) -> list[dict[str, str]]:
    """Every input binding a call can make: its `with:` over the callee's defaults."""
    merged = {k: [v] for k, v in defaults.items()} | given
    keys = list(merged)
    out = [dict(zip(keys, combo, strict=True)) for combo in itertools.product(*merged.values())]
    return out[:MAX_COMBOS] or [{}]


def _resolve_uses(uses: str, repo: Repo) -> tuple[Repo, str] | None:
    """Split a `uses:` value into (the repo the file is in, the path within it)."""
    uses = uses.strip()
    if uses.startswith("./"):
        return repo, uses[2:].rstrip("/")
    if "@" not in uses or uses.startswith("docker://"):
        return None
    target, ref = uses.rsplit("@", 1)
    parts = target.split("/")
    if len(parts) < 2:
        return None
    return Repo(owner=parts[0], name=parts[1], ref=ref), "/".join(parts[2:])


def _action_doc(repo: Repo, path: str) -> dict | None:
    for name in ("action.yml", "action.yaml"):
        doc = _load(repo.read(f"{path}/{name}" if path else name))
        if doc:
            return doc
    return None


def _run_steps(
    walk: _Walk, steps: object, ctx: dict, env: dict[str, list[str]], at: _Where, depth: int
) -> dict[str, dict]:
    """Evaluate a job's (or composite action's) steps; return their outputs."""
    outputs: dict[str, dict] = {}
    if not isinstance(steps, list):
        return outputs
    for step in steps:
        if not isinstance(step, dict):
            continue
        step_ctx = {**ctx, "steps": outputs, "env": env}
        step_env = {**env, **_substitute_env(step.get("env"), step_ctx)}
        step_ctx["env"] = step_env
        uses = step.get("uses")
        if isinstance(uses, str):
            _run_action(walk, uses, step, step_ctx, step_env, at, depth)
        run = step.get("run")
        if not isinstance(run, str):
            continue
        scripts = substitute(run, step_ctx)
        found: dict[str, list[str]] = {}
        for script in scripts:
            _scan_script(walk, script, step_env, at)
            for name, values in step_outputs(script, step_env).items():
                bucket = found.setdefault(name, [])
                bucket.extend(v for v in values if v not in bucket)
        if step.get("id") is not None and found:
            outputs[_str(step["id"])] = {"outputs": found}
    return outputs


def _run_action(
    walk: _Walk,
    uses: str,
    step: dict,
    ctx: dict,
    env: dict[str, list[str]],
    at: _Where,
    depth: int,
) -> None:
    located = _resolve_uses(uses, at.repo)
    if located is None:
        return
    target, path = located
    if target.remote and not walk.follow:
        return
    given = _with(step.get("with"), ctx)
    name = f"{target.owner}/{target.name}" if target.remote else ""
    if name in INSTALLING_ACTIONS:
        key = INSTALLING_ACTIONS[name]
        values = given.get(key)
        if values is None:
            default = _input_defaults((_action_doc(target, path) or {}).get("inputs")).get(key)
            if default is None:
                walk.add(at.entry, at.cite, None, at.via, f"{uses} default", uses)
                return
            values = [default]
            # The channel is the action's, not written locally: cite the step.
            for value in values:
                walk.add(at.entry, at.cite or uses, value, at.via)
            return
        for value in values:
            walk.add(at.entry, at.cite, value or None, at.via, fallback=uses)
        return
    if depth <= 0:
        return
    doc = _action_doc(target, path)
    if doc is None:
        return
    runs = doc.get("runs")
    if not isinstance(runs, dict) or runs.get("using") != "composite":
        return
    inner = at.into(target, path, uses)
    for inputs in _bindings(given, _input_defaults(doc.get("inputs"))):
        action_ctx = {"inputs": inputs, "github": ctx["github"], "env": env}
        _run_steps(walk, runs.get("steps"), action_ctx, env, inner, depth - 1)


def _job_order(jobs: dict) -> list:
    """Jobs in an order that puts every job after the ones it `needs`."""
    order: list = []
    pending = list(jobs)
    while pending:
        ready = []
        for name in pending:
            job = jobs[name]
            wants = job.get("needs") if isinstance(job, dict) else None
            wants = [wants] if isinstance(wants, str) else list(wants or [])
            if all(w in order or w not in jobs for w in wants):
                ready.append(name)
        if not ready:
            order.extend(pending)
            break
        order.extend(ready)
        pending = [n for n in pending if n not in ready]
    return order


def _on(doc: dict) -> object:
    # PyYAML reads a bare `on:` key as the boolean True.
    return doc.get("on", doc.get(True))


def _triggers(doc: dict) -> set[str]:
    on = _on(doc)
    if isinstance(on, str):
        return {on}
    if isinstance(on, (list, dict)):
        return {str(t) for t in on}
    return set()


def _trigger_inputs(doc: dict, trigger: str) -> dict[str, str]:
    on = _on(doc)
    spec = on.get(trigger) if isinstance(on, dict) else None
    return _input_defaults(spec.get("inputs") if isinstance(spec, dict) else None)


def _run_workflow(
    walk: _Walk, doc: dict, path: str, inputs: dict[str, str], at: _Where, depth: int
) -> None:
    # `path` too: a same-repo call keeps the caller's `via`, so without it a
    # callee bound to no inputs reads as already walked.
    key = (at.repo.slug, path, at.via, at.entry, tuple(sorted(inputs.items())))
    if key in walk.seen:
        return
    walk.seen.add(key)
    github = {"base_ref": walk.branch, "ref_name": walk.branch, "head_ref": ""}
    base = {"inputs": inputs, "github": github}
    wf_env = _substitute_env(doc.get("env"), {**base, "env": {}})
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return
    needs: dict[str, dict] = {}
    for job_name in _job_order(jobs):
        job = jobs[job_name]
        if not isinstance(job, dict):
            continue
        ctx0 = {**base, "needs": needs, "env": wf_env}
        strategy = job.get("strategy")
        combos = _matrix(strategy.get("matrix") if isinstance(strategy, dict) else None, ctx0)
        outputs: dict[str, list[str]] = {}
        for combo in combos:
            ctx = {**ctx0, "matrix": combo}
            env = {**wf_env, **_substitute_env(job.get("env"), ctx)}
            ctx["env"] = env
            uses = job.get("uses")
            if isinstance(uses, str):
                _call_workflow(walk, uses, job, ctx, at, depth)
                continue
            steps = _run_steps(walk, job.get("steps"), ctx, env, at, depth)
            spec = job.get("outputs")
            for name, expr in spec.items() if isinstance(spec, dict) else ():
                values = substitute(_str(expr), {**ctx, "steps": steps})
                bucket = outputs.setdefault(str(name), [])
                bucket.extend(v for v in values if v not in bucket)
        needs[str(job_name)] = {"outputs": outputs}


def _call_workflow(walk: _Walk, uses: str, job: dict, ctx: dict, at: _Where, depth: int) -> None:
    located = _resolve_uses(uses, at.repo)
    if located is None or depth <= 0:
        return
    target, path = located
    if target.remote and not walk.follow:
        return
    doc = _load(target.read(path))
    if doc is None:
        return
    inner = at.into(target, path, uses)
    given = _with(job.get("with"), ctx)
    for inputs in _bindings(given, _trigger_inputs(doc, "workflow_call")):
        _run_workflow(walk, doc, path, inputs, inner, depth - 1)


# ── spread ──────────────────────────────────────────────────────────────────

_SPREAD_KEY_RE = re.compile(r"^CONCIERGE_JUJU_CHANNEL(?:/.*)?$")
_SPREAD_CONFIG_RE = re.compile(r"^CONCIERGE(?:/.*)?$")
# charm-ci writes `CONCIERGE: '$(HOST: echo "${CONCIERGE:-concierge.yaml}")'`.
_HOST_DEFAULT_RE = re.compile(r"\$\{CONCIERGE:-([^}]+)\}")


def _spread_files(repo_root: Path) -> list[str]:
    """Every `spread.yaml` in the repo, root first, as repo-relative paths.

    A monorepo keeps one per sub-charm (`mysql-router-operators` has
    `machines/spread.yaml` and `kubernetes/spread.yaml`), with nothing at the
    root, so looking only there reads its CI as installing no Juju at all.
    The walk skips dotted directories and stops `SPREAD_DEPTH` levels down,
    which is deeper than any sub-charm in the corpus sits.

    Like the workflows, the reading is the repo's, not the charm's: a
    sibling's `spread.yaml` counts for every sub-charm, as one workflow
    covering five sub-charms does.
    """
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(repo_root):
        depth = len(Path(dirpath).relative_to(repo_root).parts)
        keep = depth < SPREAD_DEPTH
        dirnames[:] = sorted(d for d in dirnames if keep and not d.startswith("."))
        if "spread.yaml" in filenames:
            found.append((Path(dirpath) / "spread.yaml").relative_to(repo_root).as_posix())
    return sorted(found, key=lambda rel: (rel.count("/"), rel))


def _spread(walk: _Walk, text: str, entry: str, repo: Repo) -> None:
    """Read the Juju channels a `spread.yaml` sets for Concierge.

    Spread expands `KEY/variant: value` into one job per variant, so every
    value of `CONCIERGE_JUJU_CHANNEL` anywhere in the file is a channel some
    job runs against; the `concierge prepare` in its scripts then has its
    channel from the environment rather than from a flag.

    `canonical/charm-ci`'s `integration-test` backend provisions with
    `opcli env provision -c "$CONCIERGE"` in a script that lives in charm-ci,
    not here, so for it `CONCIERGE` (and its variants) names a Concierge
    config file, `concierge.yaml` when unset, and the channel is that file's.
    Spread runs from the directory its `spread.yaml` is in, so `repo` is
    rooted there and the config file is read beside it.
    """
    doc = _load(text)
    if doc is None:
        return
    channels: list[str] = []
    configs: list[str] = []
    opcli: list[bool] = []
    scripts: list[str] = []

    def visit(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "environment" and isinstance(value, dict):
                    for name, raw in value.items():
                        value = _str(raw).strip("'\"")
                        if _SPREAD_KEY_RE.match(str(name)) and "$" not in value:
                            channels.append(value)
                        elif _SPREAD_CONFIG_RE.match(str(name)):
                            default = _HOST_DEFAULT_RE.search(value)
                            if "$" not in value or default:
                                configs.append(default[1] if default else value)
                elif key == "type" and value == "integration-test":
                    opcli.append(True)
                elif isinstance(value, str) and "\n" in value:
                    scripts.append(value)
                else:
                    visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(doc)
    at = _Where(repo, entry)
    for channel in channels:
        walk.add(entry, None, channel, ())
    env = {"CONCIERGE_JUJU_CHANNEL": channels} if channels else {}
    for script in scripts:
        _scan_script(walk, script, env, at)
    if opcli:
        for name in dict.fromkeys(configs or ["concierge.yaml"]):
            doc = _load(at.repo.read(name))
            juju = doc.get("juju") if doc else None
            channel = juju.get("channel") if isinstance(juju, dict) else None
            cite = name if configs else "integration-test"
            walk.add(entry, cite, _str(channel) or None, (name,))


# ── zuul ────────────────────────────────────────────────────────────────────

#: Job variables the Zuul jobs in the corpus install Juju from: Sunbeam's
#: roles read `juju_channel`, zosci-config's `juju-setup` role
#: `juju_snap_channel`.
ZUUL_CHANNEL_VARS = ("juju_channel", "juju_snap_channel")

#: Where a repo keeps its own Zuul config. `osci.yaml` is what the OpenStack
#: charms' third-party CI (OSCI) reads, beside the `.zuul.yaml` OpenDev reads.
_ZUUL_FILES = ("zuul.yaml", ".zuul.yaml", "osci.yaml")
_ZUUL_DIRS = ("zuul.d", ".zuul.d")

#: Config repos whose jobs and templates the in-repo config refers to by name.
#: A Zuul tenant's config is not visible from the repo, so these are named
#: here: the classic OpenStack charms' `osci.yaml` lists the
#: `charm-functional-jobs` template and nothing else, and the template, the
#: jobs and their Juju channels are all in zosci-config.
ZUUL_CONFIG_REPOS: tuple[tuple[Repo, tuple[str, ...]], ...] = (
    (
        Repo(owner="openstack-charmers", name="zosci-config", ref="master"),
        ("zuul.d/jobs.yaml", "zuul.d/project-templates.yaml"),
    ),
)


@dataclass
class _ZuulConfig:
    """Job and template definitions, each with the file it came from.

    `repos` maps that file back to the repo it is in, which is where a job's
    playbooks and their roles are read from.
    """

    jobs: dict[str, list[tuple[dict, str]]] = field(default_factory=dict)
    templates: dict[str, tuple[dict, str]] = field(default_factory=dict)
    repos: dict[str, Repo] = field(default_factory=dict)

    def add(self, item: dict, where: str, repo: Repo) -> None:
        self.repos[where] = repo
        job = item.get("job")
        if isinstance(job, dict) and job.get("name"):
            self.jobs.setdefault(_str(job["name"]), []).append((job, where))
        template = item.get("project-template")
        if isinstance(template, dict) and template.get("name"):
            self.templates.setdefault(_str(template["name"]), (template, where))


def _zuul_items(text: str | None) -> list[dict]:
    if not text:
        return []
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return []
    return [item for item in doc if isinstance(item, dict)] if isinstance(doc, list) else []


def _zuul_files(repo_root: Path) -> list[str]:
    found = [name for name in _ZUUL_FILES if (repo_root / name).is_file()]
    for name in _ZUUL_DIRS:
        paths = (repo_root / name).glob("*")
        found += sorted(
            p.relative_to(repo_root).as_posix()
            for p in paths
            if p.suffix in (".yml", ".yaml") and p.is_file()
        )
    return found


def _branch_matches(spec: object, branch: str) -> bool:
    """Whether a Zuul `branches:` matcher (a regex, or a list of them) takes `branch`."""
    if spec is None:
        return True
    patterns = [spec] if isinstance(spec, str) else spec if isinstance(spec, list) else []
    for pattern in patterns:
        pattern = _str(pattern)
        if pattern == branch:
            return True
        with contextlib.suppress(re.error):
            if re.fullmatch(pattern, branch):
                return True
    return False


def _pipeline_jobs(body: dict) -> list[tuple[str, dict]]:
    """Every (job name, variant attributes) a project or template's pipelines list."""
    out: list[tuple[str, dict]] = []
    for pipeline in body.values():
        jobs = pipeline.get("jobs") if isinstance(pipeline, dict) else None
        for entry in jobs if isinstance(jobs, list) else ():
            if isinstance(entry, str):
                out.append((entry, {}))
            elif isinstance(entry, dict):
                for name, variant in entry.items():
                    out.append((_str(name), variant if isinstance(variant, dict) else {}))
    return out


def _zuul_vars(attrs: dict) -> dict:
    found = attrs.get("vars")
    return found if isinstance(found, dict) else {}


def _job_chain(config: _ZuulConfig, name: str, branch: str) -> list[list[tuple[dict, str]]]:
    """Job `name`'s variants for `branch`, then its parent's, and so on up.

    The parent is the one the first matching definition names, as in Zuul.
    """
    chain: list[list[tuple[dict, str]]] = []
    seen: set[str] = set()
    while name and name not in seen and len(chain) < MAX_DEPTH * 4:
        seen.add(name)
        variants = [
            (job, where)
            for job, where in config.jobs.get(name, ())
            if _branch_matches(job.get("branches"), branch)
        ]
        if not variants:
            break
        chain.append(variants)
        name = _str(variants[0][0].get("parent"))
    return chain


def _channel_var(found: dict) -> object | None:
    for var in ZUUL_CHANNEL_VARS:
        if var in found:
            return found[var]
    return None


def _job_channel(config: _ZuulConfig, name: str, branch: str) -> tuple[object, str] | None:
    """Return the Juju channel job `name` sets on `branch`, and the file setting it.

    A job's variants apply in definition order over its parent's.
    """
    for variants in _job_chain(config, name, branch):
        for job, where in reversed(variants):
            value = _channel_var(_zuul_vars(job))
            if value is not None:
                return value, where
    return None


def _role_default(config: _ZuulConfig, name: str, branch: str) -> tuple[object, str] | None:
    """Return the channel a role job `name` runs defaults to, and the file defaulting it.

    zosci-config's `func-target` sets no channel: its parent's pre-run
    playbook applies the `juju-setup` role, whose `defaults/main.yaml` does.
    A role default ranks below every job and project variable, so this is
    only asked once those have all come up empty. Roles are read from the
    repo that defines the job whose playbook names them.
    """
    for variants in _job_chain(config, name, branch):
        for job, where in variants:
            repo = config.repos[where]
            for key in ("pre-run", "run"):
                playbooks = job.get(key)
                for playbook in [playbooks] if isinstance(playbooks, str) else playbooks or ():
                    for role in _playbook_roles(repo, _str(playbook)):
                        for ext in ("yaml", "yml"):
                            path = f"roles/{role}/defaults/main.{ext}"
                            value = _channel_var(_load(repo.read(path)) or {})
                            if value is not None:
                                return value, f"{repo.slug}:{path}" if repo.remote else path
    return None


def _playbook_roles(repo: Repo, path: str) -> list[str]:
    """Return the roles an Ansible playbook's plays apply, by name."""
    try:
        doc = yaml.safe_load(repo.read(path) or "")
    except yaml.YAMLError:
        return []
    roles: list[str] = []
    for play in doc if isinstance(doc, list) else ():
        listed = play.get("roles") if isinstance(play, dict) else None
        for role in listed if isinstance(listed, list) else ():
            name = role.get("role", role.get("name")) if isinstance(role, dict) else role
            if name:
                roles.append(_str(name))
    return roles


def _zuul(walk: _Walk, repo_root: Path) -> None:
    """Record the Juju channels the repo's Zuul jobs install.

    The OpenStack charms run their zaza suites on Zuul, not Actions, so a
    reading of `.github/` alone puts all of them in the "no Juju found" band.
    For each job the repo's project stanza runs on `walk.branch` — its own and
    its templates', each `branches:` matcher honoured — the channel is the
    job's (variant over definition over parent), else the project's `vars:`
    — Zuul ranks job variables above project variables — else the default of
    a role the job's playbooks apply. A job for which none of those names a
    channel installs none as far as this can tell, and is not recorded: unit
    test jobs are the common case.

    Remote config repos are read only with `follow`, like a remote workflow.
    """
    files = _zuul_files(repo_root)
    if not files:
        return
    projects: list[tuple[dict, str]] = []
    local: list[tuple[dict, str]] = []
    for rel in files:
        for item in _zuul_items(walk.local.read(rel)):
            local.append((item, rel))
            project = item.get("project")
            # A named project stanza in a repo is configuring some other repo.
            if isinstance(project, dict) and "name" not in project:
                projects.append((project, rel))
    if not projects:
        return
    config = _ZuulConfig()
    # Config-repo definitions first: an in-repo variant layers over them.
    if walk.follow:
        for repo, paths in ZUUL_CONFIG_REPOS:
            for path in paths:
                for item in _zuul_items(repo.read(path)):
                    config.add(item, f"{repo.slug}:{path}", repo)
    for item, rel in local:
        config.add(item, rel, walk.local)
    project_vars: dict = {}
    for project, _ in projects:
        project_vars.update(_zuul_vars(project))
    for project, rel in projects:
        # (job, variant, file listing it, the text in `rel` that leads to it)
        refs = [(name, variant, rel, name) for name, variant in _pipeline_jobs(project)]
        templates = project.get("templates")
        for template in templates if isinstance(templates, list) else ():
            body = config.templates.get(_str(template))
            if body is not None:
                listed = body[1]
                refs += [(n, v, listed, _str(template)) for n, v in _pipeline_jobs(body[0])]
        for name, variant, listed, lead in refs:
            if not _branch_matches(variant.get("branches"), walk.branch):
                continue
            found = _job_channel(config, name, walk.branch)
            if _channel_var(_zuul_vars(variant)) is not None:
                found = (_channel_var(_zuul_vars(variant)), listed)
            if found is None and _channel_var(project_vars) is not None:
                found = (_channel_var(project_vars), rel)
            if found is None:
                found = _role_default(config, name, walk.branch)
            if found is None:
                continue
            value, where = _str(found[0]), found[1]
            via = () if where == rel else (where,)
            # Cite the channel where this file spells it, else the job or
            # template name that leads to the file that does.
            cite = None if where == rel else lead
            if "{{" in value:
                walk.add(rel, cite, None, via, value, name)
            else:
                walk.add(rel, cite, value, via)


# ── entry point ─────────────────────────────────────────────────────────────


def default_branch(repo_root: Path) -> str:
    """Return the branch a pull request to this repo targets, as best a checkout says."""
    for args in (
        ["symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD"],
        ["symbolic-ref", "-q", "--short", "HEAD"],
    ):
        try:
            out = subprocess.run(
                ["git", "-C", str(repo_root), *args],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            ).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            out = ""
        if out:
            return out.removeprefix("origin/")
    return "main"


_MEMO: dict[tuple, list[Install]] | None = None


@contextlib.contextmanager
def memoised():
    """Share one reading per repo across every charm read inside the block.

    Every sub-charm of a monorepo asks the same question of the same
    `.github/`, and `CharmSource` is per charm. A replay knows the tree and
    the fetch hook cannot change under it until it checks out the next
    commit, so it wraps each commit's charms in this; nothing else should,
    since the key does not see the tree's contents.
    """
    global _MEMO
    previous = _MEMO
    _MEMO = {}
    try:
        yield
    finally:
        _MEMO = previous


def read(repo_root: Path, *, follow: bool, branch: str | None = None) -> list[Install]:
    """Return every Juju install the CI of the repo at `repo_root` makes.

    `follow` allows remote workflows and actions to be read; it is a no-op
    unless the run has also installed a workflow cache (`workflows.enabled`).
    """
    key = (repo_root, follow and workflows.enabled(), branch)
    if _MEMO is not None and key in _MEMO:
        return _MEMO[key]
    installs = _read(repo_root, follow=follow, branch=branch)
    if _MEMO is not None:
        _MEMO[key] = installs
    return installs


def _read(repo_root: Path, *, follow: bool, branch: str | None) -> list[Install]:
    local = Repo(root=repo_root)
    walk = _Walk(local, branch or default_branch(repo_root), follow and workflows.enabled())
    wf_dir = repo_root / ".github" / "workflows"
    paths = sorted(p for p in wf_dir.glob("*") if p.suffix in (".yml", ".yaml") and p.is_file())
    for path in paths:
        doc = _load(local.read(str(path.relative_to(repo_root))))
        if doc is None:
            continue
        triggers = _triggers(doc)
        if not triggers or triggers == {"workflow_call"}:
            continue  # only reachable as a callee, with its inputs bound there
        entry = path.relative_to(repo_root).as_posix()
        inputs = _trigger_inputs(doc, "workflow_dispatch")
        _run_workflow(walk, doc, entry, inputs, _Where(local, entry), MAX_DEPTH)
    for rel in _spread_files(repo_root):
        here = Repo(root=(repo_root / rel).parent)
        _spread(walk, local.read(rel) or "", rel, here)
    _zuul(walk, repo_root)
    return walk.installs
