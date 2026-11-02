"""RC-357: de odcn-staging-overlay zet OPI op TLS naar ArgoCD, net als odcn-production.

RC-315 deed dat voor odcn-production en sandboxed-local, en zette in base een mount van de
ConfigMap `argocd-ca-bundle`. Die ConfigMap stond niet in de staging-overlay, dus een nieuwe
uitrol daar bleef hangen in ContainerCreating. De overlay wordt echt gerenderd: een toets op
de losse bestanden zou niet zien dat de ConfigMap buiten `resources` is gebleven.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
KUSTOMIZE_ROOT = REPO_ROOT / "bootstrap/rig-system/kustomize"
BASE_DEPLOYMENT = KUSTOMIZE_ROOT / "operations-manager/base/deployment.yaml"
ARGOCD_CR = KUSTOMIZE_ROOT / "overlays/odcn-staging/argocd-deployment.yaml"

BUNDEL_PAD = "/etc/ssl/argocd/service-ca.crt"


@pytest.fixture(scope="module")
def staging_overlay() -> list[dict]:
    """De gerenderde odcn-staging-overlay van de operations-manager."""
    if shutil.which("kustomize") is None:
        pytest.skip("kustomize ontbreekt")
    proc = subprocess.run(
        ["kustomize", "build", "operations-manager/overlays/odcn-staging"],
        cwd=KUSTOMIZE_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return [doc for doc in yaml.safe_load_all(proc.stdout) if doc]


def _resource(documenten: list[dict], kind: str, naam: str) -> dict:
    for doc in documenten:
        if doc.get("kind") == kind and doc["metadata"]["name"] == naam:
            return doc
    raise AssertionError(f"{kind}/{naam} staat niet in de gerenderde overlay")


def _env_regels(documenten: list[dict]) -> dict[str, str]:
    configmap = _resource(documenten, "ConfigMap", "operations-manager-config")
    regels = (r.strip() for r in configmap["data"][".env"].splitlines())
    return dict(r.split("=", 1) for r in regels if "=" in r and not r.startswith("#"))


class TestDeCABundelOpStaging:
    def test_de_configmap_staat_in_de_overlay(self, staging_overlay: list[dict]) -> None:
        bundel = _resource(staging_overlay, "ConfigMap", "argocd-ca-bundle")

        annotaties = bundel["metadata"].get("annotations", {})
        assert annotaties.get("service.beta.openshift.io/inject-cabundle") == "true"
        assert bundel["metadata"]["namespace"] == "rig-staging-operations"

    def test_de_opi_deployment_mount_hem(self, staging_overlay: list[dict]) -> None:
        deployment = _resource(staging_overlay, "Deployment", "operations-manager")
        spec = deployment["spec"]["template"]["spec"]

        volume = next(v for v in spec["volumes"] if v["name"] == "argocd-ca-bundle")
        assert volume["configMap"]["name"] == "argocd-ca-bundle"

        mount = next(m for m in spec["containers"][0]["volumeMounts"] if m["name"] == "argocd-ca-bundle")
        assert mount["mountPath"] == "/etc/ssl/argocd"


class TestDeInstellingenVanOPI:
    @pytest.mark.parametrize(
        ("sleutel", "waarde"),
        [
            ("ARGOCD_HOST", "argocd-server.rig-staging-operations.svc"),
            ("ARGOCD_PORT", "443"),
            ("ARGOCD_USE_TLS", "true"),
            ("ARGOCD_VERIFY_SSL", "true"),
            ("ARGOCD_CA_BUNDLE", BUNDEL_PAD),
        ],
    )
    def test_de_configmap_zet_tls(self, staging_overlay: list[dict], sleutel: str, waarde: str) -> None:
        assert _env_regels(staging_overlay)[sleutel] == waarde

    def test_de_host_is_de_volledige_servicenaam(self, staging_overlay: list[dict]) -> None:
        """De service-CA zet alleen de lange naam in de SAN-lijst, en de hostnaam wordt gecontroleerd."""
        assert _env_regels(staging_overlay)["ARGOCD_HOST"].endswith(".svc")


class TestDeMountFaaltNietHard:
    def test_de_bundel_is_optioneel_in_base(self) -> None:
        """Zonder optional blijft een cluster zonder ConfigMap in ContainerCreating hangen."""
        deployment = yaml.safe_load(BASE_DEPLOYMENT.read_text())
        volumes = deployment["spec"]["template"]["spec"]["volumes"]

        volume = next(v for v in volumes if v["name"] == "argocd-ca-bundle")
        assert volume["configMap"]["optional"] is True


class TestDeArgoCDServerOpStaging:
    @pytest.fixture(scope="class")
    def server(self) -> dict:
        return yaml.safe_load(ARGOCD_CR.read_text())["spec"]["server"]

    def test_de_server_serveert_tls(self, server: dict) -> None:
        assert server["insecure"] is False

    def test_de_route_zet_tls_opnieuw_op_naar_de_server(self, server: dict) -> None:
        """edge zou tot de router versleutelen en daarbinnen plain HTTP spreken."""
        assert server["route"]["tls"]["termination"] == "reencrypt"
