"""Tests for opi.services.deployment_diagnostics."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.api.v2.models import ErrorCategory
from opi.services.deployment_diagnostics import (
    categorize_error,
    conditions_to_errors,
    gather_deployment_errors,
    gather_sync_deviations,
    summarize_component_pods,
)


def _argo_mock(tree_nodes: list[dict[str, Any]] | None = None, raises: bool = False) -> MagicMock:
    mock = MagicMock()
    if raises:
        mock.get_application_resource_tree = AsyncMock(side_effect=RuntimeError("argo down"))
    else:
        mock.get_application_resource_tree = AsyncMock(return_value=tree_nodes or [])
    return mock


def _kubectl_mock(
    events: list[dict[str, str]] | None = None,
    events_raises: bool = False,
) -> MagicMock:
    mock = MagicMock()
    if events_raises:
        mock.get_namespace_events = AsyncMock(side_effect=RuntimeError("kubectl events down"))
    else:
        mock.get_namespace_events = AsyncMock(return_value=events or [])
    return mock


# ---------------------------------------------------------------------------
# gather_deployment_errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestConditionsToErrors:
    """The cheap, always-run app-level conditions reader."""

    def test_comparison_error_condition_becomes_error(self) -> None:
        status_data = {
            "status": {
                "health": {"status": "Healthy"},
                "sync": {"status": "Unknown"},
                "conditions": [
                    {"type": "ComparisonError", "message": "failed to generate manifests: exit status 1"},
                ],
            }
        }
        errors = conditions_to_errors(status_data)
        assert errors == [{"resource": "ComparisonError", "message": "failed to generate manifests: exit status 1"}]

    def test_conditions_without_message_are_skipped(self) -> None:
        status_data = {"status": {"conditions": [{"type": "ComparisonError", "message": ""}]}}
        assert conditions_to_errors(status_data) == []

    def test_no_conditions_is_empty(self) -> None:
        assert conditions_to_errors({"status": {}}) == []
        assert conditions_to_errors({}) == []


class TestGatherDeploymentErrors:
    """Behaviour of gather_deployment_errors."""

    async def test_status_resources_degraded_become_errors(self) -> None:
        status_data = {
            "status": {
                "health": {"status": "Degraded"},
                "resources": [
                    {"kind": "Deployment", "name": "frontend", "health": {"status": "Degraded", "message": "boom"}},
                    {"kind": "Service", "name": "frontend", "health": {"status": "Healthy"}},
                ],
            }
        }
        with patch("opi.services.deployment_diagnostics.get_prefixed_namespace", return_value="ns"):
            errors = await gather_deployment_errors(
                argo=_argo_mock(),
                kubectl=_kubectl_mock(),
                app_name="my-app",
                base_namespace="ns",
                cluster="local",
                deployment_name="prod",
                status_data=status_data,
            )
        assert {"resource": "Deployment/frontend", "message": "boom"} in errors
        assert not any(e["resource"] == "Service/frontend" for e in errors)

    async def test_status_resources_progressing_with_message_become_errors(self) -> None:
        status_data = {
            "status": {
                "health": {"status": "Progressing"},
                "resources": [
                    {"kind": "Pod", "name": "p1", "health": {"status": "Progressing", "message": "still pulling"}},
                    {"kind": "Pod", "name": "p2", "health": {"status": "Progressing"}},
                ],
            }
        }
        with patch("opi.services.deployment_diagnostics.get_prefixed_namespace", return_value="ns"):
            errors = await gather_deployment_errors(
                argo=_argo_mock(),
                kubectl=_kubectl_mock(),
                app_name="my-app",
                base_namespace="ns",
                cluster="local",
                deployment_name="prod",
                status_data=status_data,
            )
        assert {"resource": "Pod/p1", "message": "still pulling"} in errors
        assert not any(e["resource"] == "Pod/p2" for e in errors)

    async def test_resource_tree_pod_messages_become_errors(self) -> None:
        tree = [
            {
                "kind": "Pod",
                "name": "frontend-abc",
                "health": {"status": "Degraded", "message": "ImagePullBackOff"},
                "createdAt": "2026-04-22T10:00:00Z",
            },
            {"kind": "Service", "name": "frontend", "health": {"status": "Healthy", "message": ""}},
        ]
        status_data = {"status": {"health": {"status": "Degraded"}}}
        with patch("opi.services.deployment_diagnostics.get_prefixed_namespace", return_value="ns"):
            errors = await gather_deployment_errors(
                argo=_argo_mock(tree_nodes=tree),
                kubectl=_kubectl_mock(),
                app_name="my-app",
                base_namespace="ns",
                cluster="local",
                deployment_name="prod",
                status_data=status_data,
            )
        pod_err = next(e for e in errors if e["resource"] == "Pod/frontend-abc")
        assert pod_err["message"] == "ImagePullBackOff"
        assert pod_err["timestamp"] == "2026-04-22T10:00:00Z"
        assert not any(e["resource"].startswith("Service/") for e in errors)

    async def test_resource_tree_failure_is_swallowed(self) -> None:
        status_data = {"status": {"health": {"status": "Degraded"}}}
        with patch("opi.services.deployment_diagnostics.get_prefixed_namespace", return_value="ns"):
            errors = await gather_deployment_errors(
                argo=_argo_mock(raises=True),
                kubectl=_kubectl_mock(),
                app_name="my-app",
                base_namespace="ns",
                cluster="local",
                deployment_name="prod",
                status_data=status_data,
            )
        assert isinstance(errors, list)

    async def test_namespace_events_filtered_to_deployment(self) -> None:
        events = [
            {"object": "prod-frontend-abc", "reason": "BackOff", "message": "container failed", "time": "T1"},
            {"object": "other-deploy-xyz", "reason": "BackOff", "message": "irrelevant", "time": "T2"},
            {"object": "prod", "reason": "FailedScheduling", "message": "no nodes", "time": "T3"},
        ]
        status_data = {"status": {"health": {"status": "Degraded"}}}
        with patch("opi.services.deployment_diagnostics.get_prefixed_namespace", return_value="ns"):
            errors = await gather_deployment_errors(
                argo=_argo_mock(),
                kubectl=_kubectl_mock(events=events),
                app_name="my-app",
                base_namespace="ns",
                cluster="local",
                deployment_name="prod",
                status_data=status_data,
            )
        event_msgs = [e["message"] for e in errors if e["resource"].startswith("Event/")]
        assert "[BackOff] container failed" in event_msgs
        assert "[FailedScheduling] no nodes" in event_msgs
        assert "[BackOff] irrelevant" not in event_msgs

    async def test_namespace_events_skipped_when_progressing_and_no_other_errors(self) -> None:
        events = [{"object": "prod", "reason": "X", "message": "y", "time": "T"}]
        status_data = {"status": {"health": {"status": "Progressing"}}}
        kubectl = _kubectl_mock(events=events)
        with patch("opi.services.deployment_diagnostics.get_prefixed_namespace", return_value="ns"):
            await gather_deployment_errors(
                argo=_argo_mock(),
                kubectl=kubectl,
                app_name="my-app",
                base_namespace="ns",
                cluster="local",
                deployment_name="prod",
                status_data=status_data,
            )
        kubectl.get_namespace_events.assert_not_called()

    async def test_namespace_events_fetched_when_progressing_but_other_errors_exist(self) -> None:
        events = [{"object": "prod", "reason": "BackOff", "message": "y", "time": "T"}]
        status_data = {
            "status": {
                "health": {"status": "Progressing"},
                "resources": [{"kind": "Pod", "name": "p", "health": {"status": "Progressing", "message": "pulling"}}],
            }
        }
        kubectl = _kubectl_mock(events=events)
        with patch("opi.services.deployment_diagnostics.get_prefixed_namespace", return_value="ns"):
            errors = await gather_deployment_errors(
                argo=_argo_mock(),
                kubectl=kubectl,
                app_name="my-app",
                base_namespace="ns",
                cluster="local",
                deployment_name="prod",
                status_data=status_data,
            )
        kubectl.get_namespace_events.assert_called_once()
        assert any(e["resource"].startswith("Event/") for e in errors)

    async def test_pod_event_dropped_when_pod_now_healthy(self) -> None:
        events = [
            {
                "object": "prod-frontend-abc",
                "kind": "Pod",
                "reason": "FailedScheduling",
                "message": "0/12 nodes are available: pod has unbound immediate PersistentVolumeClaims.",
                "time": "T1",
            },
        ]
        tree = [{"kind": "Pod", "name": "prod-frontend-abc", "health": {"status": "Healthy"}}]
        status_data = {"status": {"health": {"status": "Degraded"}}}
        with patch("opi.services.deployment_diagnostics.get_prefixed_namespace", return_value="ns"):
            errors = await gather_deployment_errors(
                argo=_argo_mock(tree_nodes=tree),
                kubectl=_kubectl_mock(events=events),
                app_name="my-app",
                base_namespace="ns",
                cluster="local",
                deployment_name="prod",
                status_data=status_data,
            )
        assert not any(e["resource"].startswith("Event/") for e in errors)

    async def test_pod_event_dropped_when_pod_gone(self) -> None:
        events = [
            {"object": "prod-frontend-old", "kind": "Pod", "reason": "BackOff", "message": "crash", "time": "T1"},
        ]
        tree = [{"kind": "Pod", "name": "prod-frontend-new", "health": {"status": "Degraded", "message": "x"}}]
        status_data = {"status": {"health": {"status": "Degraded"}}}
        with patch("opi.services.deployment_diagnostics.get_prefixed_namespace", return_value="ns"):
            errors = await gather_deployment_errors(
                argo=_argo_mock(tree_nodes=tree),
                kubectl=_kubectl_mock(events=events),
                app_name="my-app",
                base_namespace="ns",
                cluster="local",
                deployment_name="prod",
                status_data=status_data,
            )
        assert not any(e["resource"] == "Event/prod-frontend-old" for e in errors)

    async def test_pod_event_kept_when_pod_still_unhealthy(self) -> None:
        events = [
            {"object": "prod-frontend-abc", "kind": "Pod", "reason": "BackOff", "message": "crash", "time": "T1"},
        ]
        tree = [{"kind": "Pod", "name": "prod-frontend-abc", "health": {"status": "Degraded", "message": "x"}}]
        status_data = {"status": {"health": {"status": "Degraded"}}}
        with patch("opi.services.deployment_diagnostics.get_prefixed_namespace", return_value="ns"):
            errors = await gather_deployment_errors(
                argo=_argo_mock(tree_nodes=tree),
                kubectl=_kubectl_mock(events=events),
                app_name="my-app",
                base_namespace="ns",
                cluster="local",
                deployment_name="prod",
                status_data=status_data,
            )
        assert any(e["resource"] == "Event/prod-frontend-abc" for e in errors)

    async def test_pod_event_kept_when_tree_fetch_failed(self) -> None:
        events = [
            {"object": "prod-frontend-abc", "kind": "Pod", "reason": "BackOff", "message": "crash", "time": "T1"},
        ]
        status_data = {"status": {"health": {"status": "Degraded"}}}
        with patch("opi.services.deployment_diagnostics.get_prefixed_namespace", return_value="ns"):
            errors = await gather_deployment_errors(
                argo=_argo_mock(raises=True),
                kubectl=_kubectl_mock(events=events),
                app_name="my-app",
                base_namespace="ns",
                cluster="local",
                deployment_name="prod",
                status_data=status_data,
            )
        assert any(e["resource"] == "Event/prod-frontend-abc" for e in errors)

    async def test_non_pod_event_not_verified_against_tree(self) -> None:
        events = [
            {
                "object": "prod-frontend",
                "kind": "ReplicaSet",
                "reason": "FailedCreate",
                "message": "quota",
                "time": "T1",
            },
        ]
        tree = [{"kind": "Pod", "name": "prod-frontend-abc", "health": {"status": "Healthy"}}]
        status_data = {"status": {"health": {"status": "Degraded"}}}
        with patch("opi.services.deployment_diagnostics.get_prefixed_namespace", return_value="ns"):
            errors = await gather_deployment_errors(
                argo=_argo_mock(tree_nodes=tree),
                kubectl=_kubectl_mock(events=events),
                app_name="my-app",
                base_namespace="ns",
                cluster="local",
                deployment_name="prod",
                status_data=status_data,
            )
        assert any(e["resource"] == "Event/prod-frontend" for e in errors)

    async def test_pvc_event_dropped_when_pvc_now_bound(self) -> None:
        events = [
            {
                "object": "prod-frontend-data-pvc",
                "kind": "PersistentVolumeClaim",
                "reason": "ProvisioningFailed",
                "message": "failed to provision volume",
                "time": "T1",
            },
        ]
        tree = [{"kind": "PersistentVolumeClaim", "name": "prod-frontend-data-pvc", "health": {"status": "Healthy"}}]
        status_data = {"status": {"health": {"status": "Degraded"}}}
        with patch("opi.services.deployment_diagnostics.get_prefixed_namespace", return_value="ns"):
            errors = await gather_deployment_errors(
                argo=_argo_mock(tree_nodes=tree),
                kubectl=_kubectl_mock(events=events),
                app_name="my-app",
                base_namespace="ns",
                cluster="local",
                deployment_name="prod",
                status_data=status_data,
            )
        assert not any(e["resource"].startswith("Event/") for e in errors)

    async def test_pvc_event_kept_when_pvc_still_pending(self) -> None:
        events = [
            {
                "object": "prod-frontend-data-pvc",
                "kind": "PersistentVolumeClaim",
                "reason": "ProvisioningFailed",
                "message": "failed to provision volume",
                "time": "T1",
            },
        ]
        tree = [
            {"kind": "PersistentVolumeClaim", "name": "prod-frontend-data-pvc", "health": {"status": "Progressing"}}
        ]
        status_data = {"status": {"health": {"status": "Degraded"}}}
        with patch("opi.services.deployment_diagnostics.get_prefixed_namespace", return_value="ns"):
            errors = await gather_deployment_errors(
                argo=_argo_mock(tree_nodes=tree),
                kubectl=_kubectl_mock(events=events),
                app_name="my-app",
                base_namespace="ns",
                cluster="local",
                deployment_name="prod",
                status_data=status_data,
            )
        assert any(e["resource"] == "Event/prod-frontend-data-pvc" for e in errors)

    async def test_namespace_events_fetch_failure_is_swallowed(self) -> None:
        status_data = {"status": {"health": {"status": "Degraded"}}}
        with patch("opi.services.deployment_diagnostics.get_prefixed_namespace", return_value="ns"):
            errors = await gather_deployment_errors(
                argo=_argo_mock(),
                kubectl=_kubectl_mock(events_raises=True),
                app_name="my-app",
                base_namespace="ns",
                cluster="local",
                deployment_name="prod",
                status_data=status_data,
            )
        assert isinstance(errors, list)

    async def test_kubectl_none_skips_events(self) -> None:
        status_data = {"status": {"health": {"status": "Degraded"}}}
        errors = await gather_deployment_errors(
            argo=_argo_mock(),
            kubectl=None,
            app_name="my-app",
            base_namespace="ns",
            cluster="local",
            deployment_name="prod",
            status_data=status_data,
        )
        assert not any(e["resource"].startswith("Event/") for e in errors)

    async def test_sync_result_failed_resources_become_errors(self) -> None:
        status_data = {
            "status": {
                "health": {"status": "Degraded"},
                "operationState": {
                    "syncResult": {
                        "resources": [
                            {"kind": "Ingress", "name": "frontend", "status": "SyncFailed", "message": "TLS missing"},
                            {"kind": "Service", "name": "frontend", "status": "Synced"},
                        ]
                    }
                },
            }
        }
        with patch("opi.services.deployment_diagnostics.get_prefixed_namespace", return_value="ns"):
            errors = await gather_deployment_errors(
                argo=_argo_mock(),
                kubectl=_kubectl_mock(),
                app_name="my-app",
                base_namespace="ns",
                cluster="local",
                deployment_name="prod",
                status_data=status_data,
            )
        assert {"resource": "Ingress/frontend", "message": "TLS missing"} in errors

    async def test_conditions_become_errors(self) -> None:
        status_data = {
            "status": {
                "health": {"status": "Degraded"},
                "conditions": [
                    {"type": "ComparisonError", "message": "manifest invalid"},
                    {"type": "OK", "message": ""},
                ],
            }
        }
        with patch("opi.services.deployment_diagnostics.get_prefixed_namespace", return_value="ns"):
            errors = await gather_deployment_errors(
                argo=_argo_mock(),
                kubectl=_kubectl_mock(),
                app_name="my-app",
                base_namespace="ns",
                cluster="local",
                deployment_name="prod",
                status_data=status_data,
            )
        assert {"resource": "ComparisonError", "message": "manifest invalid"} in errors
        assert not any(e["resource"] == "OK" for e in errors)

    async def test_failed_operation_phase_becomes_error(self) -> None:
        status_data = {
            "status": {
                "health": {"status": "Degraded"},
                "operationState": {
                    "phase": "Failed",
                    "message": "could not apply",
                    "finishedAt": "2026-04-22T11:00:00Z",
                },
            }
        }
        with patch("opi.services.deployment_diagnostics.get_prefixed_namespace", return_value="ns"):
            errors = await gather_deployment_errors(
                argo=_argo_mock(),
                kubectl=_kubectl_mock(),
                app_name="my-app",
                base_namespace="ns",
                cluster="local",
                deployment_name="prod",
                status_data=status_data,
            )
        op_err = next(e for e in errors if e["resource"] == "SyncOperation")
        assert op_err["message"] == "could not apply"
        assert op_err["timestamp"] == "2026-04-22T11:00:00Z"


# ---------------------------------------------------------------------------
# categorize_error
# ---------------------------------------------------------------------------


class TestCategorizeError:
    """Mapping (resource, message) -> (ErrorCategory, explanation)."""

    @pytest.mark.parametrize(
        ("resource", "message", "expected"),
        [
            ("Pod/x", "Back-off pulling image foo: manifest unknown", ErrorCategory.ImagePull),
            ("Pod/x", "ImagePullBackOff", ErrorCategory.ImagePull),
            ("Pod/x", "ErrImagePull: not found", ErrorCategory.ImagePull),
            ("Pod/x", "Back-off restarting failed container", ErrorCategory.CrashLoop),
            ("Pod/x", "CrashLoopBackOff", ErrorCategory.CrashLoop),
            ("Pod/x", "OOMKilled", ErrorCategory.OutOfMemory),
            ("Pod/x", "container killed due to out of memory", ErrorCategory.OutOfMemory),
            ("Pod/x", "Liveness probe failed: HTTP 500", ErrorCategory.HealthCheck),
            ("Pod/x", "Readiness probe failed", ErrorCategory.HealthCheck),
            ("Pod/x", "Startup probe failed", ErrorCategory.HealthCheck),
            ("SyncOperation", "Sync operation failed", ErrorCategory.SyncFailed),
            ("Ingress/x", "SyncFailed: not allowed", ErrorCategory.SyncFailed),
            ("ComparisonError", "manifest invalid", ErrorCategory.ComparisonError),
            ("Pod/x", "some unrelated message", ErrorCategory.Unknown),
        ],
    )
    def test_pattern_matching(self, resource: str, message: str, expected: ErrorCategory) -> None:
        category, _ = categorize_error(resource, message)
        assert category is expected

    def test_known_categories_have_explanation(self) -> None:
        representative: dict[ErrorCategory, tuple[str, str]] = {
            ErrorCategory.ImagePull: ("Pod/x", "ImagePullBackOff"),
            ErrorCategory.CrashLoop: ("Pod/x", "CrashLoopBackOff"),
            ErrorCategory.OutOfMemory: ("Pod/x", "OOMKilled"),
            ErrorCategory.HealthCheck: ("Pod/x", "Liveness probe failed"),
            ErrorCategory.SyncFailed: ("SyncOperation", "Sync operation failed"),
            ErrorCategory.ComparisonError: ("ComparisonError", "manifest invalid"),
            ErrorCategory.Unknown: ("Pod/x", "totally unrelated"),
        }
        for category, (resource, message) in representative.items():
            actual_category, explanation = categorize_error(resource, message)
            assert actual_category is category, f"{category}: got {actual_category}"
            if category is ErrorCategory.Unknown:
                assert explanation is None
            else:
                assert explanation is not None
                assert len(explanation) > 0


# ---------------------------------------------------------------------------
# gather_sync_deviations
# ---------------------------------------------------------------------------


def _mb_docs_status() -> dict[str, Any]:
    """Het mb-docs-helmfile-geval van 2026-08-20: alles draait, twee Jobs hangen in delete.

    Sync OutOfSync + health Progressing, laatste operatie Succeeded en die heeft de twee
    Jobs al "Pruned" gemeld - maar ze bestaan nog (finalizer-bug). De kaart toonde twee
    gele badges zonder verklaring; deze fixture pint dat de afwijkingen dat nu uitleggen.
    """
    return {
        "spec": {"syncPolicy": {"automated": {"prune": True}}},
        "status": {
            "sync": {"status": "OutOfSync"},
            "health": {"status": "Progressing"},
            "operationState": {
                "phase": "Succeeded",
                "syncResult": {
                    "resources": [
                        {"kind": "Job", "name": "docs-backend-createsuperuser-1786315497", "status": "Pruned"},
                        {"kind": "Job", "name": "docs-backend-migrate-1786315497", "status": "Pruned"},
                    ]
                },
            },
            "resources": [
                {"kind": "Deployment", "name": "docs-backend", "status": "Synced", "health": {"status": "Healthy"}},
                {
                    "kind": "Job",
                    "name": "docs-backend-createsuperuser-1786315497",
                    "status": "OutOfSync",
                    "requiresPruning": True,
                    "health": {"status": "Progressing"},
                },
                {
                    "kind": "Job",
                    "name": "docs-backend-migrate-1786315497",
                    "status": "OutOfSync",
                    "requiresPruning": True,
                    "health": {"status": "Progressing"},
                },
            ],
        },
    }


class TestGatherSyncDeviations:
    """Afwijkingen verklaren de gele badges zonder dat het fouten zijn."""

    def test_stuck_deletion_gets_its_own_reason(self) -> None:
        deviations = gather_sync_deviations(_mb_docs_status())
        assert [d["resource"] for d in deviations] == [
            "Job/docs-backend-createsuperuser-1786315497",
            "Job/docs-backend-migrate-1786315497",
        ]
        assert all(d["reason"] == "is verwijderd, maar het cluster maakt de verwijdering niet af" for d in deviations)
        assert all(d["kind"] == "Job" for d in deviations)

    @pytest.mark.asyncio
    async def test_mb_docs_case_has_deviations_but_no_errors(self) -> None:
        """Het oorspronkelijke gat: errors bleef leeg, dus de kaart zweeg."""
        errors = await gather_deployment_errors(
            argo=_argo_mock(),
            kubectl=None,
            app_name="app",
            base_namespace="ns",
            cluster="c",
            deployment_name="docs",
            status_data=_mb_docs_status(),
        )
        assert errors == []
        assert len(gather_sync_deviations(_mb_docs_status())) == 2

    def test_green_status_has_no_deviations(self) -> None:
        status_data = {
            "spec": {"syncPolicy": {"automated": {}}},
            "status": {
                "sync": {"status": "Synced"},
                "health": {"status": "Healthy"},
                "resources": [
                    {"kind": "Deployment", "name": "web", "status": "Synced", "health": {"status": "Healthy"}}
                ],
            },
        }
        assert gather_sync_deviations(status_data) == []

    def test_prune_not_yet_attempted_says_next_sync(self) -> None:
        status_data = _mb_docs_status()
        status_data["status"]["operationState"]["syncResult"]["resources"] = []
        deviations = gather_sync_deviations(status_data)
        assert all(d["reason"] == "staat niet meer in git en wordt bij de volgende sync opgeruimd" for d in deviations)

    def test_plain_diff_mentions_auto_sync(self) -> None:
        status_data = {
            "spec": {"syncPolicy": {"automated": {}}},
            "status": {
                "sync": {"status": "OutOfSync"},
                "health": {"status": "Healthy"},
                "resources": [{"kind": "Deployment", "name": "web", "status": "OutOfSync"}],
            },
        }
        assert gather_sync_deviations(status_data) == [
            {
                "resource": "Deployment/web",
                "kind": "Deployment",
                "reason": "wijkt af van git en wordt bij de volgende sync bijgewerkt",
            }
        ]

    def test_plain_diff_without_auto_sync(self) -> None:
        status_data = {
            "spec": {"syncPolicy": {}},
            "status": {
                "sync": {"status": "OutOfSync"},
                "health": {"status": "Healthy"},
                "resources": [{"kind": "Deployment", "name": "web", "status": "OutOfSync"}],
            },
        }
        assert gather_sync_deviations(status_data)[0]["reason"] == "wijkt af van git; auto-sync staat uit"

    def test_progressing_without_message_becomes_nog_bezig(self) -> None:
        status_data = {
            "status": {
                "sync": {"status": "Synced"},
                "health": {"status": "Progressing"},
                "resources": [
                    {"kind": "Deployment", "name": "web", "status": "Synced", "health": {"status": "Progressing"}},
                    {
                        "kind": "Deployment",
                        "name": "api",
                        "status": "Synced",
                        # Met message: die verschijnt al via gather_deployment_errors.
                        "health": {"status": "Progressing", "message": "waiting for rollout"},
                    },
                ],
            },
        }
        assert gather_sync_deviations(status_data) == [
            {"resource": "Deployment/web", "kind": "Deployment", "reason": "nog bezig"}
        ]

    def test_disabled_component_resources_are_dropped(self) -> None:
        status_data = {
            "status": {
                "sync": {"status": "OutOfSync"},
                "health": {"status": "Healthy"},
                "resources": [
                    {"kind": "Deployment", "name": "productie-typesense", "status": "OutOfSync"},
                    {"kind": "Deployment", "name": "productie-web", "status": "OutOfSync"},
                ],
            },
        }
        deviations = gather_sync_deviations(
            status_data, deployment_name="productie", disabled_components=frozenset({"typesense"})
        )
        assert [d["resource"] for d in deviations] == ["Deployment/productie-web"]


# ---------------------------------------------------------------------------
# summarize_component_pods (RC-162)
# ---------------------------------------------------------------------------
#
# De vraag die de kaart niet kon beantwoorden: WELKE pod handelt er verkeer af. Bij
# psd-law/pr-114 waren dat er twee voor hetzelfde component -- een die sinds 18 augustus
# bediende en een die al negentien uur niet opkwam -- en ArgoCD noemde het geheel Degraded.


def _pod(
    name: str,
    *,
    app: str,
    ready: bool,
    image: str = "",
    started_at: str | None = None,
    restart_count: int = 0,
    deleting: bool = False,
) -> dict[str, Any]:
    return {
        "name": name,
        "app": app,
        "pod_template_hash": name.split("-")[-2] if "-" in name else "",
        "deleting": deleting,
        "ready": ready,
        "image": image,
        "restart_count": restart_count,
        "started_at": started_at,
        "has_previous_attempt": restart_count > 0,
    }


def _deployment(image: str) -> dict[str, Any]:
    return {"name": "pr-114", "components": [{"reference": "profielservice", "image": image}]}


def test_summarize_reports_the_serving_pod_with_its_image_and_start():
    """Draait de ingestelde image: een gewone regel, geen verdict dat iets afwijkt."""
    deployment = _deployment("ghcr.io/minbzk/moza-profiel-service@sha256:25ab6344")
    pods = [
        _pod(
            "pr-114-profielservice-849d475c4-4qp6p",
            app="pr-114-profielservice",
            ready=True,
            image="ghcr.io/minbzk/moza-profiel-service@sha256:25ab6344",
            started_at="2026-08-18T11:59:12Z",
        ),
        _pod(
            "pr-114-profielservice-58cb9567c5-9t87d",
            app="pr-114-profielservice",
            ready=False,
            image="ghcr.io/minbzk/moza-profiel-service@sha256:2c0728ed",
            restart_count=5,
        ),
    ]

    (summary,) = summarize_component_pods(pods, deployment=deployment)

    assert summary.reference == "profielservice"
    assert summary.is_serving is True
    assert summary.pod_name == "pr-114-profielservice-849d475c4-4qp6p"
    assert summary.image == "ghcr.io/minbzk/moza-profiel-service@sha256:25ab6344"
    assert summary.running_since == "2026-08-18T11:59:12Z"
    assert summary.runs_configured_image is True


def test_summarize_reports_a_serving_pod_on_a_different_image():
    """De uitrol is niet doorgekomen: er draait iets, maar niet wat er is ingesteld."""
    deployment = _deployment("ghcr.io/minbzk/moza-profiel-service@sha256:2c0728ed")
    pods = [
        _pod(
            "pr-114-profielservice-849d475c4-4qp6p",
            app="pr-114-profielservice",
            ready=True,
            image="ghcr.io/minbzk/moza-profiel-service@sha256:25ab6344",
            started_at="2026-08-18T11:59:12Z",
        )
    ]

    (summary,) = summarize_component_pods(pods, deployment=deployment)

    assert summary.is_serving is True
    assert summary.runs_configured_image is False
    assert summary.configured_image == "ghcr.io/minbzk/moza-profiel-service@sha256:2c0728ed"


def test_summarize_reports_that_nothing_is_serving():
    """Geen enkele pod is ready: dan LIGT de applicatie eruit, en dat is een eigen uitkomst."""
    deployment = _deployment("ghcr.io/minbzk/moza-profiel-service:2.1")
    pods = [
        _pod(
            "pr-114-profielservice-58cb9567c5-9t87d",
            app="pr-114-profielservice",
            ready=False,
            image="ghcr.io/minbzk/moza-profiel-service:2.2",
            restart_count=5,
        )
    ]

    (summary,) = summarize_component_pods(pods, deployment=deployment)

    assert summary.is_serving is False
    assert summary.pod_name is None
    assert summary.image is None
    assert summary.running_since is None


def test_summarize_reports_nothing_serving_when_there_are_no_pods_at_all():
    (summary,) = summarize_component_pods([], deployment=_deployment("ghcr.io/x/y:1"))
    assert summary.is_serving is False


def test_summarize_ignores_a_terminating_pod():
    """Een pod met een deletionTimestamp draagt het label nog en is toch geen antwoord."""
    deployment = _deployment("ghcr.io/x/y:1")
    pods = [
        _pod(
            "pr-114-profielservice-849d475c4-4qp6p",
            app="pr-114-profielservice",
            ready=True,
            image="ghcr.io/x/y:1",
            started_at="2026-08-18T11:59:12Z",
            deleting=True,
        )
    ]
    (summary,) = summarize_component_pods(pods, deployment=deployment)
    assert summary.is_serving is False


def test_summarize_gives_no_verdict_when_a_digest_faces_a_tag():
    """Een digest en een tag zeggen niets over elkaar, dus er komt geen uitspraak."""
    deployment = _deployment("ghcr.io/minbzk/moza-profiel-service:2.1")
    pods = [
        _pod(
            "pr-114-profielservice-849d475c4-4qp6p",
            app="pr-114-profielservice",
            ready=True,
            image="ghcr.io/minbzk/moza-profiel-service@sha256:25ab6344",
            started_at="2026-08-18T11:59:12Z",
        )
    ]

    (summary,) = summarize_component_pods(pods, deployment=deployment)

    assert summary.is_serving is True
    assert summary.runs_configured_image is None
    assert summary.image == "ghcr.io/minbzk/moza-profiel-service@sha256:25ab6344"


def test_summarize_shows_the_source_registry_not_the_proxy():
    """De gebruiker kent zijn eigen registry; de rcr-proxyvorm is een platformdetail."""
    mappings = [{"from": "ghcr.io", "to": "rcr.rijksapps.nl/ghcr-rig"}]
    deployment = _deployment("ghcr.io/minbzk/moza-profiel-service@sha256:25ab6344")
    pods = [
        _pod(
            "pr-114-profielservice-849d475c4-4qp6p",
            app="pr-114-profielservice",
            ready=True,
            image="rcr.rijksapps.nl/ghcr-rig/minbzk/moza-profiel-service@sha256:25ab6344",
            started_at="2026-08-18T11:59:12Z",
        )
    ]

    with patch("opi.services.deployment_diagnostics.get_registry_rewrite_mappings", return_value=mappings):
        (summary,) = summarize_component_pods(pods, deployment=deployment)

    assert summary.image == "ghcr.io/minbzk/moza-profiel-service@sha256:25ab6344"
    assert "rcr.rijksapps.nl" not in (summary.image or "")
    assert summary.runs_configured_image is True


def test_summarize_matches_pods_through_the_unique_name_map():
    """Een pod van een ANDER component belandt niet bij dit component.

    De koppeling loopt via ``{generate_unique_name(...): reference}`` en niet via het
    afknippen van een prefix; ``pr-114-profielservice-extra`` begint met dezelfde letters
    als ``pr-114-profielservice`` en is toch iets anders.
    """
    deployment = {
        "name": "pr-114",
        "components": [
            {"reference": "profielservice", "image": "ghcr.io/x/y:1"},
            {"reference": "profielservice-extra", "image": "ghcr.io/x/z:1"},
        ],
    }
    pods = [
        _pod(
            "pr-114-profielservice-extra-abc123456-aaaaa",
            app="pr-114-profielservice-extra",
            ready=True,
            image="ghcr.io/x/z:1",
            started_at="2026-08-18T11:59:12Z",
        )
    ]

    eerste, tweede = summarize_component_pods(pods, deployment=deployment)

    assert eerste.reference == "profielservice"
    assert eerste.is_serving is False
    assert tweede.reference == "profielservice-extra"
    assert tweede.is_serving is True


def test_summarize_leaves_out_a_disabled_component():
    """Nul replicas is daar de bedoeling; de kaart noemt die componenten al met hun reden."""
    deployment = {
        "name": "pr-114",
        "components": [{"reference": "profielservice", "image": "ghcr.io/x/y:1", "disabled": True}],
    }
    assert summarize_component_pods([], deployment=deployment) == []
