"""RC-513: the routing stand of the sandbox is one line in git, and nothing else names it.

The sandbox serves its hostnames either through ingress-nginx or through the shared Gateway
of Envoy Gateway. One ``components`` line in
``infrastructure/bootstrap/routing/sandboxed-local`` picks the stand, the ArgoCD Application
``routing-infrastructure`` brings it to the cluster, and OPI reads it from the ConfigMap
``zad-routing`` that same stand brings along. This test keeps the second line from coming
back: a stand named in two places goes out of step, and nothing else catches that.

The build of both stands and how they fit OPI is in test_sandbox_routing_contract.py.
See docs/sandbox/routing-wisselen.md.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]

SWITCH = REPO_ROOT / "infrastructure/bootstrap/routing/sandboxed-local/kustomization.yaml"
APPLICATION = (
    REPO_ROOT / "bootstrap/rig-system/kustomize/overlays/sandboxed-local/argocd-application-routing-infrastructure.yaml"
)
APPPROJECT = REPO_ROOT / "bootstrap/rig-system/kustomize/overlays/sandboxed-local/argocd-appproject-zad-routing.yaml"
TASKFILE = REPO_ROOT / "Taskfile.yaml"

#: Where a routing resource used to live. A stand in two places is a stand that drifts.
FORMER_LOCATIONS = [
    "bootstrap/rig-system/kustomize/components/sandbox-routing",
    "infrastructure/bootstrap/infrastructure/routing",
    "bootstrap/rig-system/kustomize/envoy-gateway",
    "bootstrap/rig-system/kustomize/ingress-nginx",
    "infrastructure/bootstrap/routing/gateway",
    "infrastructure/bootstrap/routing/ingress",
]


def stand_in_git() -> str:
    (component,) = yaml.safe_load(SWITCH.read_text())["components"]
    return component.rsplit("/", 1)[-1]


def test_the_stand_is_one_of_the_two_known_values() -> None:
    assert stand_in_git() in ("ingress", "gateway")


def test_the_sandbox_is_on_ingress_in_git() -> None:
    """RC-513 leaves the sandbox on ingress; switching it over is its own step."""
    assert stand_in_git() == "ingress"


def test_no_former_location_names_the_stand_any_more() -> None:
    present = [location for location in FORMER_LOCATIONS if (REPO_ROOT / location).exists()]

    assert present == []


def test_no_taskfile_task_changes_the_stand_of_the_cluster() -> None:
    """``sandbox:routing-bootstrap`` installs the platform layer (both controllers, whatever
    the stand) and puts the stand of git on a FRESH cluster, before ArgoCD exists; everything
    else that used to install or swap a controller is gone."""
    taskfile = TASKFILE.read_text()

    for name in ("routing-apply", "install-routing-controller", "install-ingress-nginx", "install-envoy-gateway"):
        assert name not in taskfile, f"Taskfile.yaml still mentions {name}"
    assert re.search(r"^  sandbox:routing-bootstrap:$", taskfile, re.MULTILINE)


def test_the_bootstrap_installs_both_controllers() -> None:
    """The platform layer knows no stand: both controllers go on the cluster, so a switch
    never asks for a bootstrap step. The stand only decides which proxy ArgoCD runs."""
    task = TASKFILE.read_text().split("  sandbox:routing-bootstrap:", 1)[1].split("\n  generate-env-secrets", 1)[0]

    assert "platform/sandboxed-local/envoy-gateway" in task
    assert "platform/sandboxed-local/ingress-nginx" in task


def test_the_application_renders_without_the_cmp_plugin_and_prunes() -> None:
    """Without the plugin, because it would force one namespace on a build that spans four.
    With prune, because that is what cleans up the stand being left behind."""
    application = yaml.safe_load(APPLICATION.read_text())

    source = application["spec"]["source"]
    assert "plugin" not in source
    assert source["path"] == "bootstrap/routing/sandboxed-local"
    assert application["spec"]["project"] == "zad-routing"
    sync_policy = application["spec"]["syncPolicy"]
    assert sync_policy["automated"]["prune"] is True
    assert "ServerSideApply=true" in sync_policy["syncOptions"]
    assert "PruneLast=true" not in sync_policy["syncOptions"], (
        "PruneLast would make the prune wait on a new stand that cannot become healthy while "
        "the old one still holds hostPort 80 and 443"
    )


def test_the_appproject_of_the_routing_stays_namespaced() -> None:
    """Own project because the routing lands in four namespaces, and cluster scoped nothing:
    the CRDs, the cluster RBAC and the webhooks of the controllers are platform."""
    appproject = yaml.safe_load(APPPROJECT.read_text())

    assert appproject["metadata"]["name"] == "zad-routing"
    assert appproject["spec"]["clusterResourceWhitelist"] == []
