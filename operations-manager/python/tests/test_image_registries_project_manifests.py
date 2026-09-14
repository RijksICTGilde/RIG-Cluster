"""Het projectniveau: wat de dienst image-registries daar neerzet, per backend."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
from typing import Any, ClassVar

import pytest
import yaml
from opi.core.cluster_config import get_image_registries_config
from opi.generation.manifests import ManifestGenerator
from opi.services.catalog.base import ProjectManifestContext
from opi.services.catalog.image_registries import ImageRegistriesService
from opi.services.catalog.image_registries.backends import FILENAME_PREFIX, MissingRegistryCredentialsError
from opi.services.catalog.image_registries.naming import (
    PULL_USERNAME_PLACEHOLDER,
    organization_name,
    upstream_hash,
)
from opi.services.catalog.image_registries.resolution import resolve_project_image
from opi.services.registry import project_manifest_services
from opi.services.services_enums import ServiceType
from opi.utils.age import encrypt_age_content_sync


def _keypair() -> tuple[str, str]:
    """Een echt AGE-sleutelpaar; de tests die het gebruiken slaan over zonder de binary."""
    result = subprocess.run(["age-keygen"], capture_output=True, text=True, check=True)
    lines = result.stdout.splitlines() + result.stderr.splitlines()
    private_key = next(line for line in lines if line.startswith("AGE-SECRET-KEY"))
    public_key = next(line.split(": ", 1)[1].strip() for line in lines if "public key:" in line.lower())
    return public_key, private_key


requires_age = pytest.mark.skipif(
    shutil.which("age") is None or shutil.which("age-keygen") is None,
    reason="age/age-keygen binary not available",
)

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
        spec = service.contribute_project_manifests(_ctx(SANDBOX, [REGISTRY]))[0]
        config = json.loads(spec.values["secret_pairs"][".dockerconfigjson"])
        assert list(config["auths"]) == ["code.overheid.nl/robbert.uittenbroek"]
        auth = base64.b64decode(config["auths"]["code.overheid.nl/robbert.uittenbroek"]["auth"]).decode()
        assert auth == "robbert.uittenbroek:een-token"

    def test_zonder_token_blaast_hij_op(self, service: ImageRegistriesService) -> None:
        """Het model laat zo'n entry niet door; komt hij hier toch, dan niet stil geen secret."""
        naked = {"name": "publiek", "upstream": "code.overheid.nl/open", "username": "u"}
        with pytest.raises(MissingRegistryCredentialsError, match="publiek"):
            service.contribute_project_manifests(_ctx(SANDBOX, [naked]))

    def test_een_bestaand_secret_schrijft_niets(self, service: ImageRegistriesService) -> None:
        existing = {"name": "platform", "upstream": "rcr.rijksapps.nl/rig", "secretName": "rig-robot-pull-secret"}
        assert service.contribute_project_manifests(_ctx(SANDBOX, [existing])) == []


class TestQuayProxyOrganizationBackend:
    """ODCN: een credentials-secret en een Organization met proxyCache."""

    def test_twee_bestanden_de_credentials_en_de_organisatie(self, service: ImageRegistriesService) -> None:
        specs = service.contribute_project_manifests(_ctx(ODCN, [REGISTRY]))
        organisatie = organization_name(REGISTRY["upstream"], "rig", "demo")
        assert [s.filename for s in specs] == [
            f"{FILENAME_PREFIX}{organisatie}-upstream-credentials",
            f"{FILENAME_PREFIX}{organisatie}",
        ]
        assert [s.encrypt for s in specs] == [True, False]

    def test_de_organisatie_draagt_de_projectnaam_en_de_upstream_namespace_als_suffix(
        self, service: ImageRegistriesService
    ) -> None:
        """friendly_name draagt alleen de HOST, dus zonder het padsegment in de suffix
        komen twee registries onder dezelfde host op één organisatie uit."""
        organization = service.contribute_project_manifests(_ctx(ODCN, [REGISTRY]))[1]
        assert organization.values["suffix"] == f"demo-{upstream_hash(REGISTRY['upstream'])}"
        assert organization.values["friendly_name"] == "codeoverheid"
        assert organization.values["upstream"] == "code.overheid.nl/robbert.uittenbroek"

    def test_de_secretnaam_wordt_expliciet_gezet(self, service: ImageRegistriesService) -> None:
        """De operator leidt hem af ZONDER spec.suffix, en dan botsen twee projecten met
        dezelfde upstream tenantbreed op een naam."""
        organization = service.contribute_project_manifests(_ctx(ODCN, [REGISTRY]))[1]
        assert organization.values["pull_secret_name"] == (
            f"{organization_name(REGISTRY['upstream'], 'rig', 'demo')}-robot-pull-secret"
        )

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


class TestTweeRegistriesOnderDezelfdeHost:
    """De blokkerende vondst uit de review.

    ``friendly_name`` neemt alleen de HOST, dus zonder het upstream-padsegment in de
    suffix kwamen ``ghcr.io/orga`` en ``ghcr.io/orgb`` van hetzelfde project op één
    organisatienaam uit. Gevolg was drie keer stil: dezelfde bestandsnaam op het
    projectniveau (de tweede overschreef het credentials-secret van de eerste), dezelfde
    bestemming (twee componenten haalden dezelfde image op), en de keuze per component uit
    D3 die op deze backend niets meer deed.
    """

    EEN: ClassVar[dict[str, Any]] = {"name": "een", "upstream": "ghcr.io/orga", "username": "a", "password": "token-a"}
    ANDER: ClassVar[dict[str, Any]] = {
        "name": "ander",
        "upstream": "ghcr.io/orgb",
        "username": "b",
        "password": "token-b",
    }

    def test_vier_bestanden_en_geen_enkele_botsing(self, service: ImageRegistriesService) -> None:
        specs = service.contribute_project_manifests(_ctx(ODCN, [self.EEN, self.ANDER]))
        namen = [spec.filename for spec in specs]
        orga = organization_name(self.EEN["upstream"], "rig", "demo")
        orgb = organization_name(self.ANDER["upstream"], "rig", "demo")
        assert namen == [
            f"{FILENAME_PREFIX}{orga}-upstream-credentials",
            f"{FILENAME_PREFIX}{orga}",
            f"{FILENAME_PREFIX}{orgb}-upstream-credentials",
            f"{FILENAME_PREFIX}{orgb}",
        ]
        assert len(set(namen)) == len(namen)

    def test_elk_token_landt_in_zijn_eigen_secret(self, service: ImageRegistriesService) -> None:
        """De tegenproef op de meting uit de review: eerst gebruikersnaam a, daarna b, op
        hetzelfde pad, dan is er van a niets meer over."""
        specs = service.contribute_project_manifests(_ctx(ODCN, [self.EEN, self.ANDER]))
        paren = {spec.filename: spec.values.get("secret_pairs") for spec in specs if spec.encrypt}
        orga = organization_name(self.EEN["upstream"], "rig", "demo")
        orgb = organization_name(self.ANDER["upstream"], "rig", "demo")
        assert paren[f"{FILENAME_PREFIX}{orga}-upstream-credentials"] == {
            "username": "a",
            "password": "token-a",
        }
        assert paren[f"{FILENAME_PREFIX}{orgb}-upstream-credentials"] == {
            "username": "b",
            "password": "token-b",
        }

    def test_de_twee_images_gaan_naar_verschillende_organisaties(self) -> None:
        """En daarmee doet de keuze per component weer iets: twee regels, twee
        bestemmingen, twee secrets."""
        data = _ctx(ODCN, [self.EEN, self.ANDER]).project_data
        een = resolve_project_image("ghcr.io/orga/app:1", data, ODCN)
        ander = resolve_project_image("ghcr.io/orgb/app:1", data, ODCN)
        assert een.image == f"rcr.rijksapps.nl/{organization_name(self.EEN['upstream'], 'rig', 'demo')}/app:1"
        assert ander.image == f"rcr.rijksapps.nl/{organization_name(self.ANDER['upstream'], 'rig', 'demo')}/app:1"
        assert een.secret != ander.secret

    def test_dezelfde_upstream_twee_keer_levert_geen_stille_overschrijving(
        self, service: ImageRegistriesService
    ) -> None:
        """Eén organisatie per project per upstream-namespace (D2), dus twee entries met
        dezelfde upstream KUNNEN geen twee organisaties zijn. Dan wint de eerste, net als
        in de regellijst, en niet de laatste die het bestand overschrijft."""
        tweede = {**self.EEN, "name": "kopie", "username": "b", "password": "token-b"}
        specs = service.contribute_project_manifests(_ctx(ODCN, [self.EEN, tweede]))
        orga = organization_name(self.EEN["upstream"], "rig", "demo")
        assert [spec.filename for spec in specs] == [
            f"{FILENAME_PREFIX}{orga}-upstream-credentials",
            f"{FILENAME_PREFIX}{orga}",
        ]
        assert specs[0].values["secret_pairs"] == {"username": "a", "password": "token-a"}


class TestDeGebruikersnaamIsOptioneel:
    """RC-187: een entry zonder gebruikersnaam schrijft wel degelijk een secret.

    De dockerconfigjson draagt per registry een ``auth`` van
    ``base64(gebruikersnaam:wachtwoord)``; er is geen veld voor alleen een token. De
    plaatshouder ontstaat daarom HIER, bij het bouwen van het manifest, en staat niet in het
    projectbestand -- zie ``TestDeGebruikersnaamBlijftLeegInHetProjectbestand`` in
    ``test_image_registries_schema_guards.py`` voor die kant.
    """

    ZONDER: ClassVar[dict[str, Any]] = {
        "name": "code-overheid",
        "upstream": "code.overheid.nl/robbert.uittenbroek",
        "password": "een-token",
    }

    def test_het_directe_secret_draagt_een_volledig_paar(self, service: ImageRegistriesService) -> None:
        spec = service.contribute_project_manifests(_ctx(SANDBOX, [self.ZONDER]))[0]
        config = json.loads(spec.values["secret_pairs"][".dockerconfigjson"])
        auth = base64.b64decode(config["auths"]["code.overheid.nl/robbert.uittenbroek"]["auth"]).decode()
        assert auth == f"{PULL_USERNAME_PLACEHOLDER}:een-token"

    def test_het_upstream_credential_op_odcn_draagt_hem_ook(self, service: ImageRegistriesService) -> None:
        specs = service.contribute_project_manifests(_ctx(ODCN, [self.ZONDER]))
        assert specs[0].values["secret_pairs"] == {
            "username": PULL_USERNAME_PLACEHOLDER,
            "password": "een-token",
        }

    @pytest.mark.parametrize("leeg", [None, ""], ids=["afwezig", "leeg"])
    def test_een_lege_waarde_telt_als_geen_waarde(self, service: ImageRegistriesService, leeg: str | None) -> None:
        """Het formulier stuurt een leeg veld als lege string, de API laat de sleutel weg."""
        entry = {**self.ZONDER} if leeg is None else {**self.ZONDER, "username": leeg}
        spec = service.contribute_project_manifests(_ctx(SANDBOX, [entry]))[0]
        config = json.loads(spec.values["secret_pairs"][".dockerconfigjson"])
        auth = base64.b64decode(config["auths"]["code.overheid.nl/robbert.uittenbroek"]["auth"]).decode()
        assert auth.split(":", 1)[0] == PULL_USERNAME_PLACEHOLDER

    def test_een_ingevulde_gebruikersnaam_wordt_niet_vervangen(self, service: ImageRegistriesService) -> None:
        """De tegenproef: met een naam erin komt die naam in het paar, niet de plaatshouder."""
        spec = service.contribute_project_manifests(_ctx(SANDBOX, [REGISTRY]))[0]
        config = json.loads(spec.values["secret_pairs"][".dockerconfigjson"])
        auth = base64.b64decode(config["auths"]["code.overheid.nl/robbert.uittenbroek"]["auth"]).decode()
        assert auth == "robbert.uittenbroek:een-token"


class TestQuayProxyOrganizationBackendZonderInloggegevens:
    def test_zonder_token_blaast_hij_op(self, service: ImageRegistriesService) -> None:
        """Net als bij het directe secret: geen stille organisatie zonder credentials."""
        naked = {"name": "publiek", "upstream": "code.overheid.nl/open", "username": "u"}
        with pytest.raises(MissingRegistryCredentialsError, match="publiek"):
            service.contribute_project_manifests(_ctx(ODCN, [naked]))


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
            f"{organization_name(REGISTRY['upstream'], 'rig', 'demo')}-upstream-credentials"
        )

    def test_de_api_version_komt_uit_de_clusterconfig(self, tmp_path: Any, service: ImageRegistriesService) -> None:
        """Een platformfeit, geen vaste waarde in het sjabloon: als de groep/versie van de
        CRD afwijkt is dat een regel clusterconfig en niet een sjabloonwijziging."""
        manifest = self._render(tmp_path, service)
        assert manifest["apiVersion"] == get_image_registries_config(ODCN)["organization_api_version"]

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


@requires_age
class TestDeDrieOpslagvormenVanHetToken:
    """Wat er in het secret komt te staan, per opslagvorm van ``password``.

    Het veld draagt er drie (``AGE_ENCRYPTED_OR_PLAIN_PATTERN``): het armored blok, de
    eenregelige ``base64+age:``-vorm en ``plain:``. Alle drie komen langs de poort die een
    save valideert, dus alle drie kunnen in een projectbestand staan, en de eenregelige
    vorm is de HUISVORM voor eenregelige geheimen (de repository-password, de api-key en de
    projectsleutel dragen hem allemaal). Code die alleen op de armored markering toetst zet
    de andere twee LETTERLIJK in de ``.dockerconfigjson``: geen fout, wel een credential dat
    niet klopt.

    Alles hier draait tegen de echte ``age``-binary; een mock zou "wel ontsleuteld" en "de
    cijfertekst doorgegeven" niet uit elkaar kunnen houden, en dat is precies het verschil
    dat deze tests meten.
    """

    TOKEN = "ghp_HET_ECHTE_TOKEN"

    @pytest.fixture
    def project(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        system_public, system_private = _keypair()
        project_public, project_private = _keypair()
        monkeypatch.setattr("opi.core.config.settings.SOPS_AGE_PRIVATE_KEY", system_private)
        return {
            "age-public-key": project_public,
            "age-private-key": encrypt_age_content_sync(project_private, system_public),
        }

    def _ctx_met_config(self, cluster: str, registry: dict[str, Any], config: dict[str, Any]) -> ProjectManifestContext:
        ctx = _ctx(cluster, [registry])
        ctx.project_data["config"] = config
        return ctx

    def _vormen(self, project: dict[str, Any]) -> dict[str, str]:
        armored = encrypt_age_content_sync(self.TOKEN, project["age-public-key"])
        return {
            "armored": armored,
            "base64+age": "base64+age:" + base64.b64encode(armored.encode()).decode(),
            "plain": f"plain:{self.TOKEN}",
        }

    @pytest.mark.parametrize("vorm", ["armored", "base64+age", "plain"])
    def test_het_pull_secret_draagt_het_echte_token(
        self, service: ImageRegistriesService, project: dict[str, Any], vorm: str
    ) -> None:
        registry = {**REGISTRY, "password": self._vormen(project)[vorm]}
        specs = service.contribute_project_manifests(self._ctx_met_config(SANDBOX, registry, project))

        (spec,) = specs
        docker_config = json.loads(spec.values["secret_pairs"][".dockerconfigjson"])
        auth = docker_config["auths"]["code.overheid.nl/robbert.uittenbroek"]["auth"]
        gebruiker, _, wachtwoord = base64.b64decode(auth).decode().partition(":")
        assert gebruiker == "robbert.uittenbroek"
        assert wachtwoord == self.TOKEN

    @pytest.mark.parametrize("vorm", ["armored", "base64+age", "plain"])
    def test_het_upstream_credentials_secret_draagt_het_echte_token(
        self, service: ImageRegistriesService, project: dict[str, Any], vorm: str
    ) -> None:
        """Dezelfde waarde op de andere backend: daar gaat hij naar Quay, dat er upstream
        401 mee krijgt en dat vertaalt naar ``name unknown: repository not found``."""
        registry = {**REGISTRY, "password": self._vormen(project)[vorm]}
        specs = service.contribute_project_manifests(self._ctx_met_config(ODCN, registry, project))

        credentials = next(spec for spec in specs if spec.encrypt)
        assert credentials.values["secret_pairs"]["password"] == self.TOKEN

    def test_zonder_projectsleutel_stopt_het_schrijven(self, service: ImageRegistriesService) -> None:
        """Stil geen secret schrijven levert een deployment op die aan de pull blijft
        hangen zonder dat er iets in de weg stond; dit hoort een fout te zijn."""
        registry = {**REGISTRY, "password": "-----BEGIN AGE ENCRYPTED FILE-----\nx\n-----END AGE ENCRYPTED FILE-----"}

        with pytest.raises(ValueError, match="age-private-key"):
            service.contribute_project_manifests(self._ctx_met_config(SANDBOX, registry, {}))
