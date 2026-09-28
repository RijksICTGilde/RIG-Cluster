"""De bestemming van een uitnodiging moet meebewegen met het adres (RC-136).

De succesknop van een uitnodiging bewaarde een kale URL. Een hostname is afgeleid uit het
domeinformaat, het subdomein en het cluster, en alle drie kunnen wijzigen -- daarna wees de
opgeslagen URL naar een adres dat niet meer bestaat, terwijl alles om hem opnieuw uit te
rekenen gewoon in het projectbestand stond.

Vandaar ``application-target``: de KEUZE als ``component:deployment[:/pad]`` in plaats van
het antwoord. Deze suite houdt de vier plekken vast waar dat waar moet zijn: het model, de
samengestelde waarde zelf, wat het formulier wegschrijft, en wat de succespagina toont.

``application-url`` blijft geldig en gelijkwaardig, en bestaande projectbestanden worden
NIET herschreven: een uitnodiging is een lopende afspraak met iemand die de link al heeft,
en niet elke URL is afleidbaar. De voorrang is bestemming, dan URL, dan geen knop.
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
from opi.forms.editables.converters import InviteTargetConverter
from opi.manager.project_validation import validation_reasons
from opi.services.catalog.invite import InviteService
from opi.services.catalog.invite.config_model import InviteConfig
from opi.services.catalog.invite.target_format import join_target, split_target
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
            {"active": [_basis_entry(**{"application-target": "frontend:production"})]}
        )
        assert config.active[0].application_target == "frontend:production"

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
                                "application-target": "frontend:production",
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

    def test_een_waarde_die_geen_twee_namen_noemt_wordt_geweigerd(self) -> None:
        """Anders wordt een typefout stilletjes "geen knop", en dat ziet er precies zo uit
        als een bestemming die iemand expres heeft leeggelaten."""
        with pytest.raises(ValidationError) as fout:
            InviteConfig.model_validate({"active": [_basis_entry(**{"application-target": "frontend"})]})

        # ``validation_reasons`` is wat de gebruiker te zien krijgt; ``str(e)`` van pydantic
        # is ontwikkelaarsuitvoer met ``input_value`` erin en gaat nergens heen.
        melding = validation_reasons(fout.value)
        assert "component:deployment" in melding
        assert "frontend" not in melding, "de afgekeurde waarde hoort niet in de melding"

    def test_een_url_in_het_bestemmingsveld_wordt_geweigerd(self) -> None:
        """Wie zich vergist in het veld hoort dat te horen, niet een uitnodiging zonder
        knop te krijgen."""
        with pytest.raises(ValidationError):
            InviteConfig.model_validate(
                {"active": [_basis_entry(**{"application-target": "https://ergens.anders.nl/"})]}
            )


# ---------------------------------------------------------------------------
# 2. De samengestelde waarde zelf
# ---------------------------------------------------------------------------


class TestDeSamengesteldeWaarde:
    """``:`` is veilig als scheidingsteken; ``/`` zou het niet zijn.

    Deployment- en componentnamen zijn DNS-1123-labels en kunnen zelf geen dubbele punt
    bevatten, en het pad staat achteraan: splitsen van links met een maximum van twee laat
    een pad met een dubbele punt erin heel. Een pad BESTAAT uit schuine strepen, dus daarop
    splitsen gaat wel mis -- vandaar deze randgevallen.
    """

    def test_zonder_pad(self) -> None:
        assert split_target("frontend:production") == ("frontend", "production", None)

    def test_met_pad(self) -> None:
        assert split_target("frontend:production:/api") == ("frontend", "production", "/api")

    def test_een_pad_met_een_dubbele_punt_erin_blijft_heel(self) -> None:
        """Dit is de reden voor maxsplit=2 in plaats van een kale split."""
        assert split_target("frontend:production:/a:b/c") == ("frontend", "production", "/a:b/c")

    def test_een_pad_met_schuine_strepen_blijft_heel(self) -> None:
        assert split_target("frontend:production:/api/v2/dingen") == ("frontend", "production", "/api/v2/dingen")

    def test_een_lege_waarde_noemt_niets(self) -> None:
        assert split_target("") == ("", "", None)
        assert split_target(None) == ("", "", None)

    def test_rommel_die_niet_te_splitsen_valt_noemt_niets(self) -> None:
        """Geen uitzondering: vier aanroepers zouden dan hetzelfde oordeel moeten vellen,
        en een ervan rendert een publieke pagina."""
        assert split_target("frontend") == ("", "", None)

    def test_een_url_is_geen_bestemming(self) -> None:
        """Een URL bevat OOK dubbele punten, dus de kale splitsing maakte er vrolijk
        ("https", "//ergens.anders.nl/") van: een keuze die geen component noemt en toch
        geldig leek. De naamvorm is wat het scheidingsteken veilig maakt, dus die wordt
        getoetst."""
        assert split_target("https://ergens.anders.nl/") == ("", "", None)
        assert split_target("HOOFDLETTERS:production") == ("", "", None)

    @pytest.mark.parametrize(
        "waarde",
        ["frontend:production", "frontend:production:/api", "frontend:production:/a:b/c", "f:p:/api/v2"],
    )
    def test_heen_en_terug_levert_dezelfde_waarde_op(self, waarde: str) -> None:
        assert join_target(*split_target(waarde)) == waarde

    def test_zonder_pad_komt_er_geen_los_scheidingsteken_achter(self) -> None:
        assert join_target("frontend", "production", None) == "frontend:production"


# ---------------------------------------------------------------------------
# 2b. Bestaande bestanden worden niet herschreven
# ---------------------------------------------------------------------------


class TestBestaandeBestandenBlijvenMetRustGelaten:
    def test_de_dienst_staat_op_1_1(self) -> None:
        assert InviteService.config_schema_version == "1.1"

    def test_de_versieophoging_herschrijft_geen_gegevens(self) -> None:
        """Een uitnodiging is een lopende afspraak met iemand die de link al heeft: een
        migratie die de bestemming omzet verandert stilletjes waar die persoon uitkomt, en
        bij een foute match ergens anders dan de bedoeling was. Dit is bovendien de EERSTE
        ophoging in de catalogus, en het is prettig als die er een is die niets herschrijft."""
        config = {"active": [_basis_entry(**{"application-url": "https://ergens.anders.nl/"})]}

        assert InviteService().migrate_config(config, "1.0") == config

    def test_een_bestaand_adres_overleeft_een_opslag_door_het_formulier(self) -> None:
        """De keuzelijst bezit dat veld niet, dus opslaan kan hem niet laten vallen."""
        project = _met_invite(_project(), _basis_entry(**{"application-url": "https://ergens.anders.nl/"}))

        InviteService().settle_destination(project)

        assert project["services"][-1]["config"]["active"][0]["application-url"] == "https://ergens.anders.nl/"


# ---------------------------------------------------------------------------
# 3. Wat het formulier wegschrijft
# ---------------------------------------------------------------------------


class TestDeKeuzelijstSchrijftDeKeuze:
    def test_de_opgeslagen_waarde_is_de_bestemming_en_niet_de_url(self) -> None:
        """De lijst toont adressen -- die herkent een mens -- maar bewaart de keuze."""
        project = _project()

        opgeslagen = InviteTargetConverter().write(_adres(project), context_data=project)

        assert opgeslagen == "frontend:production"

    def test_een_component_met_twee_paden_bewaart_het_pad(self) -> None:
        """Twee paden zijn twee adressen; zonder het pad is de keuze niet eenduidig."""
        project = _project(paden=[{"match": "/"}, {"match": "/api"}])
        from opi.services.catalog.invite.destination import derived_destinations

        api = next(rij for rij in derived_destinations(project) if rij["path"] == "/api")

        opgeslagen = InviteTargetConverter().write(api["url"], context_data=project)

        assert opgeslagen == "frontend:production:/api"

    def test_de_lege_keuze_schrijft_geen_bestemming(self) -> None:
        assert InviteTargetConverter().write("", context_data=_project()) is None

    def test_een_adres_buiten_dit_project_levert_geen_keuze_op(self) -> None:
        assert InviteTargetConverter().write("https://ergens.anders.nl/", context_data=_project()) is None

    def test_de_opgeslagen_keuze_selecteert_zijn_eigen_regel_weer(self) -> None:
        """Anders staat de lijst bij het openen op "geen knop" terwijl er een bestemming is."""
        project = _project()

        assert InviteTargetConverter().read("frontend:production", context_data=project) == _adres(project)

    def test_een_keuze_die_niet_meer_oplost_selecteert_niets(self) -> None:
        assert InviteTargetConverter().read("frontend:weg", context_data=_project()) == ""

    def test_een_gekozen_bestemming_verdringt_een_bestaand_vast_adres(self) -> None:
        """Het model weigert allebei, dus de opslag moet er een overhouden: de nieuwe keuze."""
        project = _met_invite(
            _project(),
            _basis_entry(
                **{
                    "application-url": "https://ergens.anders.nl/",
                    "application-target": "frontend:production",
                }
            ),
        )

        InviteService().settle_destination(project)

        entry = project["services"][-1]["config"]["active"][0]
        assert "application-url" not in entry
        assert entry["application-target"] == "frontend:production"

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
    import opi.server

    # NIET mock_settings.SECRET_KEY. ``opi/server.py`` bindt ``settings`` bij zijn eigen
    # import; is die module al door een eerdere test geimporteerd, dan wijst die naam naar
    # de ECHTE settings en niet naar de mock, en tekent het koekje met een sleutel die de
    # app niet gebruikt. Deze regel leest de sleutel die de gebouwde app echt gebruikt, dus
    # in beide volgordes de goede -- de fixture hangt niet langer aan de collectievolgorde.
    _ = mock_settings
    sleutel = opi.server.settings.SECRET_KEY

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
            app = opi.server.create_app()
            client = TestClient(app)
            client.cookies.set("session", _sessiekoekje(sleutel))
            # follow_redirects=False, want TestClient VOLGT een 302 en dan meet de toets de
            # gevolgde pagina in plaats van de succespagina: de assert op 200 was groen op
            # het verkeerde scherm. Dit is de grendel die de fout hierboven zichtbaar maakt.
            antwoord = client.get(f"/invite/{SLEUTEL}/success", follow_redirects=False)
            assert antwoord.status_code == 200, f"{antwoord.status_code} -> {antwoord.headers.get('location')}"
            return antwoord.text

    return render


class TestDeSuccespagina:
    def test_een_bestemming_wordt_uitgerekend_bij_het_renderen(self, pagina: Any) -> None:
        project = _project()
        html = pagina(_met_invite(project, _basis_entry(**{"application-target": "frontend:production"})))

        assert _adres(project) in html

    def test_de_knop_volgt_een_gewijzigd_subdomein(self, pagina: Any) -> None:
        """Dit is de hele aanleiding: hetzelfde bestand, een ander subdomein, en de knop
        wijst mee in plaats van naar een adres dat niet meer bestaat."""
        entry = _basis_entry(**{"application-target": "frontend:production"})

        oud = _project(subdomein="production")
        nieuw = _project(subdomein="acceptatie")
        assert _adres(oud) != _adres(nieuw), "de toets meet niets als het adres niet verandert"

        assert _adres(oud) in pagina(_met_invite(oud, entry))
        assert _adres(nieuw) in pagina(_met_invite(nieuw, entry))

    def test_een_bestemming_die_niet_meer_oplost_toont_geen_knop(self, pagina: Any) -> None:
        """Geen knop is beter dan een knop die ergens verkeerd heen wijst: dat verschil
        ziet de gebruiker pas nadat hij geklikt heeft."""
        html = pagina(_met_invite(_project(), _basis_entry(**{"application-target": "weg:production"})))

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
        html = pagina(_met_invite(project, _basis_entry(**{"application-target": "frontend:production"})))

        assert _knop_bestemming(html) == _adres(project)


def _knop_bestemming(html: str) -> str | None:
    """Waar de knop op de succespagina heen wijst, of None als er geen knop staat.

    De hele pagina bevat andere adressen (stylesheets, de terugkeerlink), dus "geen enkele
    https op de pagina" zou niets zeggen. Dit leest de knop zelf.
    """
    knop = re.search(r"<nldd-button[^>]*\shref=\"([^\"]*)\"", html)
    return knop.group(1) if knop else None
