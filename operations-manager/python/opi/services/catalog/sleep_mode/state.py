"""Read and write per-deployment sleep runtime state (``deployments[].sleep``).

This is OPI-managed runtime state, not user config -- the same category as
``disabled``/``disabled-reason`` on a deployment component. It lives on the deployment
object under a ``sleep`` block and is mutated in place, mirroring
``project_file_handler.set_deployment_component_disabled``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: The three sleep states. ``waking`` is the transitional state where both the app and
#: the waker run, so there is no gap while the app cold-starts.
STATE_AWAKE = "awake"
STATE_SLEEPING = "sleeping"
STATE_WAKING = "waking"

VALID_STATES = (STATE_AWAKE, STATE_SLEEPING, STATE_WAKING)

#: The ``deployments[]`` key this state lives under. Declared here so the service can
#: hand it to generic code (``deployment_runtime_keys``) without a second literal.
DEPLOYMENT_KEY = "sleep"


@dataclass
class SleepState:
    """A deployment's sleep state as stored under ``deployments[].sleep``."""

    state: str = STATE_AWAKE
    expires_at: str | None = None
    wake_token: str | None = None


def _find_deployment(project_data: dict[str, Any], deployment_name: str) -> dict[str, Any] | None:
    for deployment in project_data.get("deployments", []) or []:
        if deployment.get("name") == deployment_name:
            return deployment
    return None


def read(project_data: dict[str, Any], deployment_name: str) -> SleepState:
    """Read a deployment's sleep state; defaults to ``awake`` when absent."""
    deployment = _find_deployment(project_data, deployment_name)
    sleep = (deployment or {}).get(DEPLOYMENT_KEY) or {}
    return SleepState(
        state=sleep.get("state", STATE_AWAKE),
        expires_at=sleep.get("expires-at"),
        wake_token=sleep.get("wake-token"),
    )


def write(project_data: dict[str, Any], deployment_name: str, sleep_state: SleepState) -> bool:
    """Write a deployment's sleep state in place; True if the deployment was found.

    An ``awake`` state with no expiry and no token clears the whole ``sleep`` block, so
    a deployment that never sleeps carries no runtime noise in its project file.
    """
    deployment = _find_deployment(project_data, deployment_name)
    if deployment is None:
        logger.warning(f"Deployment '{deployment_name}' not found for sleep-state update")
        return False

    if sleep_state.state == STATE_AWAKE and not sleep_state.expires_at and not sleep_state.wake_token:
        deployment.pop(DEPLOYMENT_KEY, None)
        return True

    sleep: dict[str, Any] = {"state": sleep_state.state}
    if sleep_state.expires_at:
        sleep["expires-at"] = sleep_state.expires_at
    if sleep_state.wake_token:
        sleep["wake-token"] = sleep_state.wake_token
    deployment[DEPLOYMENT_KEY] = sleep
    return True
