"""`charmtally --version` must report the installed version.

`__init__` has resolved `__version__` from package metadata since the start and
llm_score records it as `scanner_version`, but the CLI never exposed it, so the
version stamped into a scored file could not be read off the tool that wrote it.
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout

import pytest

from .. import __version__
from ..cli import main


def test_version_flag_prints_version_and_exits_zero():
    out = io.StringIO()
    with pytest.raises(SystemExit) as exc, redirect_stdout(out):
        main(["--version"])
    assert exc.value.code == 0
    assert out.getvalue().strip() == f"charmtally {__version__}"


def test_version_flag_needs_no_subcommand():
    """argparse requires a subcommand; --version must short-circuit that."""
    with pytest.raises(SystemExit) as exc, redirect_stdout(io.StringIO()):
        main(["--version"])
    assert exc.value.code == 0
