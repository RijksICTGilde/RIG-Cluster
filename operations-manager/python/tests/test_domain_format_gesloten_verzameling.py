"""``domain-format`` accepteert alleen bestaande formaat-id's, op elke weg.

Uit een echt projectbestand:

.. code-block:: yaml

    services:
      - reference: publish-on-web
        config:
          base-domain: sandbox.rijksapp.dev
          domain-format: onzin

Dat werd opgeslagen. Twee gaten tegelijk, en ze zaten aan weerskanten:

* de enforcer las het formaat als ``DOMAIN_FORMAT_TEMPLATES.get(fmt, "")``, dus een id dat
  geen template is werd een lege template en maakte elke controle eronder inhoudsloos. Het
  omgekeerde van wat je wilt: het GELDIGE ``subdomain`` werd geweigerd (geen subdomein
  ingevuld) terwijl de typefout er ongehinderd doorheen liep;
* ``PublishOnWebDeploymentConfig.domain_format`` was ``str | None``, terwijl ``DomainFormatId``
  al bestond en door de v1-router al gebruikt werd. Daardoor accepteerde de PUT-body elke
  string en stond er in ``/openapi.json`` geen enum, dus kon een client niet lezen dat
  ``onzin`` ongeldig is zonder het te proberen.

Wat zo'n waarde oplevert, gemeten: ``get_deployment_hostnames`` valt er niet over, maar zet
het domein zonder scheidingsteken achter de naam en levert ``app-prod-demokind`` op. Dus geen
crash die opvalt, maar een adres dat nergens heen gaat -- precies het soort fout dat pas in
productie zichtbaar wordt.

Een bestaand bestand met zo'n waarde raakt hierdoor niet op slot: lezen verandert niet, de
melding valt in het formulier op het veld zelf en noemt de geldige waarden, en na één keuze
uit de select gaat de opslag gewoon door. Op de 48 productieprojecten staat trouwens geen
enkele ongeldige waarde.

Waarom niet ``values_must_exist``: zie ``TestWaaromGeenValuesMustExist``. Gemeten, niet
aangenomen, en het antwoord was het tegenovergestelde van het vermoeden.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.api.openapi_choices import CHOICES_SOURCE_KEY, annotate_config_choices
from opi.forms.editables.enforcers import DomainConfigEnforcer, FieldError
from opi.forms.visualizers.providers import DomainFormatOptionsProvider
from opi.manager.project_validation import ProjectIntegrityError, validate_project_structure
from opi.services.catalog.publish_on_web.config_model import PublishOnWebDeploymentConfig
from opi.services.catalog.publish_on_web.domain_config import DomainSetting, domain_setting_path
from opi.services.catalog.publish_on_web.editables import DOMAIN_FORMAT_EDITABLE
from opi.utils.naming import DOMAIN_FORMAT_TEMPLATES
from pydantic import ValidationError

#: Het formaat uit de melding.
ONZIN = "onzin"


def _project(domain_format: str, *, subdomain: str | None = None, base_domain: str | None = None) -> dict:
    """Een minimaal project waarvan één deployment op het web publiceert."""
    config: dict[str, object] = {"domain-format": domain_format}
    if base_domain is not None:
        config["base-domain"] = base_domain
    if subdomain is not None:
        config["subdomain"] = subdomain
    return {
        "name": "demo",
        "clusters": ["local"],
        "services": ["publish-on-web"],
        "components": [{"name": "app", "image": "nginx", "ports": [8080]}],
        "deployments": [
            {
                "name": "productie",
                "components": [{"reference": "app"}],
                "services": [{"reference": "publish-on-web", "config": config}],
            }
        ],
    }


# ---------------------------------------------------------------------------
# De weg van het formulier: de wizard en de bewerkdialoog
# ---------------------------------------------------------------------------


class TestFormulierWeg:
    """De enforcer van de groep, die zowel de wizard als het opslagknooppunt draait."""

    async def test_onbestaand_formaat_wordt_geweigerd_op_het_veld(self):
        with pytest.raises(FieldError) as excinfo:
            await DomainConfigEnforcer().enforce(_project(ONZIN), {"project_name": "demo"})

        # Op het veld zelf, niet op het onzichtbare groepspad: anders ziet de gebruiker
        # in de wizard geen enkele melding bij het formaat dat hij net koos.
        assert excinfo.value.field_path == domain_setting_path(DomainSetting.DOMAIN_FORMAT, 0)

    async def test_de_melding_noemt_de_geldige_waarden(self):
        """Weigeren zonder te zeggen wat dan wel is een raadspelletje."""
        with pytest.raises(FieldError) as excinfo:
            await DomainConfigEnforcer().enforce(_project(ONZIN), {"project_name": "demo"})

        message = str(excinfo.value)
        assert ONZIN in message
        # De lijst komt uit de values provider van het veld zelf, dus precies dezelfde
        # lijst die de select vult en die de API als x-choices-source publiceert.
        for value in (option["value"] for option in DomainFormatOptionsProvider().get_options()):
            assert value in message

    async def test_een_geldig_formaat_blijft_geldig(self):
        """De negatieve kant: de strengheid mag niets wegnemen dat werkte."""
        await DomainConfigEnforcer().enforce(_project("component-deployment-project"), {"project_name": "demo"})

    async def test_het_gat_andersom_bestond_ook(self):
        """Vóór de fix kwam de typefout er wél door en het geldige formaat niet.

        Deze test legt de asymmetrie vast die de fout zo goed verborg: ``subdomain`` is
        een bestaand formaat en wordt (terecht) tegengehouden zolang er geen subdomein
        staat, terwijl ``onzin`` de hele enforcer passeerde.
        """
        with pytest.raises(FieldError, match="subdomein is vereist"):
            await DomainConfigEnforcer().enforce(_project("subdomain"), {"project_name": "demo"})


class TestOpslagknooppunt:
    """``validate_project_structure``: het knooppunt dat ELKE schrijfweg passeert."""

    async def test_onbestaand_formaat_wordt_geweigerd(self):
        with pytest.raises(ProjectIntegrityError) as excinfo:
            await validate_project_structure(_project(ONZIN))

        assert ONZIN in str(excinfo.value)

    async def test_geldig_formaat_passeert(self):
        await validate_project_structure(_project("component-deployment-project"))


# ---------------------------------------------------------------------------
# De weg van de API
# ---------------------------------------------------------------------------


class TestApiWeg:
    """De PUT-body van de configroute van publish-on-web op deploymentniveau."""

    def test_body_model_weigert_een_onbestaand_formaat(self):
        with pytest.raises(ValidationError) as excinfo:
            PublishOnWebDeploymentConfig.model_validate({"domain-format": ONZIN})

        # Pydantic zet de toegestane waarden in de 422, dus de client hoort meteen
        # waar hij uit had kunnen kiezen.
        assert "component-deployment-project" in str(excinfo.value)

    @pytest.mark.parametrize("format_id", sorted(DOMAIN_FORMAT_TEMPLATES))
    def test_elk_bestaand_formaat_wordt_geaccepteerd(self, format_id: str):
        """De negatieve kant, voor alle elf: het model mag niets afkeuren dat bestaat."""
        model = PublishOnWebDeploymentConfig.model_validate({"domain-format": format_id})
        assert model.domain_format == format_id

    async def test_upsert_deployment_weigert_een_onbestaand_formaat(self):
        """``:upsert-deployment`` draait dezelfde enforcer als de wizard."""
        with (
            patch("opi.manager.project_manager.KubectlConnector"),
            patch("opi.handlers.sops.SopsHandler"),
            patch("opi.generation.manifests.ManifestGenerator"),
            patch("opi.manager.argo_manager.ArgoManager", return_value=MagicMock()),
            patch("opi.manager.bootstrap_manager.BootstrapManager", return_value=MagicMock()),
            patch("opi.manager.delete_project_manager.DeleteProjectManager", return_value=MagicMock()),
            patch("opi.manager.keycloak_manager.KeycloakManager", return_value=MagicMock()),
            patch("opi.manager.minio_manager.MinioManager", return_value=MagicMock()),
            patch("opi.manager.redis_manager.RedisManager", return_value=MagicMock()),
            patch("opi.manager.pvc_manager.PVCManager", return_value=MagicMock()),
        ):
            from opi.manager.project_manager import ProjectManager

            manager = ProjectManager()
            manager.get_name = AsyncMock(return_value="demo")
            error = await manager._enforce_domain_config(_project(ONZIN), "productie")

        assert error is not None
        assert ONZIN in error

    async def test_upsert_deployment_laat_een_geldig_formaat_door(self):
        with (
            patch("opi.manager.project_manager.KubectlConnector"),
            patch("opi.handlers.sops.SopsHandler"),
            patch("opi.generation.manifests.ManifestGenerator"),
            patch("opi.manager.argo_manager.ArgoManager", return_value=MagicMock()),
            patch("opi.manager.bootstrap_manager.BootstrapManager", return_value=MagicMock()),
            patch("opi.manager.delete_project_manager.DeleteProjectManager", return_value=MagicMock()),
            patch("opi.manager.keycloak_manager.KeycloakManager", return_value=MagicMock()),
            patch("opi.manager.minio_manager.MinioManager", return_value=MagicMock()),
            patch("opi.manager.redis_manager.RedisManager", return_value=MagicMock()),
            patch("opi.manager.pvc_manager.PVCManager", return_value=MagicMock()),
        ):
            from opi.manager.project_manager import ProjectManager

            manager = ProjectManager()
            manager.get_name = AsyncMock(return_value="demo")
            error = await manager._enforce_domain_config(_project("component-deployment-project"), "productie")

        assert error is None


class TestOpenApiVertelDeOpties:
    """Een client moet uit ``/openapi.json`` kunnen lezen dat ``onzin`` ongeldig is."""

    @staticmethod
    def _domain_format_node() -> dict:
        schema = PublishOnWebDeploymentConfig.model_json_schema(by_alias=True)
        document = {
            "components": {"schemas": {"PublishOnWebDeploymentConfig": schema}},
            "paths": {
                "/api/v2/projects/{project_name}/services/publish-on-web/config/deployment/{name}": {
                    "put": {
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/PublishOnWebDeploymentConfig"}
                                }
                            }
                        }
                    }
                }
            },
        }
        annotate_config_choices(document)
        return document["components"]["schemas"]["PublishOnWebDeploymentConfig"]["properties"]["domain-format"]

    def test_het_veld_draagt_een_gesloten_enum(self):
        """De id-verzameling ligt vast, dus een ``enum`` is eerlijk: die elf en niets anders."""
        node = self._domain_format_node()
        enums = [branch["enum"] for branch in node["anyOf"] if "enum" in branch]

        assert len(enums) == 1
        assert set(enums[0]) == set(DOMAIN_FORMAT_TEMPLATES)
        assert ONZIN not in enums[0]

    def test_de_bronverwijzing_blijft_ernaast_staan(self):
        """De enum zegt wat BESTAAT, de bron zegt wat hier PAST -- twee vragen, twee antwoorden.

        Welke van de elf bruikbaar zijn hangt af van het gekozen base-domain (de
        punt-varianten vragen een domein dat punten ondersteunt), en dat is precies wat een
        enum niet kan uitdrukken. Vandaar allebei.
        """
        node = self._domain_format_node()

        assert CHOICES_SOURCE_KEY in node
        assert "base-domain" in node[CHOICES_SOURCE_KEY]["description"]


# ---------------------------------------------------------------------------
# De valkuil, vastgelegd
# ---------------------------------------------------------------------------


class TestWaaromGeenValuesMustExist:
    """Waarom de generieke keuzecontrole hier NIET aan staat.

    ``values_must_exist`` toetst een opgeslagen waarde aan de provider van het veld,
    opgebouwd zonder projectcontext. De bekende valkuil is dat een LEGE bron de controle
    inhoudsloos maakt. Hier is het omgekeerd en erger: de bron is nooit leeg, maar hij is
    onvolledig, want ``DomainFormatOptionsProvider`` filtert op base-domain.
    """

    def test_de_contextloze_provider_mist_de_punt_varianten(self):
        """Wat de provider oplevert zonder base-domain: zes van de elf."""
        offered = {option["value"] for option in DomainFormatOptionsProvider().get_options()}

        assert offered < set(DOMAIN_FORMAT_TEMPLATES)
        # Precies de punt-varianten ontbreken, en dat zijn geldige, opgeslagen waarden.
        assert {format_id for format_id in DOMAIN_FORMAT_TEMPLATES if "." in format_id}.isdisjoint(offered)

    def test_een_onbekend_base_domain_levert_hetzelfde_op(self):
        """Ook mét een base-domain dat we niet kennen blijft de lijst de zes streepjes."""
        offered = {
            option["value"] for option in DomainFormatOptionsProvider(base_domain="onbekend.example").get_options()
        }

        assert offered == {option["value"] for option in DomainFormatOptionsProvider().get_options()}

    def test_de_vlag_staat_daarom_uit(self):
        """Aanzetten zou vier deployments in twee productieprojecten onbewerkbaar maken.

        Gemeten op de productieprojecten: ``pm-5sj`` en ``regel-k4c`` dragen een
        punt-variant die voor hun EIGEN base-domain gewoon wordt aangeboden. De
        contextloze provider ziet dat base-domain niet en zou ze alle vier weigeren --
        waarmee een nieuwe strengheid bestaande projecten onbewerkbaar maakt.
        """
        assert not DOMAIN_FORMAT_EDITABLE.values_must_exist

    async def test_een_punt_variant_op_een_punt_domein_blijft_opslaanbaar(self):
        """De productievorm die de vlag zou hebben gesloopt, blijft werken."""
        project = _project("component.subdomain", subdomain="mijnapp", base_domain="rijks.app")
        with (
            patch("opi.forms.editables.enforcers.get_domain_supports_dots", return_value=True),
            patch("opi.forms.editables.enforcers.get_supported_base_domains", return_value={"rijks.app"}),
            # De beschikbaarheid van het subdomein is een vraag aan de database en niet
            # wat hier op het spel staat.
            patch.object(DomainConfigEnforcer, "_check_subdomain_availability", AsyncMock()),
        ):
            await DomainConfigEnforcer().enforce(project, {"project_name": "demo"})
