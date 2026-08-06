"""A deployment with no pods does not offer things that need pods.

Sleeping was visible only as a separate block below the card, while the card itself kept
offering "View Logs". There are no logs in a deployment that is asleep, so the button
promises something it cannot deliver.

Driven by the same service facts the state block renders (``expects_no_application_pods``
from ``collect_deployment_state``), so a second service that parks a deployment in a
situation gets this behaviour without touching the template.
"""

from __future__ import annotations

import pathlib

import opi

_CARD = pathlib.Path(opi.__file__).parent / "templates/project-details/_argocd-deployment-card.html.j2"


def test_the_logs_button_depends_on_the_service_facts() -> None:
    source = _CARD.read_text(encoding="utf-8")

    assert "expects_no_application_pods" in source
    assert "not expects_no_pods" in source


def test_the_card_names_no_service() -> None:
    """Generic on purpose: the card must not know that sleep-mode exists, or the next
    service that parks a deployment needs a second condition here."""
    source = _CARD.read_text(encoding="utf-8")

    for name in ("sleep-mode", "sleep_mode", "slaapstand", "waker"):
        assert name not in source, f"the card should not know about {name}"


def test_the_wake_action_has_the_same_weight_as_the_other_deployment_actions() -> None:
    """Waking is not more important than editing images or reprocessing; it just happens
    less often. A primary button next to two secondary ones reads as the thing to do."""
    from opi.services.catalog.sleep_mode.actions import sleep_actions

    project = {"name": "p", "deployments": [{"name": "d", "cluster": "sandboxed-local"}]}
    for state in ({}, {"sleep": {"state": "sleeping"}}):
        deployment = {"name": "d", "cluster": "sandboxed-local", **state}
        for action in sleep_actions({**project, "deployments": [deployment]}, "d"):
            assert action.kind == "secondary", action.label
