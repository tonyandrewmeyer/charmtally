"""Tests for charmtally.ci_juju: evaluating which Juju channels a repo's CI installs.

Each shape here is one the corpus actually uses; the comment names a repo
that has it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .. import ci_juju, workflows

if TYPE_CHECKING:
    from pathlib import Path

_RAW = workflows.RAW_BASE


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "repo"
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def _read(tmp_path: Path, files: dict[str, str], remote: dict[str, str] | None = None) -> list:
    root = _repo(tmp_path, files)
    with workflows.session(tmp_path / "cache", lambda url: (remote or {}).get(url)):
        return ci_juju.read(root, follow=True, branch="main")


def _channels(installs: list) -> list[str | None]:
    return [i.channel for i in installs]


def test_parse_channel() -> None:
    assert ci_juju.parse_channel("3.6/stable") == (3, 6)
    assert ci_juju.parse_channel("4/edge") == (4, None)
    assert ci_juju.parse_channel("2.9") == (2, 9)
    assert ci_juju.parse_channel("latest/stable") is None


def test_matrix_drives_a_snap_install(tmp_path: Path) -> None:
    """notary-k8s-operator: one job per channel, 4.0 allowed to fail."""
    wf = """\
on: [pull_request]
jobs:
  integration:
    strategy:
      matrix:
        juju-version: ["3.6/stable", "4.0/stable"]
    continue-on-error: ${{ matrix.juju-version == '4.0/stable' }}
    steps:
      - run: |
          sudo snap install juju --channel=${{ matrix.juju-version }}
"""
    installs = _read(tmp_path, {".github/workflows/ci.yaml": wf})
    assert _channels(installs) == ["3.6/stable", "4.0/stable"]
    assert installs[0].path == ".github/workflows/ci.yaml"


_OPERATOR_WORKFLOWS = (
    f"{_RAW}/canonical/operator-workflows/main/.github/workflows/integration_test.yaml"
)
_INTEGRATION_TEST = """\
on:
  workflow_call:
    inputs:
      juju-channel:
        type: string
        default: 2.9/stable
jobs:
  integration-test:
    steps:
      - uses: charmed-kubernetes/actions-operator@main
        with:
          juju-channel: ${{ inputs.juju-channel }}
"""


def test_a_callers_input_overrides_the_shared_workflows_default(tmp_path: Path) -> None:
    """operator-workflows defaults to 2.9; a caller passing 3 is not testing 2.9."""
    wf = """\
on: pull_request
jobs:
  integration-tests:
    uses: canonical/operator-workflows/.github/workflows/integration_test.yaml@main
    with:
      juju-channel: 3/stable
"""
    installs = _read(
        tmp_path, {".github/workflows/ci.yaml": wf}, {_OPERATOR_WORKFLOWS: _INTEGRATION_TEST}
    )
    assert _channels(installs) == ["3/stable"]
    # Cited at the local `uses:` that led there, with the hop named.
    assert installs[0].anchor.startswith("canonical/operator-workflows/")
    assert installs[0].via == (
        "canonical/operator-workflows@main:.github/workflows/integration_test.yaml",
    )


def test_the_shared_workflows_default_applies_when_nobody_overrides_it(tmp_path: Path) -> None:
    wf = """\
on: pull_request
jobs:
  integration-tests:
    uses: canonical/operator-workflows/.github/workflows/integration_test.yaml@main
"""
    installs = _read(
        tmp_path, {".github/workflows/ci.yaml": wf}, {_OPERATOR_WORKFLOWS: _INTEGRATION_TEST}
    )
    assert _channels(installs) == ["2.9/stable"]


def test_remote_workflows_are_not_read_without_follow(tmp_path: Path) -> None:
    wf = """\
on: pull_request
jobs:
  t:
    uses: canonical/operator-workflows/.github/workflows/integration_test.yaml@main
"""
    root = _repo(tmp_path, {".github/workflows/ci.yaml": wf})
    with workflows.session(tmp_path / "cache", {_OPERATOR_WORKFLOWS: _INTEGRATION_TEST}.get):
        assert ci_juju.read(root, follow=False, branch="main") == []
    # And with follow but no cache installed, nothing is fetched either.
    assert ci_juju.read(root, follow=True, branch="main") == []


def test_actions_operator_default_is_read_from_its_action_metadata(tmp_path: Path) -> None:
    wf = """\
on: push
jobs:
  t:
    steps:
      - uses: charmed-kubernetes/actions-operator@main
        with:
          provider: microk8s
"""
    action = "inputs:\n  juju-channel:\n    default: 3/stable\nruns:\n  using: node20\n"
    installs = _read(
        tmp_path,
        {".github/workflows/ci.yaml": wf},
        {f"{_RAW}/charmed-kubernetes/actions-operator/main/action.yml": action},
    )
    assert _channels(installs) == ["3/stable"]


def test_branch_dependent_selection_through_github_output(tmp_path: Path) -> None:
    """observability: a bash step picks the channel from the branch name."""
    wf = """\
on: pull_request
jobs:
  snap-channels:
    outputs:
      juju-channel: ${{ steps.channels.outputs.juju-channel }}
    env:
      BRANCH_NAME: ${{ github.base_ref || github.ref_name }}
    steps:
      - id: channels
        run: |
          if [[ -n "${{ inputs.juju-channel }}" ]]; then
            echo "juju-channel=${{ inputs.juju-channel }}" >> "$GITHUB_OUTPUT"
          elif [[ "${BRANCH_NAME#refs/heads/}" == "track/1" ]]; then
            echo "juju-channel=3.1/stable" >> "$GITHUB_OUTPUT"
          else
            echo "juju-channel=3.6/stable" >> "$GITHUB_OUTPUT"
          fi
  integration:
    needs: [snap-channels]
    steps:
      - env:
          CONCIERGE_JUJU_CHANNEL: "${{ needs.snap-channels.outputs.juju-channel }}"
        run: |
          sudo snap install concierge --classic
          sudo -E concierge prepare -p microk8s
"""
    assert _channels(_read(tmp_path, {".github/workflows/ci.yaml": wf})) == ["3.6/stable"]


def test_an_unknowable_condition_keeps_every_arm() -> None:
    script = """\
if [[ "$UNSET" == "x" ]]; then
  echo "c=3.1/stable" >> "$GITHUB_OUTPUT"
else
  echo "c=3.6/stable" >> "$GITHUB_OUTPUT"
fi
"""
    assert ci_juju.step_outputs(script, {}) == {"c": ["3.1/stable", "3.6/stable"]}


def test_a_repo_variable_is_unresolved_not_guessed(tmp_path: Path) -> None:
    """`vars.*` lives in repo settings; its `||` fallback may never apply."""
    wf = """\
on: push
jobs:
  t:
    steps:
      - uses: charmed-kubernetes/actions-operator@main
        with:
          juju-channel: ${{ vars.JUJU_VERSION || '3/stable' }}
"""
    installs = _read(tmp_path, {".github/workflows/ci.yaml": wf})
    assert _channels(installs) == [None]
    assert installs[0].unresolved == "${{ vars.JUJU_VERSION || '3/stable' }}"


def test_an_unexpandable_matrix_still_runs_its_steps(tmp_path: Path) -> None:
    """A suite split one runner per file (`fromJSON`) still installs a pinned Juju."""
    wf = """\
on: push
jobs:
  t:
    strategy:
      matrix: ${{ fromJSON(needs.collect.outputs.tests) }}
    steps:
      - run: sudo concierge prepare -p k8s --juju-channel=3.6/stable
"""
    assert _channels(_read(tmp_path, {".github/workflows/ci.yaml": wf})) == ["3.6/stable"]


def test_concierge_flags_built_in_a_bash_array(tmp_path: Path) -> None:
    """workflows-team's juju-nightly assembles `args=(...)` before `prepare`."""
    wf = """\
on: schedule
jobs:
  t:
    steps:
      - env:
          JUJU_CHANNEL: 4.1/edge
        run: |
          args=(-p k8s --juju-channel="$JUJU_CHANNEL")
          sudo -E concierge prepare "${args[@]}"
"""
    assert _channels(_read(tmp_path, {".github/workflows/ci.yaml": wf})) == ["4.1/edge"]


def test_concierge_reads_its_config_file(tmp_path: Path) -> None:
    wf = "on: push\njobs:\n  t:\n    steps:\n      - run: concierge prepare -c ci.yaml\n"
    files = {".github/workflows/ci.yaml": wf, "ci.yaml": "juju:\n  channel: 3.6/stable\n"}
    assert _channels(_read(tmp_path, files)) == ["3.6/stable"]


def test_an_install_naming_no_channel_is_unpinned(tmp_path: Path) -> None:
    wf = "on: push\njobs:\n  t:\n    steps:\n      - run: sudo snap install juju --classic\n"
    installs = _read(tmp_path, {".github/workflows/ci.yaml": wf})
    assert _channels(installs) == [None]
    assert installs[0].unresolved == ""


def test_other_snaps_are_not_juju(tmp_path: Path) -> None:
    wf = """\
on: push
jobs:
  t:
    steps:
      - run: |
          sudo snap install juju-wait --channel=latest/stable --classic
          sudo snap install charmcraft --channel=3.x/stable
"""
    assert _read(tmp_path, {".github/workflows/ci.yaml": wf}) == []


def test_a_workflow_only_called_is_read_with_the_callers_inputs(tmp_path: Path) -> None:
    """A `workflow_call`-only file is not an entry point; its caller binds it."""
    caller = """\
on: pull_request
jobs:
  it:
    uses: ./.github/workflows/integration.yaml
    with:
      channel: 4.0/stable
"""
    callee = """\
on:
  workflow_call:
    inputs:
      channel: {type: string, default: 2.9/stable}
jobs:
  t:
    steps:
      - run: sudo snap install juju --channel ${{ inputs.channel }}
"""
    files = {".github/workflows/ci.yaml": caller, ".github/workflows/integration.yaml": callee}
    assert _channels(_read(tmp_path, files)) == ["4.0/stable"]


def test_spread_variants(tmp_path: Path) -> None:
    """data-platform charms: one spread job per `CONCIERGE_JUJU_CHANNEL/<variant>`."""
    spread = """\
project: x
backends:
  ci:
    systems: [ubuntu-24.04]
environment:
  CONCIERGE_JUJU_CHANNEL/juju36: 3.6/stable
  CONCIERGE_JUJU_CHANNEL/juju29: 2.9/stable
prepare: |
  concierge prepare --trace
"""
    installs = _read(tmp_path, {"spread.yaml": spread})
    assert sorted(c for c in _channels(installs) if c) == ["2.9/stable", "3.6/stable"]
    assert {i.path for i in installs} == {"spread.yaml"}


def test_charm_ci_concierge_config_files(tmp_path: Path) -> None:
    """haproxy-operator: charm-ci's `integration-test` backend provisions from `$CONCIERGE`."""
    spread = """\
project: x
backends:
  integration-test:
    type: integration-test
    systems: [ubuntu-24.04]
environment:
  CONCIERGE: '$(HOST: echo "${CONCIERGE:-concierge-lxd.yaml}")'
integration-suites:
  tests/integration/:
    environment:
      CONCIERGE/legacy: concierge-juju3.yaml
"""
    files = {
        "spread.yaml": spread,
        "concierge-lxd.yaml": "juju:\n  channel: 4.1/edge\n",
        "concierge-juju3.yaml": "juju:\n  channel: 3/stable\n",
    }
    installs = _read(tmp_path, files)
    assert _channels(installs) == ["4.1/edge", "3/stable"]
    assert installs[0].via == ("concierge-lxd.yaml",)


def test_charm_ci_without_concierge_reads_the_default_config(tmp_path: Path) -> None:
    spread = "project: x\nbackends:\n  it:\n    type: integration-test\n"
    files = {"spread.yaml": spread, "concierge.yaml": "juju:\n  channel: 3.6/stable\n"}
    assert _channels(_read(tmp_path, files)) == ["3.6/stable"]


def test_a_concierge_file_is_not_read_without_the_charm_ci_backend(tmp_path: Path) -> None:
    spread = "project: x\nbackends:\n  lxd:\n    type: adhoc\nenvironment:\n  CONCIERGE: c.yaml\n"
    files = {"spread.yaml": spread, "c.yaml": "juju:\n  channel: 3.6/stable\n"}
    assert _read(tmp_path, files) == []


def test_a_local_callee_bound_to_no_inputs_is_still_walked(tmp_path: Path) -> None:
    """any-charm: `on_pull_request.yaml` calls `./ci.yaml` with no `with:` at all."""
    caller = "on: pull_request\njobs:\n  t:\n    uses: ./.github/workflows/ci.yaml\n"
    callee = """\
on: workflow_call
jobs:
  it:
    steps:
      - uses: charmed-kubernetes/actions-operator@main
        with:
          juju-channel: 3.6/stable
"""
    files = {".github/workflows/on_pr.yaml": caller, ".github/workflows/ci.yaml": callee}
    assert _channels(_read(tmp_path, files)) == ["3.6/stable"]
