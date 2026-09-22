"""Wie het kale domein mag gebruiken, en waar dat wordt afgedwongen.

Een kaal-domein-ingress claimt de APEX van een basisdomein, plus het Let's
Encrypt-certificaat erop, vanuit één tenant-namespace. Op een platformdomein neemt dat de
domeinnaam af van elke andere tenant op het cluster, en op het domein van een andere
tenant neemt het diens domein over: hun DNS wijst al naar dit cluster, want zo serveren
ze hun subdomeinen.

De regel zelf staat dus vast. Wat deze toetsen pinnen is WAAR hij bijt, en dat is sinds
RC-216 niet meer overal hetzelfde:

- **opslaan mag.** Een kaal domein op een eigen domein dat nog op goedkeuring wacht mag
  in het projectbestand staan, en het formulier laat de gebruiker er in één keer langs
  met een waarschuwing bij het vinkje. Daarvoor liep hij vast op een ``ValueError`` op
  het groepspad, die nergens rendert: de knop leek stuk (RIG-Cluster#179).
- **toepassen mag niet.** Tot de goedkeuring er is levert publicatie geen apex op, niet
  van het eigen domein en niet van de clusterzone.
- **het platformdomein weigert hard**, met of zonder aanvraag. Geen goedkeuring maakt de
  apex van een platformdomein van één project.

De shape die dit alles moet raken is die van de config-PUT
(``PUT /api/v2/projects/{p}/services/publish-on-web/deployments/{d}/config``): een
basisdomein en een kaal-domein-component, zonder domain-format. Die kwam ooit langs de
hele regel omdat hij achter ``if not domain_format: return`` stond.
"""

import ast
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from opi.connectors.subdomain import BARE_DOMAIN_PLATFORM_MESSAGE, validate_bare_domain_allowed
from opi.core.project_schema import ProjectIntegrityError
from opi.forms.editables.enforcers import DomainConfigEnforcer, FieldError, FieldWarning
from opi.manager import project_manager
from opi.manager.project_validation import validate_project_structure
from opi.services.catalog.publish_on_web.domain_config import DomainSetting, domain_setting_path
from opi.utils.naming import get_deployment_hostnames

#: Waar de meldingen over het kale domein horen te landen: bij het vinkje zelf.
_BARE_FIELD = domain_setting_path(DomainSetting.BARE_DOMAIN_COMPONENT, 0)


def _project(config: dict, allowed_domains: list[dict] | None = None) -> dict:
    """A project whose single deployment carries ``config`` under publish-on-web."""
    service_config: dict = {}
    if allowed_domains is not None:
        service_config["domains"] = {"allowed-domains": allowed_domains}
    return {
        "name": "demo",
        "services": [{"reference": "publish-on-web", "config": service_config}],
        "deployments": [
            {
                "name": "productie",
                "cluster": "local",
                "namespace": "demo",
                "services": [{"reference": "publish-on-web", "config": config}],
                "components": [{"reference": "frontend", "image": "ghcr.io/org/app:v1"}],
            }
        ],
        "components": [{"name": "frontend", "type": "single", "services": ["publish-on-web"]}],
    }


#: The project owns mijn-app.nl: an approved entry in the allow-list.
_OWNED = [{"domain": "mijn-app.nl", "status": "approved"}]


#: Exactly what the config PUT can store: a platform base domain, a bare-domain component,
#: and no domain-format anywhere.
_PUT_SHAPE = {"base-domain": "rijksapp.dev", "expose-component-on-bare-domain": "frontend"}


class TestEnforcerRefusesBareDomainOnPlatformDomain:
    async def test_without_a_domain_format(self):
        with (
            patch("opi.forms.editables.enforcers.get_supported_base_domains", return_value={"rijksapp.dev"}),
            pytest.raises(FieldError) as exc_info,
        ):
            await DomainConfigEnforcer().enforce(_project(_PUT_SHAPE), {"project_name": "demo"})
        assert str(exc_info.value) == BARE_DOMAIN_PLATFORM_MESSAGE
        assert exc_info.value.field_path == _BARE_FIELD

    async def test_with_a_domain_format(self):
        """The wizard shape stays refused too -- this is not a swap of one hole for another."""
        config = {**_PUT_SHAPE, "domain-format": "component-deployment-project"}
        with (
            patch("opi.forms.editables.enforcers.get_supported_base_domains", return_value={"rijksapp.dev"}),
            pytest.raises(FieldError, match="Kaal domein"),
        ):
            await DomainConfigEnforcer().enforce(_project(config), {"project_name": "demo"})

    async def test_an_aanvraag_does_not_unlock_it(self):
        """De helft die nooit verzacht. Het vinkje opent de weg naar een EIGEN domein; de
        apex van een platformdomein is van iedereen op het cluster en blijft dicht.

        Dit is de toets die betrapt dat de uitgang uit RC-216 te ver is doorgeschoten."""
        config = {**_PUT_SHAPE, "domain-format": "component-deployment-project"}
        project = _project(config)
        project["deployments"][0]["_request-domain"] = True
        with (
            patch("opi.forms.editables.enforcers.get_supported_base_domains", return_value={"rijksapp.dev"}),
            pytest.raises(FieldError, match="Kaal domein"),
        ):
            await DomainConfigEnforcer().enforce(project, {"project_name": "demo"})

    async def test_own_domain_without_a_format_is_still_allowed(self):
        """The rule is about domains that are not the project's own. A domain approved for
        this project reaches the availability check, which is the only thing standing
        between it and approval."""
        config = {"base-domain": "mijn-app.nl", "expose-component-on-bare-domain": "frontend"}
        with (
            patch("opi.forms.editables.enforcers.get_supported_base_domains", return_value={"rijksapp.dev"}),
            patch.object(DomainConfigEnforcer, "_check_bare_domain_availability", new=AsyncMock()) as availability,
        ):
            await DomainConfigEnforcer().enforce(_project(config, _OWNED), {"project_name": "demo"})
        availability.assert_awaited_once()

    async def test_no_bare_domain_component_is_untouched(self):
        """A deployment that does not ask for a bare domain must not be affected."""
        with patch("opi.forms.editables.enforcers.get_supported_base_domains", return_value={"rijksapp.dev"}):
            await DomainConfigEnforcer().enforce(_project({"base-domain": "rijksapp.dev"}), {"project_name": "demo"})


#: Another tenant's domain: not a platform domain, and not approved for this project.
_FOREIGN_SHAPE = {"base-domain": "victim.nl", "expose-component-on-bare-domain": "frontend"}


class TestEnforcerSendsAnUnapprovedDomainToTheAanvraag:
    """Een domein dat dit project (nog) niet heeft is geen fout maar een aanvraag.

    De melding hoort bij het vinkje dat hem veroorzaakt, en hij houdt de stap niet tegen:
    hij is een ``FieldWarning``. Daarvoor was het een ``ValueError`` op het groepspad
    ``deployments[N]``, waar geen veld staat, dus stond de gebruiker voor een knop die
    niets deed."""

    async def test_without_a_domain_format(self):
        with (
            patch("opi.forms.editables.enforcers.get_supported_base_domains", return_value={"rijksapp.dev"}),
            patch.object(DomainConfigEnforcer, "_check_bare_domain_availability", new=AsyncMock()),
            pytest.raises(FieldWarning) as exc_info,
        ):
            await DomainConfigEnforcer().enforce(_project(_FOREIGN_SHAPE), {"project_name": "demo"})
        assert exc_info.value.field_path == _BARE_FIELD
        assert "Domein aanvragen" in str(exc_info.value)

    async def test_with_a_domain_format(self):
        """De wizard-shape komt bij hetzelfde veld uit, niet op het groepspad."""
        config = {**_FOREIGN_SHAPE, "domain-format": "component-deployment-project"}
        with (
            patch("opi.forms.editables.enforcers.get_supported_base_domains", return_value={"rijksapp.dev"}),
            patch.object(DomainConfigEnforcer, "_check_bare_domain_availability", new=AsyncMock()),
            pytest.raises(FieldWarning) as exc_info,
        ):
            await DomainConfigEnforcer().enforce(_project(config), {"project_name": "demo"})
        assert exc_info.value.field_path == _BARE_FIELD

    async def test_the_checkbox_lets_the_step_through(self):
        """De uitgang: met 'Domein aanvragen' aangevinkt komt de stap er in één keer door.

        Zonder deze tak zit de aanvraag achter de weigering: ``DomainRequestHook`` draait
        pas bij PRE_SAVE, dus alleen als de stap doorgaat."""
        config = {**_FOREIGN_SHAPE, "domain-format": "component-deployment-project"}
        project = _project(config)
        project["deployments"][0]["_request-domain"] = True
        with (
            patch("opi.forms.editables.enforcers.get_supported_base_domains", return_value={"rijksapp.dev"}),
            patch.object(DomainConfigEnforcer, "_check_bare_domain_availability", new=AsyncMock()),
        ):
            assert await DomainConfigEnforcer().enforce(project, {"project_name": "demo"}) is project

    async def test_the_checkbox_lets_the_config_put_shape_through_too(self):
        """Zonder domain-format is er geen tweede uitgang verderop: de vastgehouden
        waarschuwing komt dan bij de vroege return eruit, of helemaal niet."""
        project = _project(_FOREIGN_SHAPE)
        project["deployments"][0]["_request-domain"] = True
        with (
            patch("opi.forms.editables.enforcers.get_supported_base_domains", return_value={"rijksapp.dev"}),
            patch.object(DomainConfigEnforcer, "_check_bare_domain_availability", new=AsyncMock()),
        ):
            assert await DomainConfigEnforcer().enforce(project, {"project_name": "demo"}) is project

    async def test_an_existing_request_lets_the_step_through(self):
        """Tweede gang door hetzelfde scherm: het vinkje is weg, de aanvraag staat er."""
        requested = [{"domain": "victim.nl", "status": "requested"}]
        with (
            patch("opi.forms.editables.enforcers.get_supported_base_domains", return_value={"rijksapp.dev"}),
            patch.object(DomainConfigEnforcer, "_check_bare_domain_availability", new=AsyncMock()),
        ):
            project = _project(_FOREIGN_SHAPE, requested)
            assert await DomainConfigEnforcer().enforce(project, {"project_name": "demo"}) is project

    async def test_a_denied_domain_is_refused_in_the_form(self):
        """Een afgewezen domein is een oordeel, geen openstaande aanvraag."""
        denied = [{"domain": "victim.nl", "status": "denied"}]
        with (
            patch("opi.forms.editables.enforcers.get_supported_base_domains", return_value={"rijksapp.dev"}),
            patch.object(DomainConfigEnforcer, "_check_bare_domain_availability", new=AsyncMock()),
            pytest.raises(FieldError) as exc_info,
        ):
            await DomainConfigEnforcer().enforce(_project(_FOREIGN_SHAPE, denied), {"project_name": "demo"})
        assert exc_info.value.field_path == _BARE_FIELD

    async def test_a_revoked_approval_stays_saveable(self):
        """The one exemption, and only in the save gate (``denied_blocks=False``): an
        approver revoking a domain a deployment already exposes on the apex must be able to
        record that verdict. Publication refuses the domain outright, so nothing is
        claimed on it."""
        denied = [{"domain": "mijn-app.nl", "status": "denied"}]
        config = {"base-domain": "mijn-app.nl", "expose-component-on-bare-domain": "frontend"}
        with (
            patch("opi.forms.editables.enforcers.get_supported_base_domains", return_value={"rijksapp.dev"}),
            patch.object(DomainConfigEnforcer, "_check_bare_domain_availability", new=AsyncMock()),
        ):
            await DomainConfigEnforcer(denied_blocks=False).enforce(_project(config, denied), {"project_name": "demo"})


class TestSaveGate:
    async def test_the_platform_domain_shape_cannot_be_stored(self):
        """``validate_project_structure`` is what a config PUT passes through on its way to
        disk, and it runs the same enforcer. Het platformdomein moet er ook daar op
        stuklopen."""
        with (
            patch("opi.forms.editables.enforcers.get_supported_base_domains", return_value={"rijksapp.dev"}),
            pytest.raises(ProjectIntegrityError, match="Kaal domein"),
        ):
            await validate_project_structure(_project(_PUT_SHAPE))

    async def test_a_domain_on_aanvraag_can_be_stored(self):
        """Het projectbestand legt vast wat het project WIL, en een openstaande aanvraag
        hoort daarbij. De poort laat een ``FieldWarning`` door; de apex wordt pas na de
        goedkeuring geclaimd (zie ``TestPublicationWaitsForTheApproval``)."""
        with (
            patch("opi.forms.editables.enforcers.get_supported_base_domains", return_value={"rijksapp.dev"}),
            patch.object(DomainConfigEnforcer, "_check_bare_domain_availability", new=AsyncMock()),
        ):
            await validate_project_structure(_project(_FOREIGN_SHAPE))


class TestPublicationWaitsForTheApproval:
    """Wat de opslagpoort doorlaat mag publicatie nog niet toepassen.

    ``get_deployment_hostnames`` is de lijst die bij Keycloak als redirect-URI terechtkomt
    en die de ingressen in project_manager op dezelfde voorwaarde volgen."""

    @staticmethod
    def _hostnames(status: str | None) -> list[str]:
        domains = [{"domain": "victim.nl", "status": status}] if status else []
        return get_deployment_hostnames(
            component_names=["frontend"],
            deployment_name="productie",
            project_name="demo",
            ingress_postfix=".kind",
            base_domain="victim.nl",
            domain_format="component-deployment-project",
            expose_on_bare_domain="frontend",
            project_data={"domains": {"allowed-domains": domains}},
            cluster="local",
        )

    def test_a_requested_domain_yields_no_apex(self):
        hostnames = self._hostnames("requested")
        assert "victim.nl" not in hostnames
        assert "kind" not in hostnames

    def test_a_domain_without_an_entry_yields_no_apex(self):
        assert "victim.nl" not in self._hostnames(None)

    def test_the_approved_domain_does_yield_the_apex(self):
        assert "victim.nl" in self._hostnames("approved")


class TestValidateBareDomainAllowed:
    """The helper the publication path calls just before it registers the bare domain and
    renders the apex ingress -- the gate that a write path the form layer never sees
    cannot get around. Both call sites (``register_bare_domain`` and the apex ingress) sit
    outside the format branch that runs ``apply_domain_approval_fallback``, so this helper
    carries the ownership half of the rule as well."""

    def test_platform_domain_is_refused(self):
        with pytest.raises(ValueError, match=BARE_DOMAIN_PLATFORM_MESSAGE):
            validate_bare_domain_allowed("rijksapp.dev", {"rijksapp.dev", "rijksapps.nl"}, _project({}, _OWNED))

    def test_case_is_ignored(self):
        with pytest.raises(ValueError, match=BARE_DOMAIN_PLATFORM_MESSAGE):
            validate_bare_domain_allowed("RijksApp.DEV", {"rijksapp.dev"}, _project({}, _OWNED))

    def test_foreign_domain_is_refused(self):
        with pytest.raises(ValueError, match="niet goedgekeurd voor dit project"):
            validate_bare_domain_allowed("victim.nl", {"rijksapp.dev"}, _project({}, _OWNED))

    def test_unapproved_status_is_refused(self):
        project = _project({}, [{"domain": "victim.nl", "status": "requested"}])
        with pytest.raises(ValueError, match="niet goedgekeurd voor dit project"):
            validate_bare_domain_allowed("victim.nl", {"rijksapp.dev"}, project)

    def test_own_domain_passes(self):
        validate_bare_domain_allowed("mijn-app.nl", {"rijksapp.dev"}, _project({}, _OWNED))


class TestBeidePublicatiepuntenHangenAanDeGoedkeuring:
    """De twee plekken die de apex echt claimen, gelezen uit de bron.

    ``register_bare_domain`` zet de claim in het register en de kaal-domein-ingress vraagt
    er een certificaat op aan. Ze staan midden in ``process_project``, dat git, SOPS,
    Keycloak en een database nodig heeft, dus ze zijn hier niet te draaien. Wat wel te
    meten is, is dat geen van beide bereikbaar is zonder ``is_deployment_domain_approved``
    in de voorwaarde: haal de grendel weg en deze toets wordt rood.
    """

    @staticmethod
    def _guards(marker: str) -> list[str]:
        """De ``if``-voorwaarden die een blok met ``marker`` erin bewaken."""
        bron = Path(project_manager.__file__).read_text()
        boom = ast.parse(bron)
        return [
            ast.unparse(knoop.test)
            for knoop in ast.walk(boom)
            if isinstance(knoop, ast.If) and marker in ast.unparse(knoop.body)
        ]

    def test_de_registratie_van_de_apex(self):
        guards = self._guards("register_bare_domain")
        assert guards, "de registratie van het kale domein is niet gevonden"
        assert any("is_deployment_domain_approved" in guard for guard in guards)

    def test_de_ingress_op_de_apex(self):
        guards = self._guards("ingress-bare-domain")
        assert guards, "de kaal-domein-ingress is niet gevonden"
        assert any("is_deployment_domain_approved" in guard for guard in guards)
