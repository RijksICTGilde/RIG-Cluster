"""De bestemming van een uitnodiging moet meebewegen met het adres (RC-136).

De succesknop van een uitnodiging bewaarde een kale URL. Een hostname is afgeleid uit het
domeinformaat, het subdomein en het cluster, en alle drie kunnen wijzigen -- daarna wees de
opgeslagen URL naar een adres dat niet meer bestaat, terwijl alles om hem opnieuw uit te
rekenen gewoon in het projectbestand stond.

Vandaar ``application-target``: de KEUZE (welke deployment, welk component, eventueel welk
pad) in plaats van het antwoord. Deze suite houdt de vier plekken vast waar dat waar moet
zijn: het model, de migratie, wat het formulier wegschrijft, en wat de succespagina toont.

``application-url`` blijft geldig en blijft geaccepteerd: niet elke bestemming ligt binnen
dit project.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from opi.services.catalog.invite import InviteService
from opi.services.catalog.invite.config_model import InviteConfig
from opi.services.catalog.invite.converters import ApplicationTargetConverter
from opi.services.catalog.invite.migrations import migrate_invite_config_1_0_to_1_1
from opi.services.project_store import ProjectSummary, ProjectUser
from pydantic import ValidationError

SLEUTEL = "welkom-bij-hn7"
PROJECT = "toets-hn7"


def _project(subdomein: str = "production", paden: list[dict[str, str]] | None = None) -> dict[str, Any]:
    """Een project met een gepubliceerd component, zodat er een adres AF TE LEIDEN is.

    Het subdomein is een parameter omdat dat de hele aanleiding is: wijzig hem en het adres
    wijzigt mee, terwijl de keuze erachter hetzelfde blijft.
    """
    return {
        "schema-version": 2,
        "name": PROJECT,
        "clusters": ["odcn-production"],
        "services": ["publish-on-web", "keycloak"],
        "components": [
            {
                "name": "frontend",
                "type": "frontend",
                "path": paden or [{"match": "/"}],
                "services": ["publish-on-web"],
            }
        ],
        "deployments": [
            {
                "name": "production",
                "cluster": "odcn-production",
                "namespace": PROJECT,
                "subdomain": subdomein,
                "components": [{"reference": "frontend"}],
            }
        ],
    }


def _met_invite(project: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    project = json.loads(json.dumps(project))
    project["services"] = [*project["services"], {"name": "invite", "config": {"active": [entry]}}]
    return project


def _basis_entry(**extra: Any) -> dict[str, Any]:
    return {
        "key": SLEUTEL,
        "contact-email": "help@example.nl",
        "message": {"nl": "Welkom", "en": "Welcome"},
        "success-title": {"nl": "Klaar", "en": "Done"},
        "success-button": {"nl": "Ga naar applicatie", "en": "Go to application"},
        **extra,
    }


def _adres(project: dict[str, Any]) -> str:
    """Het adres dat dit project vandaag oplevert, gemeten in plaats van uitgeschreven."""
    from opi.services.catalog.invite.destination import derived_destinations

    rijen = derived_destinations(project)
    assert rijen, "de opzet van de toets levert geen enkel afleidbaar adres op"
    return rijen[0]["url"]


# ---------------------------------------------------------------------------
# 1. Het model: hooguit een bestemming
# ---------------------------------------------------------------------------


class TestHetModelKentTweeVormen:
    def test_een_keuze_wordt_geaccepteerd(self) -> None:
        config = InviteConfig.model_validate(
            {"active": [_basis_entry(**{"application-target": {"deployment": "production", "component": "frontend"}})]}
        )
        doel = config.active[0].application_target
        assert doel is not None
        assert (doel.deployment, doel.component, doel.path) == ("production", "frontend", None)

    def test_een_vast_adres_blijft_geldig(self) -> None:
        """Niet elke bestemming ligt binnen dit project; die vorm verdwijnt dus niet."""
        config = InviteConfig.model_validate(
            {"active": [_basis_entry(**{"application-url": "https://ergens.anders.nl/"})]}
        )
        assert config.active[0].application_url == "https://ergens.anders.nl/"

    def test_allebei_tegelijk_wordt_geweigerd(self) -> None:
        """Twee bestemmingen kunnen naar twee plekken wijzen en niets kiest ertussen."""
        with pytest.raises(ValidationError) as fout:
            InviteConfig.model_validate(
                {
                    "active": [
                        _basis_entry(
                            **{
                                "application-url": "https://ergens.anders.nl/",
                                "application-target": {"deployment": "production", "component": "frontend"},
                            }
                        )
                    ]
                }
            )
        assert "één bestemming" in str(fout.value)

    def test_geen_van_beide_is_geldig(self) -> None:
        """Een uitnodiging zonder knop is een geldige uitnodiging."""
        config = InviteConfig.model_validate({"active": [_basis_entry()]})
        assert config.active[0].application_url is None
        assert config.active[0].application_target is None


# ---------------------------------------------------------------------------
# 2. De migratie v1.0 -> v1.1
# ---------------------------------------------------------------------------


class TestDeMigratie:
    def test_een_afleidbaar_adres_wordt_de_keuze_erachter(self) -> None:
        project = _project()
        config = {"active": [_basis_entry(**{"application-url": _adres(project)})]}

        gemigreerd = migrate_invite_config_1_0_to_1_1(config, project)

        entry = gemigreerd["active"][0]
        assert entry["application-target"] == {"deployment": "production", "component": "frontend"}
        assert "application-url" not in entry

    def test_een_adres_dat_niet_te_matchen_is_blijft_staan(self) -> None:
        """Dat is een extern of verouderd adres; stilletjes weggooien is erger dan bewaren."""
        config = {"active": [_basis_entry(**{"application-url": "https://ergens.anders.nl/"})]}

        gemigreerd = migrate_invite_config_1_0_to_1_1(config, _project())

        entry = gemigreerd["active"][0]
        assert entry["application-url"] == "https://ergens.anders.nl/"
        assert "application-target" not in entry

    def test_de_onderstreepte_schrijfwijze_migreert_ook(self) -> None:
        """De bestanden die aan de dienst voorafgaan dragen de veldnamen letterlijk."""
        project = _project()
        config = {"active": [{"key": SLEUTEL, "application_url": _adres(project)}]}

        entry = migrate_invite_config_1_0_to_1_1(config, project)["active"][0]

        assert entry["application-target"] == {"deployment": "production", "component": "frontend"}
        assert "application_url" not in entry

    def test_tweemaal_draaien_verandert_niets(self) -> None:
        """Vooruit-only en idempotent: de tweede gang heeft niets meer te consumeren."""
        project = _project()
        config = {"active": [_basis_entry(**{"application-url": _adres(project)})]}

        een = migrate_invite_config_1_0_to_1_1(config, project)
        twee = migrate_invite_config_1_0_to_1_1(een, project)

        assert een == twee

    def test_zonder_project_blijft_de_config_ongemoeid(self) -> None:
        """Er valt niets tegen te matchen, en dan is gokken erger dan niets doen."""
        config = {"active": [_basis_entry(**{"application-url": "https://a/"})]}

        assert migrate_invite_config_1_0_to_1_1(config, None) == config

    def test_de_dienst_draait_de_stap_alleen_vanaf_1_0(self) -> None:
        """De huidige versie migreren zou de stap op zijn eigen uitvoer loslaten."""
        from opi.services.catalog.base import PROJECT_DATA_CONTEXT_KEY

        project = _project()
        config = {"active": [_basis_entry(**{"application-url": _adres(project)})]}
        context = {PROJECT_DATA_CONTEXT_KEY: project}

        assert InviteService().migrate_config(config, "1.1", context) == config
        assert "application-target" in InviteService().migrate_config(config, "1.0", context)["active"][0]

    def test_de_dienst_staat_op_1_1(self) -> None:
        assert InviteService.config_schema_version == "1.1"


# ---------------------------------------------------------------------------
# 3. Wat het formulier wegschrijft
# ---------------------------------------------------------------------------


class TestDeKeuzelijstSchrijftDeKeuze:
    def test_de_opgeslagen_waarde_is_de_bestemming_en_niet_de_url(self) -> None:
        """De lijst toont adressen -- die herkent een mens -- maar bewaart de keuze."""
        project = _project()

        opgeslagen = ApplicationTargetConverter().write(_adres(project), context_data=project)

        assert opgeslagen == {"deployment": "production", "component": "frontend"}

    def test_een_component_met_twee_paden_bewaart_het_pad(self) -> None:
        """Twee paden zijn twee adressen; zonder het pad is de keuze niet eenduidig."""
        project = _project(paden=[{"match": "/"}, {"match": "/api"}])
        from opi.services.catalog.invite.destination import derived_destinations

        api = next(rij for rij in derived_destinations(project) if rij["path"] == "/api")

        opgeslagen = ApplicationTargetConverter().write(api["url"], context_data=project)

        assert opgeslagen == {"deployment": "production", "component": "frontend", "path": "/api"}

    def test_de_lege_keuze_schrijft_geen_bestemming(self) -> None:
        assert ApplicationTargetConverter().write("", context_data=_project()) is None

    def test_een_adres_buiten_dit_project_levert_geen_keuze_op(self) -> None:
        assert ApplicationTargetConverter().write("https://ergens.anders.nl/", context_data=_project()) is None

    def test_de_opgeslagen_keuze_selecteert_zijn_eigen_regel_weer(self) -> None:
        """Anders staat de lijst bij het openen op "geen knop" terwijl er een bestemming is."""
        project = _project()
        doel = {"deployment": "production", "component": "frontend"}

        assert ApplicationTargetConverter().read(doel, context_data=project) == _adres(project)

    def test_een_keuze_die_niet_meer_oplost_selecteert_niets(self) -> None:
        doel = {"deployment": "weg", "component": "frontend"}

        assert ApplicationTargetConverter().read(doel, context_data=_project()) == ""

    def test_een_gekozen_bestemming_verdringt_een_bestaand_vast_adres(self) -> None:
        """Het model weigert allebei, dus de opslag moet er een overhouden: de nieuwe keuze."""
        project = _met_invite(
            _project(),
            _basis_entry(
                **{
                    "application-url": "https://ergens.anders.nl/",
                    "application-target": {"deployment": "production", "component": "frontend"},
                }
            ),
        )

        InviteService().settle_destination(project)

        entry = project["services"][-1]["config"]["active"][0]
        assert "application-url" not in entry
        assert entry["application-target"] == {"deployment": "production", "component": "frontend"}

    def test_een_vast_adres_zonder_keuze_blijft_staan(self) -> None:
        """Geen keuze gemaakt betekent "hier valt niets te kiezen", niet "gooi maar weg"."""
        project = _met_invite(_project(), _basis_entry(**{"application-url": "https://ergens.anders.nl/"}))

        InviteService().settle_destination(project)

        assert project["services"][-1]["config"]["active"][0]["application-url"] == "https://ergens.anders.nl/"


# ---------------------------------------------------------------------------
# 4. De succespagina: uitrekenen bij het renderen
# ---------------------------------------------------------------------------


def _sessiekoekje(secret: str) -> str:
    """Een getekend Starlette-sessiekoekje, zoals de e2e-suite dat ook doet.

    De succespagina stuurt door naar de landingspagina als de sessie leeg is, dus zonder
    dit koekje meet de toets de omleiding en niet de knop.
    """
    payload = base64.b64encode(json.dumps({"invite_success": {"email": "iemand@example.nl"}}).encode()).decode()
    return TimestampSigner(secret).sign(payload).decode()


@pytest.fixture
def pagina(mock_settings: Any) -> Any:
    """Rendert ``/invite/<sleutel>/success`` voor een gegeven projectbestand.

    Over HTTP en dus door de echte template heen: de knop staat achter een
    ``{% if application_url %}``, en die voorwaarde is precies wat hier getoetst wordt.
    """
    from opi.server import create_app

    def render(project: dict[str, Any]) -> str:
        store = MagicMock()
        store.get_all.return_value = [
            ProjectSummary(
                name=PROJECT,
                api_key="test-api-key-12345",
                filename=f"{PROJECT}.yaml",
                users=[ProjectUser(email="user@example.com", role="admin")],
                data=project,
            )
        ]
        with patch("opi.api.invite_routes.get_project_store", return_value=store):
            app = create_app()
            client = TestClient(app)
            client.cookies.set("session", _sessiekoekje(mock_settings.SECRET_KEY))
            antwoord = client.get(f"/invite/{SLEUTEL}/success")
            assert antwoord.status_code == 200, antwoord.status_code
            return antwoord.text

    return render


class TestDeSuccespagina:
    def test_een_bestemming_wordt_uitgerekend_bij_het_renderen(self, pagina: Any) -> None:
        project = _project()
        html = pagina(
            _met_invite(project, _basis_entry(**{"application-target": {"deployment": "production", "component": "frontend"}}))
        )

        assert _adres(project) in html

    def test_de_knop_volgt_een_gewijzigd_subdomein(self, pagina: Any) -> None:
        """Dit is de hele aanleiding: hetzelfde bestand, een ander subdomein, en de knop
        wijst mee in plaats van naar een adres dat niet meer bestaat."""
        entry = _basis_entry(**{"application-target": {"deployment": "production", "component": "frontend"}})

        oud = _project(subdomein="production")
        nieuw = _project(subdomein="acceptatie")
        assert _adres(oud) != _adres(nieuw), "de toets meet niets als het adres niet verandert"

        assert _adres(oud) in pagina(_met_invite(oud, entry))
        assert _adres(nieuw) in pagina(_met_invite(nieuw, entry))

    def test_een_bestemming_die_niet_meer_oplost_toont_geen_knop(self, pagina: Any) -> None:
        """Geen knop is beter dan een knop die ergens verkeerd heen wijst: dat verschil
        ziet de gebruiker pas nadat hij geklikt heeft."""
        html = pagina(
            _met_invite(
                _project(), _basis_entry(**{"application-target": {"deployment": "production", "component": "weg"}})
            )
        )

        assert _knop_bestemming(html) is None

    def test_een_kaal_adres_wordt_gewoon_getoond(self, pagina: Any) -> None:
        html = pagina(_met_invite(_project(), _basis_entry(**{"application-url": "https://ergens.anders.nl/"})))

        assert "https://ergens.anders.nl/" in html

    def test_zonder_bestemming_staat_er_geen_knop(self, pagina: Any) -> None:
        html = pagina(_met_invite(_project(), _basis_entry()))

        assert _knop_bestemming(html) is None

    def test_de_knopmeting_vindt_een_knop_die_er_wel_is(self, pagina: Any) -> None:
        """De negatieve controle bij de twee toetsen hierboven.

        ``_knop_bestemming`` zoekt naar een tag; vindt hij die nooit, dan zijn die twee
        toetsen vacuum waar en meten ze niets. Deze regel pint dat hij hem wel vindt.
        """
        project = _project()
        html = pagina(
            _met_invite(
                project, _basis_entry(**{"application-target": {"deployment": "production", "component": "frontend"}})
            )
        )

        assert _knop_bestemming(html) == _adres(project)


def _knop_bestemming(html: str) -> str | None:
    """Waar de knop op de succespagina heen wijst, of None als er geen knop staat.

    De hele pagina bevat andere adressen (stylesheets, de terugkeerlink), dus "geen enkele
    https op de pagina" zou niets zeggen. Dit leest de knop zelf.
    """
    knop = re.search(r"<nldd-button[^>]*\shref=\"([^\"]*)\"", html)
    return knop.group(1) if knop else None
