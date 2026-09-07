"""Het projectniveau: wat de dienst image-registries daar neerzet, per backend.

Door data gedreven: geen registries in de config betekent geen enkel bestand, dus ook geen
bijdrage aan het projectniveau. Wat een registry wordt hangt af van de backend van het
cluster, en dat is het enige dat per platform verschilt.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
import yaml
from opi.generation.manifests import ManifestGenerator
from opi.services.catalog.base import ProjectManifestContext
from opi.services.catalog.image_registries import ImageRegistriesService
from opi.services.catalog.image_registries.backends import FILENAME_PREFIX
from opi.services.registry import project_manifest_services
from opi.services.services_enums import ServiceType

ODCN = "odcn-production"
SANDBOX = "sandboxed-local"

REGISTRY = {
    "name": "code-overheid",
    "upstream": "code.overheid.nl/robbert.uittenbroek",
    "username": "robbert.uittenbroek",
    "password": "een-token",
}


def _ctx(cluster: str, registries: list[dict[str, Any]] | None = None) -> ProjectManifestContext:
    services: list[Any] = ["publish-on-web"]
    if registries is not None:
        services.append({"name": "image-registries", "config": {"registries": registries}})
    return ProjectManifestContext(
        project_name="demo",
        project_data={"name": "demo", "services": services},
        cluster=cluster,
        namespace="rig-prd-demo",
    )


@pytest.fixture
def service() -> ImageRegistriesService:
    return ImageRegistriesService()


class TestDeHaakIsGeneriek:
    def test_de_dienst_meldt_zich_bij_de_emitter(self) -> None:
        """De registry verzamelt wie de haak overschrijft; er is geen tweede lijst."""
        assert ServiceType.IMAGE_REGISTRIES in {s.service_type for s in project_manifest_services()}

    def test_een_dienst_die_niets_bijdraagt_staat_er_niet_in(self) -> None:
        assert ServiceType.KEYCLOAK not in {s.service_type for s in project_manifest_services()}


class TestZonderRegistriesGebeurtErNiets:
    def test_dienst_niet_gekozen(self, service: ImageRegistriesService) -> None:
        assert service.contribute_project_manifests(_ctx(ODCN)) == []

    def test_dienst_gekozen_maar_leeg(self, service: ImageRegistriesService) -> None:
        assert service.contribute_project_manifests(_ctx(ODCN, [])) == []


class TestDirectSecretBackend:
    """kind en sandbox: een dockerconfigjson-secret, de image blijft ongewijzigd."""

    def test_een_versleuteld_secret_met_de_projectnaam(self, service: ImageRegistriesService) -> None:
        specs = service.contribute_project_manifests(_ctx(SANDBOX, [REGISTRY]))
        assert len(specs) == 1
        spec = specs[0]
        assert spec.filename == f"{FILENAME_PREFIX}demo-code-overheid-registry"
        assert spec.encrypt is True
        assert spec.values["secret_k8s_type"] == "kubernetes.io/dockerconfigjson"

    def test_de_upstream_inclusief_pad_is_de_sleutel_in_auths(self, service: ImageRegistriesService) -> None:
        """kubelet kiest de meest specifieke match; dat is wat het veld altijd al droeg."""
        import base64
        import json

        spec = service.contribute_project_manifests(_ctx(SANDBOX, [REGISTRY]))[0]
        config = json.loads(spec.values["secret_pairs"][".dockerconfigjson"])
        assert list(config["auths"]) == ["code.overheid.nl/robbert.uittenbroek"]
        auth = base64.b64decode(config["auths"]["code.overheid.nl/robbert.uittenbroek"]["auth"]).decode()
        assert auth == "robbert.uittenbroek:een-token"

    def test_zonder_inloggegevens_geen_secret(self, service: ImageRegistriesService) -> None:
        naked = {"name": "publiek", "upstream": "code.overheid.nl/open"}
        assert service.contribute_project_manifests(_ctx(SANDBOX, [naked])) == []

    def test_een_bestaand_secret_schrijft_niets(self, service: ImageRegistriesService) -> None:
        existing = {"name": "platform", "upstream": "rcr.rijksapps.nl/rig", "secretName": "rig-robot-pull-secret"}
        assert service.contribute_project_manifests(_ctx(SANDBOX, [existing])) == []


class TestQuayProxyOrganizationBackend:
    """ODCN: een credentials-secret en een Organization met proxyCache."""

    def test_twee_bestanden_de_credentials_en_de_organisatie(self, service: ImageRegistriesService) -> None:
        specs = service.contribute_project_manifests(_ctx(ODCN, [REGISTRY]))
        assert [s.filename for s in specs] == [
            f"{FILENAME_PREFIX}codeoverheid-rig-demo-upstream-credentials",
            f"{FILENAME_PREFIX}codeoverheid-rig-demo",
        ]
        assert [s.encrypt for s in specs] == [True, False]

    def test_de_organisatie_draagt_de_projectnaam_als_suffix(self, service: ImageRegistriesService) -> None:
        organization = service.contribute_project_manifests(_ctx(ODCN, [REGISTRY]))[1]
        assert organization.values["suffix"] == "demo"
        assert organization.values["friendly_name"] == "codeoverheid"
        assert organization.values["upstream"] == "code.overheid.nl/robbert.uittenbroek"

    def test_de_secretnaam_wordt_expliciet_gezet(self, service: ImageRegistriesService) -> None:
        """De operator leidt hem af ZONDER spec.suffix, en dan botsen twee projecten met
        dezelfde upstream tenantbreed op een naam."""
        organization = service.contribute_project_manifests(_ctx(ODCN, [REGISTRY]))[1]
        assert organization.values["pull_secret_name"] == "codeoverheid-rig-demo-robot-pull-secret"

        ander = ProjectManifestContext(
            project_name="ander",
            project_data={
                "name": "ander",
                "services": [{"name": "image-registries", "config": {"registries": [REGISTRY]}}],
            },
            cluster=ODCN,
            namespace="rig-prd-ander",
        )
        assert (
            service.contribute_project_manifests(ander)[1].values["pull_secret_name"]
            != organization.values["pull_secret_name"]
        )

    def test_zonder_inloggegevens_alleen_de_organisatie(self, service: ImageRegistriesService) -> None:
        naked = {"name": "publiek", "upstream": "code.overheid.nl/open"}
        specs = service.contribute_project_manifests(_ctx(ODCN, [naked]))
        assert len(specs) == 1
        assert specs[0].values["credentials_secret"] is None


class TestDeGerenderdeOrganisatie:
    """Het sjabloon zelf, want een naam in values zegt nog niets over wat er in git komt."""

    def _render(self, tmp_path: Any, service: ImageRegistriesService) -> dict[str, Any]:
        spec = service.contribute_project_manifests(_ctx(ODCN, [REGISTRY]))[1]
        generator = ManifestGenerator()
        template_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "manifests")
        path = generator.create_manifest_file(
            template_path=os.path.join(template_dir, spec.template_path),
            values=spec.values,
            output_dir=str(tmp_path),
            output_filename=spec.filename,
        )
        with open(path) as f:
            return yaml.safe_load(f)

    def test_de_proxycache_wijst_naar_de_upstream_met_pad(self, tmp_path: Any, service: ImageRegistriesService) -> None:
        manifest = self._render(tmp_path, service)
        assert manifest["kind"] == "Organization"
        assert manifest["spec"]["proxyCache"]["upstreamRegistry"] == "code.overheid.nl/robbert.uittenbroek"
        assert manifest["spec"]["proxyCache"]["credentialsSecret"]["name"] == (
            "codeoverheid-rig-demo-upstream-credentials"
        )

    def test_rotatie_staat_altijd_aan(self, tmp_path: Any, service: ImageRegistriesService) -> None:
        """rotation.enabled: false doet niet wat de documentatie belooft: het token krijgt
        alsnog retentionDays en verloopt, zonder dat iemand het ververst."""
        manifest = self._render(tmp_path, service)
        assert manifest["spec"]["robot"]["rotation"] == {"enabled": True, "retentionDays": 90}


class TestElkBestandIsWeerOpTeRuimen:
    def test_de_bestandsnaam_begint_met_de_dienstnaam(self, service: ImageRegistriesService) -> None:
        """Anders haalt de symmetrische prune hem nooit weg als de dienst uitgaat."""
        for cluster in (SANDBOX, ODCN):
            for spec in service.contribute_project_manifests(_ctx(cluster, [REGISTRY])):
                assert spec.filename.startswith(f"{ServiceType.IMAGE_REGISTRIES.value}-")
