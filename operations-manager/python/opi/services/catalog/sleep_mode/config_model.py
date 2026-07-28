"""Typed config model for the ``sleep-mode`` service.

The project-level ``services/sleep-mode/config`` block is validated against this
model (convert-then-validate, like every other service). The cluster-wide default
is merged in *before* validation by ``config.load`` -- the service owns that default
itself (see ``config._CLUSTER_DEFAULTS``), it is not scattered into
``core/cluster_config.py``.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Accepted wake modes. ``auto`` wakes on the first browser GET, ``confirm`` shows a
#: button, ``manual`` only informs (an admin wakes via the UI/API).
WakeMode = Literal["auto", "confirm", "manual"]

_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$")
_DURATION_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(value: str) -> timedelta:
    """Parse a compact duration (``48h``, ``90m``, ``30s``, ``2d``) to a timedelta.

    Raises ``ValueError`` on anything else, so a typo in the project file fails loud
    at config load rather than silently disabling the deadline.
    """
    match = _DURATION_RE.match(value)
    if not match:
        raise ValueError(f"Invalid duration '{value}': expected a number followed by s, m, h or d (e.g. 48h)")
    amount, unit = int(match.group(1)), match.group(2)
    return timedelta(seconds=amount * _DURATION_UNIT_SECONDS[unit])


def validate_match_pattern(pattern: str) -> str:
    """Validate a single match pattern, kept deliberately simple: an exact name, or a
    single ``*`` at the start ("ends with") or end ("starts with"). No mid-string ``*``,
    no ``?`` or character classes. Returns the trimmed pattern, or raises ``ValueError``.
    """
    p = pattern.strip()
    if not p:
        raise ValueError("A match pattern may not be empty")
    if any(c in p for c in "?[]"):
        raise ValueError(f"Invalid match pattern '{p}': ? and [ ] are not allowed")
    if p.count("*") > 1 or (p.count("*") == 1 and not (p.startswith("*") or p.endswith("*"))):
        raise ValueError(
            f"Invalid match pattern '{p}': use 'prefix*' (starts with), '*suffix' (ends with) or an exact name"
        )
    return p


class SleepModeConfig(BaseModel):
    """Sleep-mode config for a project, after the cluster default is merged in.

    This doubles as the runtime object callers use: ``matches(name)`` answers whether
    a deployment is in scope, and the parsed durations drive the deadlines.
    """

    # extra="forbid" turns a config typo into a loud failure; populate_by_name lets
    # tests build it with the Python field names while the file uses hyphens.
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    enabled: bool = False
    match: list[str] = Field(default_factory=list)
    sleep_after_deploy: str = Field(default="48h", alias="sleep-after-deploy")
    sleep_after_wake: str = Field(default="1h", alias="sleep-after-wake")
    waker: bool = True
    waker_component: str | None = Field(default=None, alias="waker-component")
    wake_mode: WakeMode = Field(default="auto", alias="wake-mode")
    title: str | None = None
    description: str = ""

    @field_validator("sleep_after_deploy", "sleep_after_wake")
    @classmethod
    def _validate_duration(cls, value: str) -> str:
        # Validate but keep the original string; callers parse on demand.
        parse_duration(value)
        return value

    @field_validator("match")
    @classmethod
    def _validate_match(cls, value: list[str]) -> list[str]:
        # Keep matching simple (starts-with / ends-with / exact); fail loud on fancy globs.
        return [validate_match_pattern(pattern) for pattern in value]

    def matches(self, deployment_name: str) -> bool:
        """Whether ``deployment_name`` is in scope, per the ``match`` globs (case-sensitive)."""
        import fnmatch

        return any(fnmatch.fnmatchcase(deployment_name, pattern) for pattern in self.match)

    @property
    def sleep_after_deploy_delta(self) -> timedelta:
        return parse_duration(self.sleep_after_deploy)

    @property
    def sleep_after_wake_delta(self) -> timedelta:
        return parse_duration(self.sleep_after_wake)
