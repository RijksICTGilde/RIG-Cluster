"""De laatste controle op gevaarlijke tekens in een projectbestand.

Elke schrijfweg naar een projectbestand komt langs ``GitProjectStore._validate``, ook de
wegen die geen editable kennen: een approval met een ruwe JSON-body, een service-config
PUT en programmatisch schrijven. Een pattern in het schema dekt alleen de velden die er
een hebben, dus deze toets geldt voor ELKE stringwaarde. Hij verandert niets: geen trim,
geen lengte, geen normalisatie, want een sanitizer die na de validatie draait wijzigt een
goedgekeurde waarde (CWE-180). Zie plans/invoer-eerst-schoon-dan-getoetst.md taak 6.
"""

from __future__ import annotations

import re
from typing import Any, Final

#: NUL, Cc-tekens except tab/newline, and bidi-controls (Trojan Source). Tabs/newlines
#: allowed for heredoc scripts.
_FORBIDDEN: Final = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")

#: Hoe een geweigerd teken in de melding komt: bij naam waar die bestaat, anders als
#: codepunt. De waarde zelf nooit, want die kan een plat secret zijn en deze melding
#: wordt gelogd en teruggegeven.
_NAMES: Final[dict[str, str]] = {
    "\x00": "NUL",
    "\r": "CR",
}


def find_forbidden_characters(data: Any) -> list[str]:
    """De paden met een stringwaarde die een verboden teken bevat, leeg als er geen zijn.

    Het pad is ``components[0].user-env-vars``-vormig, zodat een gebruiker weet welk veld
    hij moet aanpassen zonder dat de waarde in de melding komt.
    """
    violations: list[str] = []
    _walk(data, "", violations)
    return violations


def _codepoint(char: str) -> str:
    return _NAMES.get(char, f"U+{ord(char):04X}")


def _escape(path: str) -> str:
    """Het pad met zijn eigen verboden tekens als codepunt.

    Een sleutel met een bidi-teken komt in het pad terecht, en een melding die hem
    letterlijk weergeeft toont precies de herordening die deze toets moet voorkomen.
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
            # Ook de sleutel: een env-variabelenaam met een bidi-teken leest anders dan
            # hij in het manifest terechtkomt.
            if isinstance(key, str):
                _walk(key, child, violations)
            _walk(value, child, violations)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _walk(value, f"{path}[{index}]", violations)
