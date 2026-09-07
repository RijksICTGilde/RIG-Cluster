"""De eigen serviceaccount per project, en waarom pods er op draaien.

De ``default`` serviceaccount draagt elk pull-secret dat het platform in de namespace
repliceert -- ook dat van de proxy-organisatie van een ANDER project. Daarop draaien maakt
een private registry alleen op papier prive, en laat kubelet uit negen secrets voor
dezelfde host de juiste vissen. Vandaar een eigen serviceaccount zonder pull-secrets, en
elke gegenereerde podspec draagt de secrets die hij zelf nodig heeft.
"""

from __future__ import annotations

import os
from typing import Any

import yaml
from opi.generation.manifests import ManifestGenerator, render_template
from opi.services.catalog.base import ProjectManifestContext
from opi.services.catalog.platform import PlatformService
from opi.services.registry import project_manifest_services
from opi.services.services_enums import ServiceType
from opi.utils.naming import generate_project_service_account_name

MANIFESTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "manifests")


def _ctx(project: str = "demo") -> ProjectManifestContext:
    return ProjectManifestContext(
        project_name=project,
        project_data={"name": project, "services": []},
        cluster="odcn-production",
        namespace=f"rig-prd-{project}",
    )


class TestDePlatformDienstDraagtHem:
    def test_hij_meldt_zich_bij_de_projectemitter(self) -> None:
        assert ServiceType.PLATFORM in {s.service_type for s in project_manifest_services()}

    def test_elk_project_krijgt_hem_ook_zonder_eigen_registries(self) -> None:
        """Bij het platform en niet bij image-registries: een dienst die alleen bijdraagt
        als er registries in staan zou hem juist daar laten ontbreken."""
        specs = PlatformService().contribute_project_manifests(_ctx())
        assert len(specs) == 1
        assert specs[0].values["name"] == "demo-sa"
        assert specs[0].values["namespace"] == "rig-prd-demo"

    def test_de_bestandsnaam_is_weer_op_te_ruimen(self) -> None:
        spec = PlatformService().contribute_project_manifests(_ctx())[0]
        assert spec.filename.startswith(f"{ServiceType.PLATFORM.value}-")

    def test_hij_staat_niet_in_een_versleuteld_bestand(self) -> None:
        """Er staat geen geheim in; SOPS eromheen zou hem alleen onleesbaar maken."""
        assert PlatformService().contribute_project_manifests(_ctx())[0].encrypt is False


class TestHetGerenderdeManifest:
    def _render(self, tmp_path: Any) -> dict[str, Any]:
        spec = PlatformService().contribute_project_manifests(_ctx())[0]
        path = ManifestGenerator().create_manifest_file(
            template_path=os.path.join(MANIFESTS_DIR, spec.template_path),
            values=spec.values,
            output_dir=str(tmp_path),
            output_filename=spec.filename,
        )
        with open(path) as handle:
            return yaml.safe_load(handle)

    def test_het_is_een_serviceaccount_zonder_pull_secrets(self, tmp_path: Any) -> None:
        manifest = self._render(tmp_path)
        assert manifest["kind"] == "ServiceAccount"
        assert manifest["metadata"]["name"] == "demo-sa"
        assert "imagePullSecrets" not in manifest

    def test_hij_verandert_verder_niets_aan_de_pod(self, tmp_path: Any) -> None:
        """Alleen de erfenis van andermans pull-secrets is het punt. De pods draaiden tot
        nu toe op de default serviceaccount, die de API-token wel aankoppelt; hem hier
        uitzetten zou elke pod die de Kubernetes-API aanspreekt stil zijn token afnemen."""
        assert "automountServiceAccountToken" not in self._render(tmp_path)


class TestDePodsDraaienErOp:
    def test_de_deployment_zet_de_serviceaccount(self) -> None:
        rendered = render_template(
            "deployment.yaml.jinja",
            {
                "name": "demo-web",
                "deployment_name": "prod",
                "component_name": "web",
                "namespace": "rig-prd-demo",
                "project": {"name": "demo"},
                "cluster": "odcn-production",
                "imageURL": "ghcr.io/org/web:1",
                "replicas": 1,
                "pod_replacement_mode": "RollingUpdate",
                "application_port": 8080,
                "service_port": 8080,
                "inbound_ports": [8080],
                "service_account_name": "demo-sa",
            },
        )
        assert yaml.safe_load(rendered)["spec"]["template"]["spec"]["serviceAccountName"] == "demo-sa"

    def test_zonder_de_variabele_blijft_het_weg(self) -> None:
        """De tegenproef: het is de variabele die de regel neerzet, niet het sjabloon."""
        rendered = render_template(
            "deployment.yaml.jinja",
            {
                "name": "demo-web",
                "deployment_name": "prod",
                "component_name": "web",
                "namespace": "rig-prd-demo",
                "project": {"name": "demo"},
                "cluster": "odcn-production",
                "imageURL": "ghcr.io/org/web:1",
                "replicas": 1,
                "pod_replacement_mode": "RollingUpdate",
                "application_port": 8080,
                "service_port": 8080,
                "inbound_ports": [8080],
            },
        )
        assert "serviceAccountName" not in yaml.safe_load(rendered)["spec"]["template"]["spec"]

    def test_de_job_en_de_console_draaien_er_ook_op(self) -> None:
        """De run-bundels (job, db-console) draaien in de projectnamespace, dus zij horen
        net zo goed van de default serviceaccount af."""
        job = render_template(
            "job-pod.yaml.jinja",
            {
                "name": "demo-run",
                "namespace": "rig-prd-demo",
                "project": {"name": "demo"},
                "cluster": "odcn-production",
                "target_deployment": "prod",
                "image": "ghcr.io/org/tool:1",
                "service_account_name": "demo-sa",
            },
        )
        assert yaml.safe_load(job)["spec"]["serviceAccountName"] == "demo-sa"

        console = render_template(
            "db-console-pod.yaml.jinja",
            {
                "name": "dbconsole-demo",
                "namespace": "rig-prd-demo",
                "project": {"name": "demo"},
                "cluster": "odcn-production",
                "target_deployment": "prod",
                "ttl_seconds": 3600,
                "tool_image": "ghcr.io/org/tool:1",
                "application_port": 8081,
                "tool_args": ["--bind=0.0.0.0"],
                "secret_name": "dbconsole-demo",
                "hostname": "dbconsole-demo.example.com",
                "authorization_wall": {
                    "issuer_url": "https://keycloak.example.com/realms/demo",
                    "client_id": "demo",
                    "keycloak_secret_name": "demo-keycloak",
                    "cookie_secret_name": "demo-cookie",
                    "authenticated_emails_configmap": "demo-emails",
                },
                "service_account_name": "demo-sa",
            },
        )
        assert yaml.safe_load(console)["spec"]["serviceAccountName"] == "demo-sa"


class TestDeNaam:
    def test_hij_draagt_de_projectnaam(self) -> None:
        assert generate_project_service_account_name("mijn-project") == "mijn-project-sa"

    def test_hij_is_een_geldige_kubernetes_naam(self) -> None:
        assert generate_project_service_account_name("Mijn_Project") == "mijn-project-sa"
