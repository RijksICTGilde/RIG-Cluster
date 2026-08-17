"""The durations sleep-mode offers, owned by the service instead of by the form layer.

The two lists used to sit in ``opi/forms/visualizers/providers.py``, next to every other
service's options, while sleep-mode's cluster defaults already lived in this package. A
service that decides how it behaves should also decide what it offers, so both now sit
together and the providers just ask.

That also makes the per-cluster extra possible. The sandbox needs a five-minute sleep to
make a sleep/wake cycle observable inside a test run; production must not offer that,
because five minutes is a footgun on a real deployment. So the extras are declared per
cluster and added to the shared list, rather than the list being made configurable in
general.
"""

from __future__ import annotations

from typing import Any

#: Offered everywhere. The value is a duration string that ``parse_duration`` accepts.
_SLEEP_AFTER_DEPLOY: list[tuple[str, str]] = [
    ("4h", "4 uur"),
    ("8h", "8 uur"),
    ("12h", "12 uur"),
    ("24h", "1 dag"),
    ("48h", "2 dagen"),
    ("72h", "3 dagen"),
    ("168h", "7 dagen"),
]

_SLEEP_AFTER_WAKE: list[tuple[str, str]] = [
    ("30m", "30 minuten"),
    ("1h", "1 uur"),
    ("2h", "2 uur"),
    ("4h", "4 uur"),
    ("8h", "8 uur"),
    ("24h", "1 dag"),
]

#: Extra choices a specific cluster offers, shortest first so they sort in front.
#:
#: Only the sandbox, and only to keep a sleep/wake cycle observable within a test: the
#: sweeper there runs every minute, so a five-minute deadline is reached while someone is
#: still watching. Deliberately not offered on production, where picking it would put a
#: real deployment to sleep five minutes after every deploy.
_CLUSTER_EXTRA_DURATIONS: dict[str, list[tuple[str, str]]] = {
    "sandboxed-local": [("5m", "5 minuten (alleen sandbox, voor tests)")],
}


def _options_for(base: list[tuple[str, str]], cluster: str | None) -> list[dict[str, Any]]:
    extra = _CLUSTER_EXTRA_DURATIONS.get(cluster or "", [])
    return [{"value": value, "label": label} for value, label in [*extra, *base]]


def sleep_after_deploy_options(cluster: str | None = None) -> list[dict[str, Any]]:
    """How long after a deploy a deployment may go to sleep."""
    return _options_for(_SLEEP_AFTER_DEPLOY, cluster)


def sleep_after_wake_options(cluster: str | None = None) -> list[dict[str, Any]]:
    """How long a woken deployment stays awake before the deadline is set again."""
    return _options_for(_SLEEP_AFTER_WAKE, cluster)
