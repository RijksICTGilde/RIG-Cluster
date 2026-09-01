"""Een 5xx vertelt wat je eraan kunt doen, en geeft niets prijs.

De aanleiding: tijdens een databasestoring kreeg een gebruiker die een projectpagina
opvroeg dit kaal op het scherm::

    {"detail": "Template error: [Errno 111] Connect call failed ('172.30.19.11', 5432)"}

Een intern IP-adres, de databasepoort, geen opmaak en geen weg terug. Deze tests meten
de drie dingen die dat moesten worden: een pagina voor een browser, een envelop voor een
client, en in geen van beide iets uit de infrastructuur.
"""

from __future__ import annotations

import logging
import re

import pytest
from fastapi import APIRouter, HTTPException
from fastapi.testclient import TestClient

HTML = {"accept": "text/html,application/xhtml+xml"}
JSON = {"accept": "application/json"}

#: De uitzondering zoals hij tijdens de storing binnenkwam. Alles wat hieruit in een
#: antwoord terechtkomt is een lek: het IP-adres en de poort zijn de netwerkindeling
#: van het platform.
STORING = "[Errno 111] Connect call failed ('172.30.19.11', 5432)"

#: Wat er niet in een antwoord mag staan. Een IP-adres met poort, en de sjabloonnaam
#: waarmee de oude melding begon.
LEKT = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}|\b5432\b|Template error|Errno")

#: De zin die de webrouter zelf schrijft, geschreven voor de lezer en zonder uitzondering.
FOUTZIN = "Het project kon niet worden opgehaald. Probeer het over een minuut opnieuw."


@pytest.fixture
def foutclient(mock_settings: object) -> TestClient:
    """Een client met twee routes die falen zoals de echte pagina's faalden.

    ``fout-http`` gooit de HTTPException op die de webrouters opgooien; ``fout-kaal``
    laat een uitzondering ontsnappen, de tak die de ``Exception``-handler afvangt. Onder
    ``/health/`` omdat die prefix langs de autorisatiemiddleware komt en niet gemount is
    -- elders wordt een niet-ingelogde browser eerst naar de inlogpagina gestuurd en meet
    de test de foutafhandeling helemaal niet. Dezelfde 500 staat ook onder ``/api/``, om
    te kunnen meten dat daar nooit markup uitkomt.
    """
    from opi.server import create_app

    app = create_app()
    router = APIRouter()

    @router.get("/health/fout-http")
    async def _fout_http() -> None:
        raise HTTPException(status_code=500, detail=FOUTZIN)

    @router.get("/api/fout-http")
    async def _fout_http_api() -> None:
        raise HTTPException(status_code=500, detail=FOUTZIN)

    @router.get("/health/fout-kaal")
    async def _fout_kaal() -> None:
        raise OSError(STORING)

    app.include_router(router)
    # Productie draait met DEBUG uit (geen DEBUG in de configmaps, dus de default False).
    # Staat Starlette's debugmodus aan, dan komt er een stacktrace uit voordat welke
    # handler dan ook draait; de conftest zet DEBUG op de mock-settings aan, dus hier
    # expliciet terug naar de stand waarin een gebruiker deze fout ziet.
    app.debug = False
    # raise_server_exceptions=False: Starlette gooit een onafgevangen uitzondering na het
    # antwoord altijd opnieuw op, zodat de server hem kan loggen. Zonder dit ziet de test
    # die uitzondering in plaats van het antwoord dat de gebruiker krijgt.
    # Geen contextmanager: die zou de lifespan starten, en dit meet de foutafhandeling.
    return TestClient(app, raise_server_exceptions=False)


class TestEenBrowserKrijgtEenPagina:
    def test_een_5xx_is_een_pagina_en_geen_json(self, foutclient: TestClient) -> None:
        antwoord = foutclient.get("/health/fout-http", headers=HTML)
        assert antwoord.status_code == 500
        assert antwoord.headers["content-type"].startswith("text/html")
        assert "Er ging iets mis" in antwoord.text
        assert "Het project kon niet worden opgehaald" in antwoord.text

    def test_de_pagina_wijst_een_weg_terug(self, foutclient: TestClient) -> None:
        antwoord = foutclient.get("/health/fout-http", headers=HTML)
        assert 'href="/dashboard"' in antwoord.text

    def test_de_pagina_noemt_het_kenmerk(self, foutclient: TestClient) -> None:
        """Zonder kenmerk is 'er ging iets mis' een doodlopende weg voor wie het meldt."""
        antwoord = foutclient.get("/health/fout-http", headers=HTML)
        assert re.search(r"kenmerk:<br><code>req-[0-9a-f]{8}</code>", antwoord.text)

    def test_elk_verzoek_krijgt_zijn_eigen_kenmerk(self, foutclient: TestClient) -> None:
        eerste = re.search(r"req-[0-9a-f]{8}", foutclient.get("/health/fout-http", headers=HTML).text)
        tweede = re.search(r"req-[0-9a-f]{8}", foutclient.get("/health/fout-http", headers=HTML).text)
        assert eerste is not None
        assert tweede is not None
        assert eerste.group() != tweede.group()


class TestErLektNiets:
    def test_een_onafgevangen_uitzondering_komt_niet_op_de_pagina(self, foutclient: TestClient) -> None:
        antwoord = foutclient.get("/health/fout-kaal", headers=HTML)
        assert antwoord.status_code == 500
        assert antwoord.headers["content-type"].startswith("text/html")
        assert not LEKT.search(antwoord.text), "de infrastructuur staat op het scherm"

    def test_een_onafgevangen_uitzondering_komt_niet_in_de_envelop(self, foutclient: TestClient) -> None:
        antwoord = foutclient.get("/health/fout-kaal", headers=JSON)
        assert antwoord.status_code == 500
        assert not LEKT.search(antwoord.text), "de infrastructuur staat in het antwoord"

    def test_de_log_heeft_de_volledige_fout_met_hetzelfde_kenmerk(
        self, foutclient: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """De andere helft van de belofte: wat de gebruiker niet ziet, ziet de beheerder wel."""
        with caplog.at_level("ERROR"):
            antwoord = foutclient.get("/health/fout-kaal", headers=HTML)
        kenmerk = re.search(r"req-[0-9a-f]{8}", antwoord.text)
        assert kenmerk, "de pagina noemt geen kenmerk"
        geregistreerd = [r for r in caplog.records if STORING in (r.exc_text or "")]
        assert geregistreerd, "de volledige fout staat niet in de log"
        assert all(getattr(r, "flow_id", None) == kenmerk.group() for r in geregistreerd)


class TestEenClientKrijgtEenEnvelop:
    def test_de_envelop_is_problem_json_met_een_categorie(self, foutclient: TestClient) -> None:
        antwoord = foutclient.get("/health/fout-http", headers=JSON)
        assert antwoord.status_code == 500
        assert antwoord.headers["content-type"].startswith("application/problem+json")
        lichaam = antwoord.json()
        assert lichaam["category"] == "InternalError"
        assert lichaam["status"] == 500
        assert lichaam["instance"] == "/health/fout-http"
        assert re.fullmatch(r"req-[0-9a-f]{8}", lichaam["reference"])

    def test_detail_blijft_bestaan_voor_de_clients_die_het_lezen(self, foutclient: TestClient) -> None:
        """zad-cli drukt ``detail`` af; de envelop is een uitbreiding, geen breuk."""
        lichaam = foutclient.get("/health/fout-http", headers=JSON).json()
        assert lichaam["detail"].startswith("Het project kon niet worden opgehaald.")
        assert lichaam["reference"] in lichaam["detail"], "wie alleen detail leest mist het kenmerk"

    def test_een_client_krijgt_geen_html(self, foutclient: TestClient) -> None:
        antwoord = foutclient.get("/health/fout-http", headers=JSON)
        assert "<html" not in antwoord.text

    def test_een_api_pad_krijgt_nooit_html(self, foutclient: TestClient) -> None:
        """Ook niet aan een browser: een client die /api parseert mag geen markup krijgen."""
        antwoord = foutclient.get("/api/fout-http", headers=HTML)
        assert antwoord.status_code == 500
        assert "<html" not in antwoord.text
        assert antwoord.json()["category"] == "InternalError"


class TestDe404BlijftWatHijWas:
    """De regressietoets: het bestaande gedrag mag niet meeveranderen."""

    def test_een_browser_krijgt_de_404_pagina(self, foutclient: TestClient) -> None:
        antwoord = foutclient.get("/static/bestaat-niet.png", headers=HTML)
        assert antwoord.status_code == 404
        assert "Deze pagina bestaat niet" in antwoord.text

    def test_een_404_houdt_zijn_kale_detail_veld(self, foutclient: TestClient) -> None:
        antwoord = foutclient.get("/static/bestaat-niet.png", headers=JSON)
        assert antwoord.status_code == 404
        assert antwoord.headers["content-type"].startswith("application/json")
        assert antwoord.json() == {"detail": "Not Found"}

    def test_een_401_houdt_zijn_kale_detail_veld(self, foutclient: TestClient) -> None:
        antwoord = foutclient.get("/api/v2/projects", headers=HTML)
        assert antwoord.status_code == 401
        assert antwoord.headers["content-type"].startswith("application/json")
        assert antwoord.json()["detail"]


class _ToegelatenGebruikers:
    """Genoeg gebruikersdienst voor de middleware: opslaan, en dit adres toelaten."""

    def __init__(self, email: str) -> None:
        self._email = email

    def store_user(self, user: dict[str, str]) -> None:
        pass

    def is_email_allowed(self, email: str) -> bool:
        return email == self._email


class TestDeStoringZelf:
    """De pagina uit de melding, met de fout die er die avond optrad.

    De synthetische routes hierboven meten de handler; deze meet de weg ernaartoe. Het
    pad, de uitzondering en de status zijn die van de storing: ``/projects/dd-mco/details``
    terwijl de database niet antwoordde.
    """

    @pytest.fixture
    def projectclient(self, mock_settings: object, monkeypatch: pytest.MonkeyPatch) -> TestClient:
        from opi.middleware import authorization
        from opi.server import create_app
        from opi.web import router as webrouter

        gebruiker = {"email": "beheerder@voorbeeld.nl", "name": "Beheerder"}
        monkeypatch.setattr(authorization, "get_user", lambda request: gebruiker)
        # De middleware toetst het adres aan de allowlist en stuurt anders naar
        # /permission-denied; dan komt de route helemaal niet aan bod.
        monkeypatch.setattr(authorization, "get_user_service", lambda: _ToegelatenGebruikers(gebruiker["email"]))
        monkeypatch.setattr(webrouter, "get_current_user", lambda request: gebruiker)
        monkeypatch.setattr(webrouter, "is_user_authorized_for_project", lambda *args, **kwargs: True)

        def _database_onbereikbaar() -> object:
            raise OSError(STORING)

        monkeypatch.setattr(webrouter, "get_project_store", _database_onbereikbaar)

        app = create_app()
        app.debug = False
        return TestClient(app, raise_server_exceptions=False)

    def test_de_projectpagina_geeft_een_pagina_zonder_infrastructuur(self, projectclient: TestClient) -> None:
        antwoord = projectclient.get("/projects/dd-mco/details", headers=HTML, follow_redirects=False)

        assert antwoord.status_code == 500
        assert antwoord.headers["content-type"].startswith("text/html")
        assert "De projectpagina kon niet worden opgebouwd" in antwoord.text
        assert not LEKT.search(antwoord.text), "de infrastructuur staat op het scherm"
        assert re.search(r"kenmerk:<br><code>req-[0-9a-f]{8}</code>", antwoord.text)

    def test_dezelfde_aanroep_met_json_geeft_de_envelop(self, projectclient: TestClient) -> None:
        antwoord = projectclient.get("/projects/dd-mco/details", headers=JSON, follow_redirects=False)

        assert antwoord.status_code == 500
        assert "<html" not in antwoord.text
        assert antwoord.json()["category"] == "InternalError"
        assert not LEKT.search(antwoord.text)

    def test_de_log_heeft_wel_het_hele_verhaal(
        self, projectclient: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("ERROR"):
            antwoord = projectclient.get("/projects/dd-mco/details", headers=HTML, follow_redirects=False)

        kenmerk = re.search(r"req-[0-9a-f]{8}", antwoord.text)
        assert kenmerk is not None
        regels = [r for r in caplog.records if STORING in (r.exc_text or "") or STORING in r.getMessage()]
        assert regels, "de volledige fout staat niet in de log"
        assert any(getattr(r, "flow_id", None) == kenmerk.group() for r in regels)


class TestEenOnbekendeStatus:
    """Een uitzondering in de foutafhandeling is het ene ding dat nooit mag gebeuren."""

    def test_een_status_buiten_de_standaard_valt_niet_om(self) -> None:
        from opi.core.errors import statusomschrijving

        assert statusomschrijving(500) == "Internal Server Error"
        assert statusomschrijving(599) == "599"


class TestDeLogKrijgtHetRegelnummer:
    """Het regelnummer en de bronregel van Jinja2 verhuisden van het antwoord naar de log."""

    def _mislukking(self) -> Exception:
        fout = ValueError("expected token 'end of print statement'")
        fout.lineno = 12  # type: ignore[attr-defined]
        fout.source = "\n".join(f"regel {n}" for n in range(1, 20))  # type: ignore[attr-defined]
        return fout

    def test_regelnummer_en_bronregel_staan_in_de_logregel(self, caplog: pytest.LogCaptureFixture) -> None:
        from opi.core.errors import log_render_failure

        logger = logging.getLogger("toets.render")
        with caplog.at_level("ERROR"):
            try:
                raise self._mislukking()
            except ValueError as exc:
                log_render_failure(logger, "het dashboard", exc)

        bericht = caplog.records[-1].getMessage()
        assert "het dashboard" in bericht
        assert "regel 12" in bericht
        assert "bron: regel 12" in bericht
        assert caplog.records[-1].exc_info is not None, "zonder traceback heeft de beheerder niets"

    def test_een_fout_zonder_regelnummer_levert_gewoon_de_tekst(self, caplog: pytest.LogCaptureFixture) -> None:
        from opi.core.errors import log_render_failure

        logger = logging.getLogger("toets.render")
        with caplog.at_level("ERROR"):
            try:
                raise OSError(STORING)
            except OSError as exc:
                log_render_failure(logger, "de projectpagina", exc)

        assert STORING in caplog.records[-1].getMessage()
