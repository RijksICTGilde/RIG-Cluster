"""Twee dingen die het platform weet en de gebruiker niet te horen kreeg.

Uit de praktijkrondes van de zad-cli, bevinding 21 en 22. Ze delen een vorm: de informatie
staat in de code of in de clusterconfiguratie, en de enige manier om erachter te komen was
het te proberen en te zien dat het niet werkte.

**21 -- de authorization-wall is niet te binden zonder projectconfiguratie.** De dienst is
COMPONENT-gebonden maar draagt zijn enige instelling op PROJECTniveau, en hij mag zichzelf
niet aanmelden (``allows_implicit_project_selection`` staat op False, met een reden:
een muur voor de applicatie zetten is een beveiligingsbesluit). De API weigerde dus, en dat
is de goede uitkomst -- maar de melding noemde alleen de reden ("they need project-level
configuration that cannot be assumed") en niet de weg eruit: geen endpoint, geen
verwijzing naar wat er in de body hoort, en geen woord over de drie eisen uit ``requires``
die daarna alsnog een voor een omvielen.

**22 -- een eigen domein krijgt geen certificaat in de sandbox.** Gemeten: het cluster
draait een NEP cert-manager-CRD zonder controller (``infrastructure/bootstrap/infrastructure/cert-manager/fake-crd``,
Taskfile), dus de Issuer wordt aangemaakt, meldt Ready, en er wordt nooit iets uitgegeven.
Er is geen Certificate-resource dat blijft hangen -- die CRD bestaat er niet eens. Alles
staat op groen en de bezoeker krijgt het verkeerde certificaat. En het valt precies samen
met het moment dat de goedkeuringsmelding VERDWIJNT: zolang het domein niet is goedgekeurd
publiceert de deployment op het clusteradres en is er niets aan de hand.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.core.cluster_config import supports_custom_domain_certificates
from opi.forms.editables.enforcers import DomainConfigEnforcer, FieldWarning
from opi.manager.project_manager import ProjectManager
from opi.services.catalog.base import ConfigLayer, config_endpoint_path
from opi.services.catalog.publish_on_web.domain_config import custom_domain_certificate_note
from opi.services.registry import SERVICES, get_service
from opi.services.services import ServiceAdapter, ServiceValidationError, unmet_service_requirements
from opi.services.services_enums import ServiceType

# --------------------------------------------------------------------------------------
# Bevinding 21: de weigering moet uitvoerbaar zijn
# --------------------------------------------------------------------------------------


def _project(services: list[Any] | None = None) -> dict[str, Any]:
    return {
        "schema-version": 2,
        "name": "mijn-project",
        "description": "test",
        "clusters": ["odcn-production"],
        "users": [{"email": "a@b.nl", "role": "admin"}],
        "services": services if services is not None else [],
        "components": [
            {
                "name": "app",
                "type": "single",
                "ports": {"inbound": [8080], "outbound": [80, 443]},
                "path": [{"match": "/"}],
                "services": [],
            }
        ],
    }


class TestTheRefusalIsActionable:
    """Wat de aanroeper krijgt als hij de auth wall aan een component hangt.

    HERZIEN NA DE OORSPRONKELIJKE REPARATIE. Eerst weigerde dit onvoorwaardelijk, met als
    redenering dat een muur voor je applicatie een projectbesluit is. Dat besluit is
    teruggedraaid: het projectniveau van deze dienst draagt alleen een OPTIONELE
    bannertekst, dus er valt niets te kiezen dat wij anders voor de gebruiker invullen, en
    hem laten struikelen over een tweede aanroep die hij niet kan raden is bevoogdend.

    Wat blijft is de AFHANKELIJKHEID. Zonder publish-on-web en keycloak kan een auth wall
    niet werken; dat is een feit en geen keuze, en dat hoort de aanroeper meteen te horen.
    """

    def test_zonder_de_vereiste_diensten_gaat_het_niet_door(self) -> None:
        data = _project()
        with pytest.raises(ServiceValidationError) as excinfo:
            ServiceAdapter.ensure_project_selection(data, ServiceType.AUTHORIZATION_WALL.value)

        message = str(excinfo.value)
        assert "services/publish-on-web" in message
        assert "services/keycloak" in message
        assert "services/keycloak/config/restrict-access" in message

    def test_de_melding_gaat_over_de_eisen_en_niet_over_een_beslissing(self) -> None:
        """De twee redenen door elkaar halen stuurt de lezer de verkeerde kant op."""
        data = _project()
        with pytest.raises(ServiceValidationError) as excinfo:
            ServiceAdapter.ensure_project_selection(data, ServiceType.AUTHORIZATION_WALL.value)

        assert "needs a project-level decision" not in str(excinfo.value)

    def test_een_geweigerde_selectie_laat_het_projectbestand_ongemoeid(self) -> None:
        data = _project()
        with pytest.raises(ServiceValidationError):
            ServiceAdapter.ensure_project_selection(data, ServiceType.AUTHORIZATION_WALL.value)
        assert data["services"] == []

    def test_met_de_vereiste_diensten_schrijft_hij_zichzelf_bij(self) -> None:
        """Het punt van de melding: geen tweede aanroep die je moet raden."""
        data = _project(["publish-on-web", {"name": "keycloak", "config": {"restrict-access": "iedereen"}}])

        ServiceAdapter.ensure_project_selection(data, ServiceType.AUTHORIZATION_WALL.value)

        # Een kale selectie, geen leeg configblok: dat zou suggereren dat er iets staat.
        assert data["services"][-1] == ServiceType.AUTHORIZATION_WALL.value

    async def test_de_component_api_meldt_dezelfde_eisen(self) -> None:
        """De weg waarlangs de zad-cli hier terechtkwam: een component met de dienst erop."""
        data = _project()
        data["components"] = []
        project_manager = MagicMock()
        project_manager.get_contents = AsyncMock(return_value=data)
        project_manager.get_name = AsyncMock(return_value="mijn-project")
        project_manager.save_and_commit_project = AsyncMock()

        result = await ProjectManager.add_component(
            project_manager,
            name="app",
            image="",
            deployment_names=[],
            services=[ServiceType.AUTHORIZATION_WALL.value],
        )

        assert result["success"] is False
        assert result["error_type"] == "invalid_services"
        assert "services/keycloak" in result["error"]
        project_manager.save_and_commit_project.assert_not_called()


class TestTheNamedEndpointExists:
    """Een melding die een verzoek noemt dat niet bestaat is erger dan een melding zonder."""

    def test_a_named_endpoint_is_always_a_registered_route(self) -> None:
        """De systematische poort. Vijf van de zes geweigerde diensten hebben een
        configroute op projectniveau; ``attachments`` niet, want die laag draagt daar een
        DEFINITIE (onder ``data``) en geen configblok, dus er is nooit een route voor
        gegenereerd. De melding mag hem dan ook niet noemen."""
        from opi.api.v2.router import v2_router

        registered = {getattr(route, "path", None) for route in v2_router.routes}
        refused = [
            service_type for service_type, service in SERVICES.items() if service.implicit_project_entry() is None
        ]
        assert refused, "premisse: er wordt nog een dienst geweigerd"

        named_at_least_once = False
        for service_type in refused:
            hint = ServiceAdapter._project_selection_hint(service_type, "mijn-project")
            path = config_endpoint_path(ConfigLayer.PROJECT, service_type.value, "mijn-project")
            if path in hint:
                named_at_least_once = True
                template = config_endpoint_path(ConfigLayer.PROJECT, service_type.value, "{project_name}")
                assert template in registered, f"{service_type.value} wijst naar een dode route"
            else:
                assert f"GET /api/v2/services/{service_type.value}" in hint

        assert named_at_least_once, "premisse: minstens een weigering noemt een echte route"

    def test_attachments_is_not_sent_to_a_route_that_was_never_generated(self) -> None:
        """De concrete valkuil achter de poort hierboven."""
        hint = ServiceAdapter._project_selection_hint(ServiceType.ATTACHMENTS, "mijn-project")
        assert "PUT" not in hint
        assert "GET /api/v2/services/attachments" in hint

    def test_the_route_generator_and_the_message_share_one_string(self) -> None:
        """``_config_write_route`` bouwt het pad dat FastAPI registreert; de melding
        bouwt het pad dat de aanroeper leest. Zodra dat twee strings worden, gaat er een
        stilzwijgend verouderen."""
        from opi.api.v2.router import _config_write_route

        for layer in (ConfigLayer.PROJECT, ConfigLayer.COMPONENT, ConfigLayer.DEPLOYMENT):
            suffix, _ = _config_write_route(layer)
            assert config_endpoint_path(layer, "svc").endswith(suffix)


class TestUnmetRequirements:
    def test_a_missing_service_is_unmet(self) -> None:
        assert unmet_service_requirements(_project(), ["services/keycloak"]) == ["services/keycloak"]

    def test_a_bare_selection_meets_the_one_level_form(self) -> None:
        assert unmet_service_requirements(_project(["keycloak"]), ["services/keycloak"]) == []

    def test_a_bare_selection_does_not_meet_a_config_requirement(self) -> None:
        """Een kale selectie draagt geen configuratie, dus de diepere eis staat nog open."""
        assert unmet_service_requirements(_project(["keycloak"]), ["services/keycloak/config/restrict-access"]) == [
            "services/keycloak/config/restrict-access"
        ]

    def test_identity_survives_an_entry_that_carries_config(self) -> None:
        """De valkuil uit instructions/services.md: op de sleutels van het dict lezen
        laat elke dienst mét config vallen."""
        data = _project([{"name": "keycloak", "config": {"template": "standaard"}}])
        assert unmet_service_requirements(data, ["services/keycloak"]) == []

    def test_a_path_outside_the_services_block_is_not_guessed_at(self) -> None:
        assert unmet_service_requirements(_project(), ["components[*]/iets"]) == []


# --------------------------------------------------------------------------------------
# Bevinding 22: een eigen domein zonder certificaat is zichtbaar op het moment van zetten
# --------------------------------------------------------------------------------------


class TestTheClusterAnswersWhetherItCanIssue:
    def test_the_sandbox_cannot(self) -> None:
        """Nagemeten op kind-rig-sandbox: geen certificates-CRD, geen controller, alleen
        een nep-Issuer-CRD uit infrastructure/bootstrap/infrastructure/cert-manager/fake-crd."""
        assert supports_custom_domain_certificates("sandboxed-local") is False

    def test_production_can(self) -> None:
        assert supports_custom_domain_certificates("odcn-production") is True

    def test_a_cluster_that_does_not_declare_it_stays_silent(self) -> None:
        """Afwezig is geen 'nee': een waarschuwing over een cluster waarvan het platform
        het niet weet, is een gok over andermans cluster."""
        assert supports_custom_domain_certificates("local") is True


class TestTheNote:
    def test_a_supported_domain_says_nothing(self) -> None:
        assert custom_domain_certificate_note("sandboxed-local", "sandbox.rijksapp.dev") is None

    def test_no_domain_says_nothing(self) -> None:
        assert custom_domain_certificate_note("sandboxed-local", None) is None

    def test_a_cluster_that_can_issue_says_nothing(self) -> None:
        assert custom_domain_certificate_note("odcn-production", "mijn-app.nl") is None

    def test_an_own_domain_on_the_sandbox_names_the_way_out(self) -> None:
        note = custom_domain_certificate_note("sandboxed-local", "mijn-app.nl")
        assert note is not None
        assert "mijn-app.nl" in note
        assert "tls: provided" in note


def _deployment_project(config: dict[str, Any], allowed_domains: list[dict] | None = None) -> dict[str, Any]:
    """Een project met een deployment die ``config`` onder publish-on-web draagt."""
    service_config: dict[str, Any] = {}
    if allowed_domains is not None:
        service_config["domains"] = {"allowed-domains": allowed_domains}
    return {
        "name": "demo",
        "services": [{"reference": "publish-on-web", "config": service_config}],
        "deployments": [
            {
                "name": "productie",
                "cluster": "sandboxed-local",
                "namespace": "demo",
                "services": [{"reference": "publish-on-web", "config": config}],
                "components": [{"reference": "frontend", "image": "ghcr.io/org/app:v1"}],
            }
        ],
        "components": [{"name": "frontend", "type": "single", "services": ["publish-on-web"]}],
    }


class TestTheFormWarnsWhenTheValueIsSet:
    """De waarschuwing hoort te vallen op het moment dat het domein wordt gezet."""

    async def test_an_approved_own_domain_still_warns(self) -> None:
        """Dit is het scenario uit de bevinding. Zolang het domein op goedkeuring wacht
        publiceert de deployment op het clusteradres en is er niets mis; het certificaat
        gaat pas mis NADAT de goedkeuring binnen is, en juist dan verdwijnt de
        goedkeuringsmelding."""
        project = _deployment_project(
            {"base-domain": "mijn-app.nl", "domain-format": "deployment-project"},
            [{"domain": "mijn-app.nl", "status": "approved"}],
        )
        with (
            patch("opi.core.config.settings.CLUSTER_MANAGER", "sandboxed-local"),
            pytest.raises(FieldWarning) as excinfo,
        ):
            await DomainConfigEnforcer().enforce(project, {"project_name": "demo"})

        assert "tls: provided" in str(excinfo.value)

    async def test_it_warns_without_a_domain_format_too(self) -> None:
        """De configroute kan een base-domain zetten zonder ooit een domain-format te
        zetten; achter de early return was de waarschuwing alleen uit de wizard te halen."""
        project = _deployment_project({"base-domain": "mijn-app.nl"}, [{"domain": "mijn-app.nl", "status": "approved"}])
        with (
            patch("opi.core.config.settings.CLUSTER_MANAGER", "sandboxed-local"),
            pytest.raises(FieldWarning, match="certificaat"),
        ):
            await DomainConfigEnforcer().enforce(project, {"project_name": "demo"})

    async def test_an_unapproved_domain_hears_both_obstacles_at_once(self) -> None:
        """Goedkeuring en certificaat zijn twee verschillende hindernissen op dezelfde
        waarde. Er kan er maar een tegelijk uit de enforcer komen, dus staan ze in een
        melding -- anders hoort de gebruiker de tweede pas als de eerste is opgelost."""
        project = _deployment_project({"base-domain": "mijn-app.nl", "domain-format": "deployment-project"}, [])
        with (
            patch("opi.core.config.settings.CLUSTER_MANAGER", "sandboxed-local"),
            pytest.raises(FieldWarning) as excinfo,
        ):
            await DomainConfigEnforcer().enforce(project, {"project_name": "demo"})

        message = str(excinfo.value)
        assert "op aanvraag" in message
        assert "tls: provided" in message

    async def test_a_supported_domain_is_left_alone(self) -> None:
        """Het platformcertificaat dekt de domeinen die het cluster zelf aanbiedt, dus daar
        valt niets te melden -- ook niet op het cluster dat verder niets kan uitgeven."""
        project = _deployment_project({"base-domain": "sandbox.rijksapp.dev", "domain-format": "deployment-project"})
        with patch("opi.core.config.settings.CLUSTER_MANAGER", "sandboxed-local"):
            await DomainConfigEnforcer().enforce(project, {"project_name": "demo"})


class TestTheApiSaysItOnTheWrite:
    def test_the_warning_rides_on_the_deployment_write(self) -> None:
        """``warnings`` en niet ``approvals``: dit wacht op niemand, en het begint waar de
        goedkeuringsmelding ophoudt."""
        project = _deployment_project({"base-domain": "mijn-app.nl"}, [{"domain": "mijn-app.nl", "status": "approved"}])
        with patch("opi.manager.project_manager.settings.CLUSTER_MANAGER", "sandboxed-local"):
            warnings = ProjectManager._certificate_warnings(project, "productie")

        assert len(warnings) == 1
        assert "tls: provided" in warnings[0]

    def test_a_supported_domain_produces_no_warning(self) -> None:
        project = _deployment_project({"base-domain": "sandbox.rijksapp.dev"})
        with patch("opi.manager.project_manager.settings.CLUSTER_MANAGER", "sandboxed-local"):
            assert ProjectManager._certificate_warnings(project, "productie") == []

    def test_an_unknown_deployment_produces_no_warning(self) -> None:
        project = _deployment_project({"base-domain": "mijn-app.nl"})
        with patch("opi.manager.project_manager.settings.CLUSTER_MANAGER", "sandboxed-local"):
            assert ProjectManager._certificate_warnings(project, "bestaat-niet") == []


class TestTheApiSaysItBeforeTheWrite:
    def test_the_cluster_listing_carries_the_capability(self) -> None:
        """``GET /projects/{p}/clusters`` somt de domeinen op die een cluster aanbiedt.
        Zonder dit veld leest '__custom__' als een gelijkwaardige keuze."""
        from opi.api.v2.models import ClusterInfo

        info = ClusterInfo.model_validate(
            {"name": "sandboxed-local", "manager": True, "base-domains": [], "custom-domain-certificates": False}
        )
        assert info.model_dump(by_alias=True)["custom-domain-certificates"] is False

    def test_the_tls_field_documents_the_way_out(self) -> None:
        """Het OpenAPI-document is waar een client 'provided' moet kunnen vinden."""
        service = get_service(ServiceType.PUBLISH_ON_WEB)
        model = service.config_model_for(ConfigLayer.COMPONENT)
        assert model is not None
        assert "custom-domain-certificates" in model.model_fields["tls"].description

    def test_the_base_domain_field_points_at_the_cluster_endpoint(self) -> None:
        service = get_service(ServiceType.PUBLISH_ON_WEB)
        model = service.config_model_for(ConfigLayer.DEPLOYMENT)
        assert model is not None
        description = model.model_fields["base_domain"].description
        assert description is not None
        assert "/clusters" in description
