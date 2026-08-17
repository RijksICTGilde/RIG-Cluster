"""WP6: a disabled component (scaled to zero) must not be reported as busy.

``gather_deployment_errors`` collects the raw problems a deployment card shows. A
component the watcher (or a user) disabled runs at 0 replicas -- its end state, not a
wait state -- yet ArgoCD can still carry a "waiting for rollout"/"pods being created"
(Progressing) message or a stale old-pod entry for it. The card already surfaces the
component as *disabled* separately, so these entries are state-vs-progress noise and are
dropped when the component is in ``disabled_components``.
"""

from __future__ import annotations

from typing import Any

import pytest
from opi.services.deployment_diagnostics import gather_deployment_errors


class _NoTreeArgo:
    """ArgoConnector stub: no resource tree (keeps the test to status.resources)."""

    async def get_application_resource_tree(self, app_name: str) -> list[dict[str, Any]]:
        return []


def _status_with_progressing(deployment_name: str, component: str) -> dict[str, Any]:
    """An Application status whose only non-healthy resource is one component's
    Deployment, reported Progressing with a "pods being created" message."""
    return {
        "status": {
            "health": {"status": "Progressing"},
            "resources": [
                {
                    "kind": "Deployment",
                    "name": f"{deployment_name}-{component}",
                    "health": {
                        "status": "Progressing",
                        "message": "Waiting for rollout to finish: 0 of 1 updated replicas are available...",
                    },
                }
            ],
        }
    }


@pytest.mark.asyncio
async def test_disabled_component_progressing_entry_dropped() -> None:
    status = _status_with_progressing("dep", "typesense")
    errors = await gather_deployment_errors(
        argo=_NoTreeArgo(),
        kubectl=None,
        app_name="proj-dep",
        base_namespace="ns",
        cluster="c",
        deployment_name="dep",
        status_data=status,
        disabled_components=frozenset({"typesense"}),
    )
    assert errors == []


@pytest.mark.asyncio
async def test_enabled_component_progressing_entry_kept() -> None:
    """The same entry for a component that is NOT disabled is a real progress signal
    and stays -- the filter must not swallow live deployments."""
    status = _status_with_progressing("dep", "typesense")
    errors = await gather_deployment_errors(
        argo=_NoTreeArgo(),
        kubectl=None,
        app_name="proj-dep",
        base_namespace="ns",
        cluster="c",
        deployment_name="dep",
        status_data=status,
        disabled_components=frozenset(),
    )
    assert len(errors) == 1
    assert errors[0]["resource"] == "Deployment/dep-typesense"


@pytest.mark.asyncio
async def test_app_level_entry_survives_disabled_filter() -> None:
    """An app-level entry (SyncOperation, no deployment prefix) is not a component
    resource, so disabling a component must not drop it."""
    status = {
        "status": {
            "health": {"status": "Degraded"},
            "operationState": {"phase": "Failed", "message": "sync failed"},
        }
    }
    errors = await gather_deployment_errors(
        argo=_NoTreeArgo(),
        kubectl=None,
        app_name="proj-dep",
        base_namespace="ns",
        cluster="c",
        deployment_name="dep",
        status_data=status,
        disabled_components=frozenset({"typesense"}),
    )
    assert [e["resource"] for e in errors] == ["SyncOperation"]
