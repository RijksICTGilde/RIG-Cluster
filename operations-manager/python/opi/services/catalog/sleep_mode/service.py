"""Sleep-mode state transitions -- the one place that changes sleep state.

These are pure functions over ``project_data``: they move a deployment between
``awake`` / ``sleeping`` / ``waking`` and set the deadline, but never touch git. The
web route, the API route and the sweeper all call these and then commit + reprocess,
so the transition logic lives in exactly one place and is unit-testable with a fixed
``now``. Every function is idempotent: a transition that does not apply returns False
and changes nothing, which is what keeps the git commit-storm in check.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from opi.services.catalog.sleep_mode.state import (
    STATE_AWAKE,
    STATE_SLEEPING,
    STATE_WAKING,
    SleepState,
    read,
    write,
)

if TYPE_CHECKING:
    from datetime import datetime, timedelta


def to_sleeping(project_data: dict, deployment_name: str, encrypted_token: str | None) -> bool:
    """awake -> sleeping. Stores the (already-encrypted) wake token if one is given.

    A deployment without a waker (no web component) passes ``None`` and simply sleeps.
    No-op unless currently awake.
    """
    current = read(project_data, deployment_name)
    if current.state != STATE_AWAKE:
        return False
    return write(
        project_data,
        deployment_name,
        SleepState(state=STATE_SLEEPING, expires_at=None, wake_token=encrypted_token),
    )


def begin_wake(project_data: dict, deployment_name: str, now: datetime, waking_timeout: timedelta) -> bool:
    """sleeping -> waking. Sets a stuck-detection deadline (now + waking_timeout).

    This is the only transition the wake endpoints accept, and only from ``sleeping``;
    everything else (already waking/awake) is an idempotent no-op returning False, so a
    leaked token or a repeated call cannot cause a second commit.
    """
    current = read(project_data, deployment_name)
    if current.state != STATE_SLEEPING:
        return False
    return write(
        project_data,
        deployment_name,
        SleepState(
            state=STATE_WAKING,
            expires_at=(now + waking_timeout).isoformat(),
            wake_token=current.wake_token,
        ),
    )


def to_awake(project_data: dict, deployment_name: str, now: datetime, sleep_after_wake: timedelta) -> bool:
    """waking|sleeping -> awake. Sets the next sleep deadline and drops the wake token.

    Used both for the waking -> awake cleanup after the app is back and for the
    sweeper reverting a stuck ``waking``. No-op if already awake.
    """
    current = read(project_data, deployment_name)
    if current.state == STATE_AWAKE:
        return False
    return write(
        project_data,
        deployment_name,
        SleepState(state=STATE_AWAKE, expires_at=(now + sleep_after_wake).isoformat(), wake_token=None),
    )


def set_sleep_deadline(project_data: dict, deployment_name: str, now: datetime, sleep_after: timedelta) -> bool:
    """Reset a deployment to awake with a fresh sleep deadline (now + sleep_after).

    Called on create and on every image update, so activity pushes the deadline out.
    Always writes (the deadline moved), dropping any stale token.
    """
    return write(
        project_data,
        deployment_name,
        SleepState(state=STATE_AWAKE, expires_at=(now + sleep_after).isoformat(), wake_token=None),
    )
