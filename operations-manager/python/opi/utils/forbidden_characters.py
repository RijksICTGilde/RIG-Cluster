"""The last check for dangerous characters in a project file.

Every write path to a project file passes ``GitProjectStore._validate``, including the paths
that know no editable: an approval with a raw JSON body, a service config PUT and a
programmatic write. A pattern in the schema covers only the fields that have one, so this
check holds for EVERY string value. It changes nothing: no trim, no length, no normalisation,
because a sanitizer running after the validation alters an approved value (CWE-180). See
`plans/invoer-eerst-schoon-dan-getoetst.md` task 6.
"""

from __future__ import annotations

import re
from typing import Any, Final

#: NUL, Cc-tekens except tab/newline, and bidi-controls (Trojan Source). Tabs/newlines
#: allowed for heredoc scripts.
_FORBIDDEN: Final = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")

#: How a refused character reaches the message: by name where there is one, otherwise as a
#: code point. Never the value itself, because it can be a plain secret and this message is
#: logged and returned.
_NAMES: Final[dict[str, str]] = {
    "\x00": "NUL",
    "\r": "CR",
}


def find_forbidden_characters(data: Any) -> list[str]:
    """The paths whose string value holds a forbidden character, empty when there are none.

    The path is shaped like ``components[0].user-env-vars``, so a user knows which field to
    change without the value ending up in the message.
    """
    violations: list[str] = []
    _walk(data, "", violations)
    return violations


def _codepoint(char: str) -> str:
    return _NAMES.get(char, f"U+{ord(char):04X}")


def _escape(path: str) -> str:
    """The path with its own forbidden characters as code points.

    A key holding a bidi character ends up in the path, and a message that renders it
    literally shows exactly the reordering this check is meant to prevent.
    """
    return _FORBIDDEN.sub(lambda match: _codepoint(match.group()), path)


def _walk(node: Any, path: str, violations: list[str]) -> None:
    if isinstance(node, str):
        found = sorted({match.group() for match in _FORBIDDEN.finditer(node)})
        if found:
            named = ", ".join(_codepoint(char) for char in found)
            violations.append(f"{_escape(path) or '(wortel)'} ({named})")
    elif isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path else str(key)
            # The key as well: an env variable name holding a bidi character reads
            # differently from how it lands in the manifest.
            if isinstance(key, str):
                _walk(key, child, violations)
            _walk(value, child, violations)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _walk(value, f"{path}[{index}]", violations)
