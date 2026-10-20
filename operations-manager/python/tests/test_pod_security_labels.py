"""Pod Security-labels op een projectnamespace (RC-316).

Een tweede verdedigingslaag naast de hardening in de sjablonen: wat via helm, helmfile
of een los manifest in een deploymentmap binnenkomt, komt er niet langs de API-server
mee weg. Op de sandbox wordt ``baseline`` afgedwongen en waarschuwt ``restricted``; op
``local`` waarschuwt baseline alleen, want de CA-mount daar is een hostPath.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from opi.core.cluster_config import get_argo_namespace, get_pod_security_labels, namespace_labels
from opi.core.config import settings
from opi.generation.manifests import render_template
from opi.manager.project_manager import ProjectManager
from ruamel.yaml import YAML

ENFORCE = "pod-security.kubernetes.io/enforce"
WARN = "pod-security.kubernetes.io/warn"
AUDIT = "pod-security.kubernetes.io/audit"


def test_the_sandbox_enforces_baseline_and_warns_on_restricted() -> None:
    assert get_pod_security_labels("sandboxed-local") == {
        ENFORCE: "baseline",
        WARN: "restricted",
        AUDIT: "restricted",
    }


def test_local_only_warns_because_its_ca_mount_is_a_hostpath() -> None:
    """ca_certificate.enabled op local rendert een hostPath, en dat verbiedt baseline."""
    assert get_pod_security_labels("local") == {
        ENFORCE: "privileged",
        WARN: "baseline",
        AUDIT: "baseline",
    }


def test_production_has_no_labels_until_capsule_and_the_syncer_are_checked() -> None:
    """Zie features/pod-security-labels.md: ontbreekt de sleutel, dan zet OPI geen label."""
    assert get_pod_security_labels("odcn-production") == {}


def _render(cluster: str) -> dict[str, Any]:
    result = render_template(
        "namespace.yaml.jinja",
        {"namespace": "rig-myproject", "pod_security_labels": get_pod_security_labels(cluster)},
    )
    return YAML().load(result)


def test_the_rendered_namespace_carries_the_labels_of_its_cluster() -> None:
    for cluster in ("local", "sandboxed-local"):
        labels = _render(cluster)["metadata"]["labels"]
        for key, value in get_pod_security_labels(cluster).items():
            assert labels[key] == value
        assert labels["created-by"] == "operations-manager", "het bestaande label blijft"


def test_the_rendered_namespace_has_no_pod_security_label_on_production() -> None:
    labels = _render("odcn-production")["metadata"]["labels"]
    assert not [key for key in labels if key.startswith("pod-security.kubernetes.io/")]


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch) -> ProjectManager:
    """Een project met een bestaande namespace, zoals elke verwerking die aantreft."""
    pm = ProjectManager(project_file_relative_path="projects/demo.yaml")
    project_data: dict[str, Any] = {
        "name": "demo",
        "deployments": [{"name": "main", "namespace": "demo", "cluster": settings.CLUSTER_MANAGER}],
    }
    monkeypatch.setattr(pm, "get_contents", AsyncMock(return_value=project_data))
    monkeypatch.setattr(pm, "get_deployments", AsyncMock(return_value=project_data["deployments"]))
    monkeypatch.setattr(pm, "get_progress_manager", lambda: None)
    return pm


async def test_processing_an_existing_project_sets_the_three_labels(
    manager: ProjectManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """De namespace-apply draait alleen bij aanmaak, dus een bestaande namespace krijgt
    de labels van de label-apply die elke verwerking doet."""
    kubectl = AsyncMock()
    kubectl.namespace_exists = AsyncMock(return_value=True)
    kubectl.apply_labels_to_resource = AsyncMock(return_value=True)
    monkeypatch.setattr(manager, "_kubectl_connector", kubectl)

    assert await manager.check_and_create_namespaces() is True

    applied = kubectl.apply_labels_to_resource.await_args.kwargs["labels"]
    expected = get_pod_security_labels(settings.CLUSTER_MANAGER)
    assert expected, "dit cluster hoort de labels te zetten"
    for key, value in expected.items():
        assert applied[key] == value
    assert "argocd.argoproj.io/managed-by" in applied, "de bestaande label gaat mee in dezelfde call"


def test_the_namespace_labels_are_one_list_for_both_creation_paths() -> None:
    """project_manager en git_monitor labelen namespaces, en delen deze lijst."""
    labels = namespace_labels("sandboxed-local")
    assert labels["argocd.argoproj.io/managed-by"] == get_argo_namespace("sandboxed-local")
    assert labels[ENFORCE] == "baseline"
