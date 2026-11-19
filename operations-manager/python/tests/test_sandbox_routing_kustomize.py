"""RC-510: the gateway-stand components must actually kustomize-build, and the ingress-stand
components must stay empty so attaching them changes nothing (the "ingress-stand builds are
byte-identical" promise from the task).

A kustomize Component cannot be built standalone when it carries a ``$patch: delete`` against
a resource defined elsewhere (kustomize Components are always merged into a parent
Kustomization), so a bare ``kustomize build`` on the component directory always fails - that
failure is not a bug. The real question, and the one the "Klaar als" criterion asks, is
whether the component builds when composed with the Ingress it is supposed to replace. The
full overlays need a SOPS key this session does not have, so each test below builds a
throwaway kustomization with only the relevant base resources plus the component,
``--load-restrictor LoadRestrictionsNone``.

See docs/sandbox/routing-wisselen.md.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]

ENVOY_GATEWAY_BASE = REPO_ROOT / "bootstrap/rig-system/kustomize/envoy-gateway/base"
ENVOY_GATEWAY_GATEWAY = REPO_ROOT / "bootstrap/rig-system/kustomize/envoy-gateway/gateway"

BOOTSTRAP_GATEWAY_COMPONENT = REPO_ROOT / "bootstrap/rig-system/kustomize/components/sandbox-routing/gateway"
BOOTSTRAP_INGRESS_COMPONENT = REPO_ROOT / "bootstrap/rig-system/kustomize/components/sandbox-routing/ingress"
INFRA_GATEWAY_COMPONENT = REPO_ROOT / "infrastructure/bootstrap/infrastructure/routing/gateway"
INFRA_INGRESS_COMPONENT = REPO_ROOT / "infrastructure/bootstrap/infrastructure/routing/ingress"
MAILSINK_GATEWAY_COMPONENT = REPO_ROOT / "infrastructure/bootstrap/infrastructure/routing/mailsink-gateway"
MAILSINK_INGRESS_COMPONENT = REPO_ROOT / "infrastructure/bootstrap/infrastructure/routing/mailsink-ingress"

OPERATIONS_MANAGER_INGRESS = (
    REPO_ROOT / "bootstrap/rig-system/kustomize/operations-manager/overlays/sandboxed-local/ingress.yaml"
)
OPERATIONS_MANAGER_DEPLOYMENT = REPO_ROOT / "bootstrap/rig-system/kustomize/operations-manager/base/deployment.yaml"
ARGOCD_CR = REPO_ROOT / "bootstrap/rig-system/kustomize/overlays/sandboxed-local/argocd-deployment.yaml"
KEYCLOAK_INGRESS = REPO_ROOT / "infrastructure/bootstrap/infrastructure/keycloak/controller/base/ingress.yaml"
FORGEJO_INGRESS = REPO_ROOT / "infrastructure/bootstrap/infrastructure/forgejo/controller/base/ingress.yaml"
MINIO_INGRESS = (
    REPO_ROOT / "infrastructure/bootstrap/infrastructure/minio/controller/overlays/sandboxed-local/ingress.yaml"
)
PROMETHEUS_INGRESS = (
    REPO_ROOT / "infrastructure/bootstrap/infrastructure/prometheus/controller/overlays/sandboxed-local/ingress.yaml"
)
REGISTRY_INGRESS = (
    REPO_ROOT / "infrastructure/bootstrap/infrastructure/registry/controller/overlays/sandboxed-local/ingress.yaml"
)
MAILSINK_INGRESS = (
    REPO_ROOT / "infrastructure/bootstrap/infrastructure/mail/controller/overlays/sandboxed-local/ingress-mailsink.yaml"
)


def _kustomize() -> str:
    found = shutil.which("kustomize")
    if found is None:
        pytest.skip("kustomize is missing")
    return found


def _kustomize_build(
    tmp_path: Path, *, resources: list[Path], components: list[Path]
) -> subprocess.CompletedProcess[str]:
    kustomize = _kustomize()
    lines = ["apiVersion: kustomize.config.k8s.io/v1beta1", "kind: Kustomization"]
    if resources:
        lines.append("resources:")
        lines += [f"- {os.path.relpath(path, tmp_path)}" for path in resources]
    if components:
        lines.append("components:")
        lines += [f"- {os.path.relpath(path, tmp_path)}" for path in components]
    (tmp_path / "kustomization.yaml").write_text("\n".join(lines) + "\n")
    return subprocess.run(
        [kustomize, "build", "--load-restrictor", "LoadRestrictionsNone", str(tmp_path)],
        capture_output=True,
        text=True,
    )


def _kinds(build: str) -> list[str]:
    return [doc["kind"] for doc in yaml.safe_load_all(build) if doc]


@pytest.mark.parametrize("path", [ENVOY_GATEWAY_BASE, ENVOY_GATEWAY_GATEWAY])
def test_envoy_gateway_manifests_build_standalone(path: Path) -> None:
    """The controller and the shared Gateway need no external resource."""
    result = subprocess.run([_kustomize(), "build", str(path)], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


def test_infra_gateway_component_replaces_the_ingress_of_each_of_its_five_services(tmp_path: Path) -> None:
    result = _kustomize_build(
        tmp_path,
        resources=[KEYCLOAK_INGRESS, FORGEJO_INGRESS, MINIO_INGRESS, PROMETHEUS_INGRESS, REGISTRY_INGRESS],
        components=[INFRA_GATEWAY_COMPONENT],
    )

    assert result.returncode == 0, result.stderr
    kinds = _kinds(result.stdout)
    assert kinds.count("HTTPRoute") == 5
    assert "Ingress" not in kinds


def test_mailsink_gateway_component_replaces_its_ingress(tmp_path: Path) -> None:
    result = _kustomize_build(tmp_path, resources=[MAILSINK_INGRESS], components=[MAILSINK_GATEWAY_COMPONENT])

    assert result.returncode == 0, result.stderr
    assert _kinds(result.stdout) == ["HTTPRoute"]


def test_bootstrap_gateway_component_replaces_the_portal_ingress_and_sets_argocd_insecure(tmp_path: Path) -> None:
    result = _kustomize_build(
        tmp_path,
        resources=[OPERATIONS_MANAGER_INGRESS, OPERATIONS_MANAGER_DEPLOYMENT, ARGOCD_CR],
        components=[BOOTSTRAP_GATEWAY_COMPONENT],
    )

    assert result.returncode == 0, result.stderr
    documents = {doc["kind"]: doc for doc in yaml.safe_load_all(result.stdout) if doc}
    assert "Ingress" not in documents
    assert _kinds(result.stdout).count("HTTPRoute") == 2
    argocd = documents["ArgoCD"]
    assert argocd["spec"]["server"]["insecure"] is True
    assert argocd["spec"]["server"]["ingress"]["enabled"] is False


def test_bootstrap_gateway_component_points_opi_at_the_plain_http_argocd(tmp_path: Path) -> None:
    """argocd-server loses its TLS in this stand, so OPI must not keep talking https to it:
    the env below wins over the .env in the configmap."""
    result = _kustomize_build(
        tmp_path,
        resources=[OPERATIONS_MANAGER_INGRESS, OPERATIONS_MANAGER_DEPLOYMENT, ARGOCD_CR],
        components=[BOOTSTRAP_GATEWAY_COMPONENT],
    )

    assert result.returncode == 0, result.stderr
    deployment = next(doc for doc in yaml.safe_load_all(result.stdout) if doc and doc["kind"] == "Deployment")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    env = {entry["name"]: entry.get("value") for entry in container["env"]}
    assert env["ARGOCD_PORT"] == "80"
    assert env["ARGOCD_USE_TLS"] == "false"
    assert env["ARGOCD_VERIFY_SSL"] == "false"
    assert "SOPS_AGE_KEY_FILE" in env, "the patch must merge into the env, not replace it"


@pytest.mark.parametrize(
    "component",
    [BOOTSTRAP_INGRESS_COMPONENT, INFRA_INGRESS_COMPONENT, MAILSINK_INGRESS_COMPONENT],
    ids=["bootstrap", "infra", "mailsink"],
)
def test_ingress_components_stay_empty_so_the_ingress_stand_stays_byte_identical(component: Path) -> None:
    """Once this component gains resources or patches, the ingress stand changes with it and
    the task's "byte-identical" promise no longer holds."""
    kustomization = yaml.safe_load((component / "kustomization.yaml").read_text())

    assert kustomization.get("resources") == []
    assert "patches" not in kustomization
