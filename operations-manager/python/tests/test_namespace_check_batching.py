"""The namespace check must not ask the cluster once per deployment.

Measured on production: 45 projects across 44 distinct namespaces produced 127
``kubectl get namespace`` calls plus 127 ``kubectl label namespace`` calls, each a
separate subprocess. That loop was 70 of the 83 seconds the pod took to boot, and
every one of those namespaces already existed with the correct label.

Two things were wrong. Namespaces are per project, but the loop ran per deployment,
so a project with three deployments checked the same namespace three times. And the
label was applied unconditionally, "because it is idempotent" -- which is true, and
still costs a process and an API write each time.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from opi.manager.project_manager import ProjectManager

MANAGED_BY = "argocd.argoproj.io/managed-by"


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch) -> ProjectManager:
    pm = ProjectManager(project_file_relative_path="projects/demo.yaml")

    project_data: dict[str, Any] = {
        "name": "demo",
        # Three deployments, one namespace: the shape that caused the duplication.
        "deployments": [
            {"name": "main", "namespace": "demo", "cluster": "test-cluster"},
            {"name": "acc", "namespace": "demo", "cluster": "test-cluster"},
            {"name": "pr-1", "namespace": "demo", "cluster": "test-cluster"},
        ],
    }
    monkeypatch.setattr(pm, "get_contents", AsyncMock(return_value=project_data))
    monkeypatch.setattr(pm, "get_deployments", AsyncMock(return_value=project_data["deployments"]))
    monkeypatch.setattr(pm, "get_progress_manager", lambda: None)
    return pm


def _kubectl(monkeypatch: pytest.MonkeyPatch, manager: ProjectManager) -> AsyncMock:
    kubectl = AsyncMock()
    kubectl.namespace_exists = AsyncMock(return_value=True)
    kubectl.apply_label_to_resource = AsyncMock(return_value=True)
    monkeypatch.setattr(manager, "_kubectl_connector", kubectl)
    return kubectl


async def test_one_namespace_is_checked_once_not_once_per_deployment(
    manager: ProjectManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    kubectl = _kubectl(monkeypatch, manager)

    assert await manager.check_and_create_namespaces() is True

    assert kubectl.namespace_exists.await_count == 1, "three deployments share one namespace"
    assert kubectl.apply_label_to_resource.await_count == 1


async def test_a_pre_read_label_map_removes_the_cluster_calls_entirely(
    manager: ProjectManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What startup does: it already knows the namespace exists and is labelled."""
    kubectl = _kubectl(monkeypatch, manager)
    from opi.core.cluster_config import get_argo_namespace
    from opi.core.config import settings

    known = {f"{_prefix()}demo": get_argo_namespace(settings.CLUSTER_MANAGER)}

    assert await manager.check_and_create_namespaces(known_namespace_labels=known) is True

    assert kubectl.namespace_exists.await_count == 0
    assert kubectl.apply_label_to_resource.await_count == 0, "the label was already correct"


async def test_a_missing_label_is_still_applied(manager: ProjectManager, monkeypatch: pytest.MonkeyPatch) -> None:
    """The check may be cheap, but it may not become a no-op."""
    kubectl = _kubectl(monkeypatch, manager)

    known = {f"{_prefix()}demo": ""}  # namespace exists, label missing

    assert await manager.check_and_create_namespaces(known_namespace_labels=known) is True

    assert kubectl.namespace_exists.await_count == 0
    assert kubectl.apply_label_to_resource.await_count == 1


async def test_a_namespace_missing_from_the_map_is_created(
    manager: ProjectManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent from the map means absent from the cluster, so it must be created.

    This is the failure that would matter: treating "not in the map" as "fine" would
    silently skip namespace creation for a new project.
    """
    kubectl = _kubectl(monkeypatch, manager)
    created = AsyncMock()
    monkeypatch.setattr(manager, "_create_namespace_with_argocd_label", created)

    assert await manager.check_and_create_namespaces(known_namespace_labels={}) is True

    created.assert_awaited_once()
    assert kubectl.apply_label_to_resource.await_count == 0, "creation applies the label itself"


def _prefix() -> str:
    from opi.core.cluster_config import get_prefixed_namespace
    from opi.core.config import settings

    return get_prefixed_namespace(settings.CLUSTER_MANAGER, "")
