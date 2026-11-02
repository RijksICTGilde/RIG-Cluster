"""The operator's script in the feature doc, split into the launcher and the flags.

The tests that read those lines have to step over the launcher before they can hand the rest to
an argument parser. One module owns it, so a change to the prefix cannot leave one test file
reading a list that is silently empty. Why there is a launcher at all: ``scripts/README.md``.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
FEATURE_DOC = REPO / "features" / "sops-sleutel-roteren.md"

#: How every documented invocation of an ``opi``-importing entry starts.
LAUNCHER = "uv run --project operations-manager/python python"

#: And how the one entry that does not import ``opi`` starts.
BARE_PYTHON = "python3"


#: A step whose whole content is one backticked command: a list marker or nothing in front of
#: it, and at most one punctuation mark behind it. Mid-sentence backticks are prose, and the
#: docs use those for counter-examples ("``python3 scripts/x.py`` stops on ModuleNotFoundError").
_INLINE_STEP = re.compile(r"^\s*(?:\d+\.|[-*])?\s*`([^`]+)`[.,;:]?$")

_LAUNCHER_STARTS = tuple(f"{start} " for start in (shlex.split(LAUNCHER)[0], BARE_PYTHON))


def command_lines(doc: Path) -> list[str]:
    """Every pasteable command in ``doc``: the lines of a ```bash block, plus inline steps.

    Inline too: an operator pastes a numbered step as readily as a block line, and five broken
    KEK commands sat in such steps past a block-only gate (RC-334).
    """
    commands: list[str] = []
    inside = False
    for raw in doc.read_text().splitlines():
        if raw.startswith("```"):
            inside = raw.startswith("```bash")
            continue
        if inside:
            line = raw.strip()
            if line and not line.startswith("#"):
                commands.append(line)
            continue
        step = _INLINE_STEP.match(raw)
        if step and step.group(1).startswith(_LAUNCHER_STARTS):
            commands.append(step.group(1).strip())
    return commands


def documented_lines(*scripts: str, doc: Path = FEATURE_DOC) -> list[str]:
    """Every command in ``doc`` that invokes one of ``scripts``, launcher and all."""
    prefixes = tuple(f"{prefix} scripts/{name}" for name in scripts for prefix in (LAUNCHER, BARE_PYTHON))
    return [line for line in command_lines(doc) if line.startswith(prefixes)]


def flags(line: str) -> list[str]:
    """The arguments of a documented line: everything after the launcher and the script path."""
    tokens = shlex.split(line)
    for index, token in enumerate(tokens):
        if token.startswith("scripts/"):
            return tokens[index + 1 :]
    raise AssertionError(f"no script path in this line: {line}")
