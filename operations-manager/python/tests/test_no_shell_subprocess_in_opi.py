"""Guard: nothing in ``opi/`` starts a subprocess through a shell.

A shell string means the arguments are data for an interpreter: a manifest with a secret
ends up in the argv of ``/bin/sh -c`` (readable in the process table) and a value with
shell syntax in it is executed. Both have already happened here, in
``KubectlConnector._run_kubectl_command`` (RC-302) and in the pg_dump clone
(``tests/test_pgdump_command_injection.py``). Every connector passes its arguments as a
list to ``create_subprocess_exec`` and its input through a pipe.

There is no exception list: a new shell call is a finding, not a case to add here.
"""

from __future__ import annotations

from pathlib import Path

import opi

FORBIDDEN = ("create_subprocess_shell", "shell=True")


def test_opi_starts_no_subprocess_through_a_shell() -> None:
    offenders = [
        f"{source.relative_to(Path(opi.__file__).parent.parent)}: {pattern}"
        for source in sorted(Path(opi.__file__).parent.rglob("*.py"))
        for pattern in FORBIDDEN
        if pattern in source.read_text(encoding="utf-8")
    ]

    assert not offenders, "pass the arguments as a list to create_subprocess_exec: " + ", ".join(offenders)
