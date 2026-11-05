"""Welke zadctl-versies deze API ondersteunt.

zadctl stuurt ``User-Agent: zadctl/X.Y.Z``. Twee settings bepalen wat daarmee gebeurt:
``ZADCTL_MIN_VERSION`` (lager wordt geweigerd met 426) en ``ZADCTL_RECOMMENDED_VERSION``
(lager gaat door, met een waarschuwing in een header). Leeg betekent uit.

Een verzoek zonder zadctl-User-Agent valt erbuiten: andere clients, en zadctl-versies van
voor deze afspraak, die ``python-httpx/...`` sturen. Die zijn niet van elkaar te
onderscheiden, en ze weigeren zou elke script treffen dat de API direct aanroept.
"""

from __future__ import annotations

import re

_USER_AGENT = re.compile(r"^zadctl/(\d+)\.(\d+)\.(\d+)")
_SETTING = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

UPGRADE_URL = "https://github.com/RijksICTGilde/zad-cli/releases/latest"


def version_from_user_agent(user_agent: str) -> tuple[int, int, int] | None:
    """De zadctl-versie uit een User-Agent, of None als het geen zadctl is."""
    match = _USER_AGENT.match(user_agent)
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def parse_setting(name: str, value: str) -> tuple[int, int, int] | None:
    """Een gezette grens als tuple, None als hij leeg is; anders dan X.Y.Z faalt de opstart."""
    if not value:
        return None
    match = _SETTING.match(value)
    if not match:
        raise RuntimeError(f"{name} moet de vorm X.Y.Z hebben, niet {value!r}")
    return int(match[1]), int(match[2]), int(match[3])
