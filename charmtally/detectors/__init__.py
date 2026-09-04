"""Run a single feature's detectors against a charm tree.

Five kinds run per Python file in scope:
    import         — AST: matches `import X` and `from X import Y` (with optional names filter)
    call           — AST: matches `*.<attr>(...)` where <attr> is the trailing dotted suffix
    call-kwarg     — AST: as `call`, but only when a named keyword argument is
                     passed one of a listed set of literal values
    observe-event  — regex: matches `observe(... on.<snake_name>(...)|on['<snake_name>'] ...)`
                     for each given event class (translated CamelCase→snake_case,
                     dropping trailing 'Event')
    regex          — raw multiline regex over file contents

Four more run per Python file and back the architecture axis:
    ast-init-call               — a `self.X(...)` call inside `__init__`
    ast-observe-shared-handler  — one handler bound to N distinct events
    ast-shared-method           — N `_on_*` handlers delegating to one method
    ast-subclass-module         — a ClassDef base resolves, through the file's
                                   own import table, to a dotted path under a
                                   configured module root

Four are file-independent: they read the charm root directly rather than the
Python files `_select_files` returns.
    yaml-key           — a mapping key present in YAML matching a glob
    pytest-config-key  — a pytest setting in pyproject/pytest.ini/setup.cfg/tox.ini
    requires-interface — an interface named in the metadata `requires:` block
    relation-count     — bucket the charm by its requires/provides/peers count

The package is split along those three groups: `_files` selects, reads and
parses what the detectors run over, `_ast` and `_config` hold the kinds
themselves, and `_registry` dispatches to them and owns `detect_feature`.
"""

from __future__ import annotations

from ._files import CharmSource, Evidence, SourceFile
from ._registry import detect_feature

__all__ = ["CharmSource", "Evidence", "SourceFile", "detect_feature"]
