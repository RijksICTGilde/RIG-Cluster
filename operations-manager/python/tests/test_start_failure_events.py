"""A container that never starts: the portal names the cause instead of "check the logs".

The case is ``productie-web`` in ``rig-nginx-scz`` on a Fundament cluster, November 2026. The
start command was typed as ``sh, -c, "..."``, so runc looked for a program called ``sh,``.
The Failed event that said so was dropped, and the card only said the application crashed
and to look at logs that did not exist.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from opi.connectors.kubectl import KubectlConnector, _event_last_seen
from opi.forms.editables.validators import CommandLineValidator
from opi.services.event_interpreter import interpret_argocd_errors

RUNC_MESSAGE = (
    "Error: failed to create containerd task: failed to create shim task: OCI runtime create failed: "
    "runc create failed: unable to start container process: error during container init: "
    'exec: "sh,": executable file not found in $PATH'
)
POD = "productie-web-65c6c5988-9kls6"


def _card(errors: list[dict[str, str]]) -> list[dict[str, str]]:
    return interpret_argocd_errors(errors, deployment_name="productie", component_names=["web"])


def test_a_start_failure_names_the_cause_and_keeps_the_runc_text() -> None:
    errors = [
        {"resource": f"Pod/{POD}", "message": "back-off 5m0s restarting failed container=app pod=" + POD},
        {"resource": f"Event/{POD}", "message": f"[Failed] {RUNC_MESSAGE}"},
        {"resource": f"Event/{POD}", "message": "[BackOff] Back-off restarting failed container app in pod " + POD},
    ]

    card = _card(errors)

    titles = [entry["message"] for entry in card]
    assert "Container kan niet starten" in titles
    assert "Applicatie crasht herhaaldelijk" not in titles
    start = next(entry for entry in card if entry["message"] == "Container kan niet starten")
    assert start["resource"] == "web"
    assert start["original_message"] == RUNC_MESSAGE
    assert "komma" in start["suggestion"]


def test_a_real_crash_still_says_it_crashes() -> None:
    errors = [
        {"resource": f"Event/{POD}", "message": "[BackOff] Back-off restarting failed container app in pod " + POD}
    ]

    assert [entry["message"] for entry in _card(errors)] == ["Applicatie crasht herhaaldelijk"]


def test_an_exec_format_error_keeps_its_own_cause() -> None:
    message = 'runc create failed: unable to start container process: exec: "/app": exec format error'
    errors = [{"resource": f"Event/{POD}", "message": f"[Failed] {message}"}]

    assert [entry["message"] for entry in _card(errors)] == ["Verkeerd platform voor container image"]


@pytest.mark.parametrize("message", ["StartError", "RunContainerError", "CreateContainerError: failed"])
def test_each_remaining_start_failure_pattern_is_recognized(message: str) -> None:
    """RUNC_MESSAGE above only exercises half of the pattern's alternatives
    (executable file not found, runc create failed, the containerd/shim lines);
    the other three k8s waiting-reasons get their own case here."""
    errors = [{"resource": f"Event/{POD}", "message": f"[Failed] {message}"}]

    assert [entry["message"] for entry in _card(errors)] == ["Container kan niet starten"]


def test_a_start_failure_on_one_component_leaves_another_components_crash_alone() -> None:
    other_pod = "productie-worker-6d9f7b4c88-2xqzt"
    errors = [
        {"resource": f"Event/{POD}", "message": f"[Failed] {RUNC_MESSAGE}"},
        {
            "resource": f"Event/{other_pod}",
            "message": "[BackOff] Back-off restarting failed container app in pod " + other_pod,
        },
    ]

    result = interpret_argocd_errors(errors, deployment_name="productie", component_names=["web", "worker"])

    assert len(result) == 2
    crash = [entry for entry in result if entry["message"] == "Applicatie crasht herhaaldelijk"]
    assert len(crash) == 1
    assert crash[0]["resource"] == "worker"


def _event(reason: str, created: datetime, **extra: Any) -> dict[str, Any]:
    return {
        "type": "Warning",
        "reason": reason,
        "message": reason,
        "involvedObject": {"kind": "Pod", "name": POD},
        "metadata": {"creationTimestamp": created.strftime("%Y-%m-%dT%H:%M:%SZ")},
        **extra,
    }


async def _namespace_events(items: list[dict[str, Any]]) -> list[dict[str, str]]:
    connector = KubectlConnector()
    stdout = json.dumps({"items": items})
    with (
        patch.object(KubectlConnector, "is_connected", True),
        patch.object(connector, "_run_kubectl_command", AsyncMock(return_value=(stdout, "", 0))),
    ):
        return await connector.get_namespace_events("rig-nginx-scz", limit=30)


class TestEventLastSeen:
    """Direct tests of the fallback chain, without the connector's age filter in the way."""

    def test_series_wins_over_a_present_lasttimestamp(self) -> None:
        event = {
            "metadata": {"creationTimestamp": "2026-01-01T00:00:00Z"},
            "lastTimestamp": "2026-01-02T00:00:00Z",
            "series": {"lastObservedTime": "2026-01-03T00:00:00Z"},
        }
        assert _event_last_seen(event) == "2026-01-03T00:00:00Z"

    def test_lasttimestamp_wins_over_eventtime_and_creationtimestamp(self) -> None:
        event = {
            "metadata": {"creationTimestamp": "2026-01-01T00:00:00Z"},
            "lastTimestamp": "2026-01-02T00:00:00Z",
            "eventTime": "2026-01-04T00:00:00Z",
        }
        assert _event_last_seen(event) == "2026-01-02T00:00:00Z"

    def test_eventtime_is_used_when_series_and_lasttimestamp_are_both_absent(self) -> None:
        """events.k8s.io events (as opposed to the legacy core/v1 Event) leave
        lastTimestamp empty and have no series on a single occurrence either."""
        event = {
            "metadata": {"creationTimestamp": "2026-01-01T00:00:00Z"},
            "eventTime": "2026-01-04T00:00:00Z",
        }
        assert _event_last_seen(event) == "2026-01-04T00:00:00Z"

    def test_falls_back_to_creationtimestamp_when_nothing_else_is_set(self) -> None:
        event = {"metadata": {"creationTimestamp": "2026-01-01T00:00:00Z"}}
        assert _event_last_seen(event) == "2026-01-01T00:00:00Z"

    def test_an_empty_event_yields_an_empty_string(self) -> None:
        assert _event_last_seen({}) == ""


@pytest.mark.asyncio
async def test_a_repeating_event_counts_from_its_last_occurrence() -> None:
    now = datetime.now(UTC)
    stamp = (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    items = [_event("BackOff", now - timedelta(hours=5), lastTimestamp=stamp)]

    events = await _namespace_events(items)

    assert [event["reason"] for event in events] == ["BackOff"]
    assert events[0]["time"] == stamp


@pytest.mark.asyncio
async def test_an_events_api_event_uses_its_series() -> None:
    now = datetime.now(UTC)
    observed = (now - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    items = [
        _event("Failed", now - timedelta(hours=3), lastTimestamp=None, series={"lastObservedTime": observed}),
        _event("Stale", now - timedelta(hours=3)),
    ]

    events = await _namespace_events(items)

    assert [event["reason"] for event in events] == ["Failed"]


@pytest.mark.asyncio
async def test_events_come_newest_first_by_last_occurrence() -> None:
    now = datetime.now(UTC)
    items = [
        _event("Older", now - timedelta(minutes=30)),
        _event("Repeating", now - timedelta(minutes=90), lastTimestamp=now.strftime("%Y-%m-%dT%H:%M:%SZ")),
    ]

    events = await _namespace_events(items)

    assert [event["reason"] for event in events] == ["Repeating", "Older"]


@pytest.mark.parametrize("line", ['sh, -c, "date > /data/index.html"', '["sh", "-c", "date"]'])
def test_a_list_typed_into_the_start_command_is_refused(line: str) -> None:
    assert "komma" in CommandLineValidator().validate(line)[0]


@pytest.mark.parametrize("line", ['sh -c "date > /data/index.html"', "echo hello, world"])
def test_an_ordinary_start_command_passes(line: str) -> None:
    assert CommandLineValidator().validate(line) == []


def test_the_list_check_only_applies_to_a_typed_string() -> None:
    """The project file stores ``command`` as a list; that is not what a user typed into
    the field and must not be judged by the same comma/bracket rule."""
    assert CommandLineValidator().validate(["sh,", "-c", "date"]) == []
