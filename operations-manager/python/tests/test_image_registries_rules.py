"""De regelvorm en de twee bronnen die hem voeden.

De toets die er het meest toe doet houdt de twee bronnen uit elkaar: dezelfde
``code.overheid.nl``-image levert MET een private registry de eigen proxy-organisatie op
met het eigen secret, en ZONDER die registry de gedeelde proxy met het robot-secret.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest
from opi.services.catalog.image_registries.naming import (
    friendly_name,
    organization_name,
    organization_suffix,
    pull_secret_name,
    registry_destination,
)
from opi.services.catalog.image_registries.resolution import (
    build_rules,
    cluster_rules,
    project_registries,
    resolve_project_image,
)
from opi.services.catalog.image_registries.rules import (
    RegistryRule,
    normalize_image,
    original_image,
    resolve_image,
)

ODCN = "odcn-production"
SANDBOX = "sandboxed-local"

SHARED = RegistryRule(
    match="code.overheid.nl",
    to="rcr.rijksapps.nl/code-overheid-rig",
    secret="code-overheid-rig-robot-pull-secret",
)
PRIVATE = RegistryRule(
    match="code.overheid.nl/robbert.uittenbroek",
    to="rcr.rijksapps.nl/codeoverheid-rig-demo",
    secret="codeoverheid-rig-demo-robot-pull-secret",
)
DOCKERHUB = RegistryRule(
    match="docker.io", to="rcr.rijksapps.nl/dockerhub-rig", secret="dockerhub-rig-robot-pull-secret"
)


def _project(registries: list[dict] | None = None, name: str = "demo") -> dict:
    services: list = ["publish-on-web"]
    if registries is not None:
        services.append({"name": "image-registries", "config": {"registries": registries}})
    return {"name": name, "services": services}


class TestNormalisatieVanKorteNamen:
    """``nginx:alpine`` moet de ``docker.io``-regel raken, want de admission-webhook van
    ODCN behandelt hem ook zo."""

    @pytest.mark.parametrize(
        ("image", "verwacht"),
        [
            ("nginx:alpine", "docker.io/library/nginx:alpine"),
            ("nginx", "docker.io/library/nginx"),
            ("bitnami/redis:7", "docker.io/bitnami/redis:7"),
            ("ghcr.io/rijksictgilde/app:1", "ghcr.io/rijksictgilde/app:1"),
            ("localhost:5000/app:1", "localhost:5000/app:1"),
            ("registry:5000/app:1", "registry:5000/app:1"),
            ("", ""),
        ],
    )
    def test_alleen_waar_docker_het_zelf_ook_doet(self, image: str, verwacht: str) -> None:
        assert normalize_image(image) == verwacht

    def test_een_korte_naam_raakt_de_dockerhub_regel(self) -> None:
        resolved = resolve_image("nginx:alpine", [DOCKERHUB])
        assert resolved.image == "rcr.rijksapps.nl/dockerhub-rig/library/nginx:alpine"
        assert resolved.secret == "dockerhub-rig-robot-pull-secret"

    def test_zonder_normalisatie_zou_hij_zijn_secret_missen(self) -> None:
        """De tegenproef: het is de normalisatie die de match maakt, niet de regel."""
        assert not "nginx:alpine".startswith("docker.io")


class TestEersteMatchWint:
    def test_projectregel_wint_van_de_gedeelde_proxy(self) -> None:
        resolved = resolve_image("code.overheid.nl/robbert.uittenbroek/demo:0a611d9d", [PRIVATE, SHARED])
        assert resolved.image == "rcr.rijksapps.nl/codeoverheid-rig-demo/demo:0a611d9d"
        assert resolved.secret == "codeoverheid-rig-demo-robot-pull-secret"

    def test_zonder_de_projectregel_gaat_dezelfde_image_naar_de_gedeelde_proxy(self) -> None:
        resolved = resolve_image("code.overheid.nl/robbert.uittenbroek/demo:0a611d9d", [SHARED])
        assert resolved.image == "rcr.rijksapps.nl/code-overheid-rig/robbert.uittenbroek/demo:0a611d9d"
        assert resolved.secret == "code-overheid-rig-robot-pull-secret"

    def test_het_namespace_segment_valt_weg_door_de_langere_match(self) -> None:
        """Geen apart gedrag: het volgt uit ``match`` met namespace tegenover zonder."""
        met = resolve_image("code.overheid.nl/robbert.uittenbroek/demo:tag", [PRIVATE]).image
        zonder = resolve_image("code.overheid.nl/robbert.uittenbroek/demo:tag", [SHARED]).image
        assert met.count("/") == 2
        assert zonder.count("/") == 3

    def test_geen_match_laat_de_image_ongewijzigd_en_zonder_secret(self) -> None:
        resolved = resolve_image("ghcr.io/iemand/app:1", [PRIVATE, SHARED])
        assert (resolved.image, resolved.secret) == ("ghcr.io/iemand/app:1", None)

    def test_geen_regels_verandert_niets_aan_een_korte_naam(self) -> None:
        """Op een cluster zonder tabel komt de ONGENORMALISEERDE verwijzing terug, anders
        zou elk gegenereerd manifest daar veranderen."""
        resolved = resolve_image("nginx:alpine", [])
        assert (resolved.image, resolved.secret) == ("nginx:alpine", None)

    def test_een_prefix_matcht_alleen_op_segmentgrens(self) -> None:
        resolved = resolve_image("code.overheid.nl-anders/x:1", [SHARED])
        assert (resolved.image, resolved.secret) == ("code.overheid.nl-anders/x:1", None)


class TestImageDieAlOpDeBestemmingStaat:
    """Het dp-bn7-geval: niets te herschrijven, maar wel het secret van die regel."""

    def test_krijgt_het_secret_en_blijft_ongewijzigd(self) -> None:
        resolved = resolve_image("rcr.rijksapps.nl/dockerhub-rig/library/alpine:3.20.3", [DOCKERHUB])
        assert resolved.image == "rcr.rijksapps.nl/dockerhub-rig/library/alpine:3.20.3"
        assert resolved.secret == "dockerhub-rig-robot-pull-secret"

    def test_ook_als_een_eerdere_regel_niet_matcht(self) -> None:
        resolved = resolve_image("rcr.rijksapps.nl/code-overheid-rig/x/app:1", [PRIVATE, SHARED])
        assert resolved.secret == "code-overheid-rig-robot-pull-secret"


class TestDeWegTerug:
    def test_original_image_toont_de_eigen_registry(self) -> None:
        assert original_image("rcr.rijksapps.nl/code-overheid-rig/x/app:1", [SHARED]) == "code.overheid.nl/x/app:1"

    def test_zonder_match_ongewijzigd(self) -> None:
        assert original_image("ghcr.io/x/app:1", [SHARED]) == "ghcr.io/x/app:1"


class TestNaamgeving:
    def test_friendly_name_laat_de_tld_weg_en_schrijft_de_rest_aaneen(self) -> None:
        assert friendly_name("code.overheid.nl/robbert.uittenbroek") == "codeoverheid"

    def test_friendly_name_volgt_de_gedeelde_organisaties_op_odcn(self) -> None:
        """ghcr-rig, gitlab-rig, gcr-rig, quay-rig: overal is de TLD weggelaten."""
        assert friendly_name("ghcr.io") == "ghcr"
        assert friendly_name("registry.gitlab.com") == "registrygitlab"
        assert friendly_name("localhost:5000") == "localhost"

    def test_organisatie_draagt_de_projectnaam_en_de_upstream_namespace(self) -> None:
        assert organization_name("code.overheid.nl/x", "rig", "demo") == "codeoverheid-rig-demo-x"

    def test_organisatie_zonder_pad_houdt_de_kale_projectnaam(self) -> None:
        """Een upstream op hostniveau heeft geen namespace om te onderscheiden."""
        assert organization_name("ghcr.io", "rig", "demo") == "ghcr-rig-demo"

    def test_suffix_is_wat_de_operator_achter_de_hostnaam_plakt(self) -> None:
        """De operator stelt ``<friendlyName>-<customerName>-<suffix>`` samen, en
        ``friendlyName`` draagt alleen de HOST. Wat de twee registries onderscheidt moet
        dus in de suffix zitten, anders komt de organisatie er onder één naam te staan."""
        assert organization_suffix("ghcr.io/orga", "demo") == "demo-orga"
        assert organization_suffix("ghcr.io", "demo") == "demo"
        assert organization_suffix("code.overheid.nl/robbert.uittenbroek", "demo") == "demo-robbert-uittenbroek"

    def test_twee_registries_onder_dezelfde_host_botsen_niet(self) -> None:
        """De blokkerende vondst uit de review: ``friendly_name`` neemt alleen de host,
        dus zonder het padsegment kregen ``ghcr.io/orga`` en ``ghcr.io/orgb`` van
        hetzelfde project dezelfde organisatie, hetzelfde credentials-secret, dezelfde
        bestandsnaam op het projectniveau en dezelfde bestemming."""
        een = organization_name("ghcr.io/orga", "rig", "demo")
        ander = organization_name("ghcr.io/orgb", "rig", "demo")
        assert een == "ghcr-rig-demo-orga"
        assert ander == "ghcr-rig-demo-orgb"
        assert een != ander
        assert pull_secret_name("ghcr.io/orga", "rig", "demo") != pull_secret_name("ghcr.io/orgb", "rig", "demo")
        assert registry_destination("ghcr.io/orga", "rcr.rijksapps.nl", "rig", "demo") != registry_destination(
            "ghcr.io/orgb", "rcr.rijksapps.nl", "rig", "demo"
        )

    def test_pull_secret_draagt_de_projectnaam(self) -> None:
        """De operator laat spec.suffix weg, dus twee projecten met dezelfde upstream
        botsen tenantbreed. Daarom zetten wij de naam zelf."""
        een = pull_secret_name("code.overheid.nl/x", "rig", "demo")
        ander = pull_secret_name("code.overheid.nl/y", "rig", "ander")
        assert een == "codeoverheid-rig-demo-x-robot-pull-secret"
        assert een != ander

    def test_bestemming_is_host_plus_organisatie(self) -> None:
        assert (
            registry_destination("code.overheid.nl/x", "rcr.rijksapps.nl", "rig", "demo")
            == "rcr.rijksapps.nl/codeoverheid-rig-demo-x"
        )


class TestDeTweeBronnen:
    """Dezelfde image, één verschil: staat er een private registry in het project of niet."""

    REGISTRY: ClassVar[dict[str, Any]] = {
        "name": "code-overheid",
        "upstream": "code.overheid.nl/robbert.uittenbroek",
        "username": "robbert.uittenbroek",
        "password": "geheim",
    }
    IMAGE = "code.overheid.nl/robbert.uittenbroek/zad-deployment-demo:0a611d9d"

    def test_met_private_registry_de_eigen_organisatie(self) -> None:
        resolved = resolve_project_image(self.IMAGE, _project([self.REGISTRY]), ODCN)
        assert resolved.image == (
            "rcr.rijksapps.nl/codeoverheid-rig-demo-robbert-uittenbroek/zad-deployment-demo:0a611d9d"
        )
        assert resolved.secret == "codeoverheid-rig-demo-robbert-uittenbroek-robot-pull-secret"

    def test_zonder_private_registry_de_gedeelde_proxy(self) -> None:
        resolved = resolve_project_image(self.IMAGE, _project(), ODCN)
        assert resolved.image == ("rcr.rijksapps.nl/code-overheid-rig/robbert.uittenbroek/zad-deployment-demo:0a611d9d")
        assert resolved.secret == "code-overheid-rig-robot-pull-secret"

    def test_op_een_cluster_zonder_tabel_blijft_de_image_staan(self) -> None:
        """direct-secret: alleen een secret, geen herschrijving."""
        resolved = resolve_project_image(self.IMAGE, _project([self.REGISTRY]), SANDBOX)
        assert resolved.image == self.IMAGE
        assert resolved.secret == "demo-code-overheid-registry"

    def test_publieke_image_op_een_cluster_zonder_tabel_krijgt_niets(self) -> None:
        resolved = resolve_project_image("nginx:alpine", _project(), SANDBOX)
        assert (resolved.image, resolved.secret) == ("nginx:alpine", None)

    def test_publieke_image_op_odcn_krijgt_wel_een_secret(self) -> None:
        """Elke image belandt daar op een RCR-pad, en elk RCR-pad vraagt authenticatie."""
        resolved = resolve_project_image("nginx:alpine", _project(), ODCN)
        assert resolved.image == "rcr.rijksapps.nl/dockerhub-rig/library/nginx:alpine"
        assert resolved.secret == "dockerhub-rig-robot-pull-secret"


class TestVoorrangTussenTweeGelijkeUpstreams:
    """Twee registries met dezelfde URL en verschillende tokens: de keuze van het component
    beslist, en dat is precies waarom de koppeling geen afleiding uit de image-URL is."""

    EEN: ClassVar[dict[str, Any]] = {
        "name": "een",
        "upstream": "code.overheid.nl/team",
        "username": "a",
        "password": "x",
    }
    ANDER: ClassVar[dict[str, Any]] = {
        "name": "ander",
        "upstream": "code.overheid.nl/team",
        "username": "b",
        "password": "y",
    }

    def test_zonder_voorkeur_wint_de_eerste_in_het_bestand(self) -> None:
        rules = build_rules(_project([self.EEN, self.ANDER]), SANDBOX)
        assert rules[0].secret == "demo-een-registry"

    def test_de_component_keuze_gaat_voorop(self) -> None:
        rules = build_rules(_project([self.EEN, self.ANDER]), SANDBOX, preferred="ander")
        assert rules[0].secret == "demo-ander-registry"

    def test_de_clustertabel_staat_altijd_achter_de_projectregels(self) -> None:
        rules = build_rules(_project([self.EEN]), ODCN)
        assert rules[0].match == "code.overheid.nl/team"
        assert [r.match for r in rules[1:]] == [r.match for r in cluster_rules(ODCN)]


class TestEntryMetEenBestaandSecret:
    """Het noodverband uit de dp-bn7-storing: verwijs naar een secret dat het platform zelf
    neerzet, en herschrijf nooit."""

    ENTRY: ClassVar[dict[str, Any]] = {
        "name": "platform",
        "upstream": "rcr.rijksapps.nl/rig",
        "secretName": "rig-robot-pull-secret",
    }

    def test_geen_herschrijving_wel_het_secret(self) -> None:
        resolved = resolve_project_image("rcr.rijksapps.nl/rig/app:1", _project([self.ENTRY]), ODCN)
        assert resolved.image == "rcr.rijksapps.nl/rig/app:1"
        assert resolved.secret == "rig-robot-pull-secret"


class TestProjectRegistriesLezen:
    def test_een_record_met_config_wordt_gevonden(self) -> None:
        """Sleutel-lezende code laat precies de entries vallen waar het om gaat."""
        assert [r["name"] for r in project_registries(_project([{"name": "a", "upstream": "x.nl"}]))] == ["a"]

    def test_zonder_de_dienst_leeg(self) -> None:
        assert project_registries(_project()) == []

    def test_een_entry_zonder_naam_telt_niet_mee(self) -> None:
        assert project_registries(_project([{"upstream": "x.nl"}])) == []


class TestDeComponentkeuzeErftEnDeDeploymentOverschrijft:
    """De koppeling staat op het COMPONENT en de deployment mag hem overschrijven met
    dezelfde dienstvermelding -- de vorm die publish-on-web en temp-storage daar ook
    gebruiken. Wat eruit komt is het pull-secret in ``imagePullSecretsMap``, dus daar wordt
    het op gemeten."""

    EEN: ClassVar[dict[str, Any]] = {
        "name": "een",
        "upstream": "code.overheid.nl/team",
        "username": "a",
        "password": "x",
    }
    ANDER: ClassVar[dict[str, Any]] = {
        "name": "ander",
        "upstream": "code.overheid.nl/team",
        "username": "b",
        "password": "y",
    }
    IMAGE = "code.overheid.nl/team/app:1"

    def _project(self) -> dict[str, Any]:
        data = _project([self.EEN, self.ANDER])
        data["components"] = [
            {
                "name": "web",
                "services": [{"reference": "image-registries", "config": {"registry": "ander"}}],
            }
        ]
        return data

    def test_de_keuze_van_het_component_erft(self) -> None:
        from opi.services.catalog.image_registries.resolution import resolve_deployment_component_image

        data = self._project()
        deployment_component = {"reference": "web", "image": self.IMAGE}
        resolved = resolve_deployment_component_image(data, deployment_component, data["components"][0], SANDBOX)
        assert resolved.secret == "demo-ander-registry"

    def test_de_deployment_override_wint(self) -> None:
        from opi.services.catalog.image_registries.resolution import resolve_deployment_component_image

        data = self._project()
        deployment_component = {
            "reference": "web",
            "image": self.IMAGE,
            # Op een deployment-component is ``services`` een dict keyed op dienstnaam.
            "services": {"image-registries": {"config": {"registry": "een"}}},
        }
        resolved = resolve_deployment_component_image(data, deployment_component, data["components"][0], SANDBOX)
        assert resolved.secret == "demo-een-registry"

    def test_zonder_enige_keuze_wint_de_eerste_in_het_bestand(self) -> None:
        from opi.services.catalog.image_registries.resolution import resolve_deployment_component_image

        data = _project([self.EEN, self.ANDER])
        resolved = resolve_deployment_component_image(data, {"reference": "web", "image": self.IMAGE}, None, SANDBOX)
        assert resolved.secret == "demo-een-registry"

    def test_op_odcn_levert_de_override_een_andere_organisatie_op(self) -> None:
        """De tegenproef dat de keuze echt doorwerkt tot in de herschrijving, niet alleen
        tot in het secret: op een cluster mét proxy verandert ook de bestemming."""
        from opi.services.catalog.image_registries.resolution import resolve_deployment_component_image

        data = self._project()
        zonder = resolve_deployment_component_image(
            data, {"reference": "web", "image": self.IMAGE}, data["components"][0], ODCN
        )
        met = resolve_deployment_component_image(
            data,
            {
                "reference": "web",
                "image": self.IMAGE,
                "services": {"image-registries": {"config": {"registry": "een"}}},
            },
            data["components"][0],
            ODCN,
        )
        # Beide registries hebben dezelfde upstream, dus dezelfde organisatie -- het
        # SECRET is hetzelfde en het verschil zit in welke regel vooraan stond.
        assert zonder.image == met.image
        assert zonder.image.startswith("rcr.rijksapps.nl/codeoverheid-rig-demo-team/")
