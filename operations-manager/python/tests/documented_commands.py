"""The operator's script in the feature doc, split into the launcher and the flags.

The tests that read those lines have to step over the launcher before they can hand the rest to
an argument parser. One module owns it, so a change to the prefix cannot leave one test file
reading a list that is silently empty. Why there is a launcher at all: ``scripts/README.md``.
"""

from __future__ import annotations

import shlex
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
FEATURE_DOC = REPO / "features" / "sops-sleutel-roteren.md"

#: How every documented invocation of an ``opi``-importing entry starts.
LAUNCHER = "uv run --project operations-manager/python python"

#: And how the one entry that does not import ``opi`` starts.
BARE_PYTHON = "python3"


def documented_lines(*scripts: str, doc: Path = FEATURE_DOC) -> list[str]:
    """Every line of ``doc`` that invokes one of ``scripts``, launcher and all."""
    prefixes = tuple(f"{prefix} scripts/{name}" for name in scripts for prefix in (LAUNCHER, BARE_PYTHON))
    return [line.strip() for line in doc.read_text().splitlines() if line.strip().startswith(prefixes)]


def flags(line: str) -> list[str]:
    """The arguments of a documented line: everything after the launcher and the script path."""
    tokens = shlex.split(line)
    for index, token in enumerate(tokens):
        if token.startswith("scripts/"):
            return tokens[index + 1 :]
    raise AssertionError(f"no script path in this line: {line}")
