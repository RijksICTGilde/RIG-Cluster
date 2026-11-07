"""/permission-denied, in zijn twee gevallen (398f2d80e).

De pagina liep eerder over ``<c-list>``/``<c-link>``: ``c-list-item`` legt tekst en link
als twee items naast elkaar, en die plakten in de weergave aan elkaar zonder spatie. De
reparatie zet de uitleg in ``<nldd-rich-text>`` met gewone ``<ul>``/``<p>`` en een
``<nldd-link>``, zodat de tekstloop een spatie houdt. Deze toetsen meten de markup die dat
garandeert, niet alleen dat de pagina rendert.
"""

from unittest.mock import patch

from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from opi.server import create_app, settings
from tests.inmemory_session_store import InMemorySessionStore

SESSIE_ID = "permission-denied-sessie"


def _sessiekoekje(secret: str) -> str:
    return TimestampSigner(secret).sign(SESSIE_ID.encode()).decode()


def _pagina_met_denied_reason(mock_settings: object) -> str:
    """De lazy ``get_session_store()`` leest de store pas tijdens het verzoek, dus de patch
    moet nog aan staan als de GET binnenkomt."""
    sleutel = settings.SECRET_KEY
    sessie_store = InMemorySessionStore()
    sessie_store.seed(SESSIE_ID, {"denied_reason": "email-not-verified"})
    with patch("opi.middleware.server_side_session.get_session_store", return_value=sessie_store):
        client = TestClient(create_app())
        client.cookies.set("session", _sessiekoekje(sleutel))
        return client.get("/permission-denied").text


def test_geen_toegang_zonder_geverifieerd_emailadres_plakt_tekst_en_link_niet_aan_elkaar(
    mock_settings: object,
) -> None:
    html = _pagina_met_denied_reason(mock_settings)

    assert "Geen Toegang: e-mailadres niet geverifieerd" in html
    assert "<c-list" not in html
    assert "<c-link" not in html
    assert (
        'Neem contact op met de\n                    <nldd-link href="mailto:robbert.uittenbroek@rijksoverheid.nl" '
        'text="platformbeheerder"></nldd-link>' in html
    )


def test_geen_toegang_zonder_lijst_van_toegelaten_gebruikers_plakt_tekst_en_link_niet_aan_elkaar(
    test_client: TestClient,
) -> None:
    """Zonder session-cookie: dezelfde route, het andere (meest gewone) geval."""
    html = test_client.get("/permission-denied").text

    assert "Geen Toegang tot ZAD" in html
    assert "<c-list" not in html
    assert (
        "Neem contact op met een collega die al een project in ZAD heeft of de\n                "
        '<nldd-link href="mailto:robbert.uittenbroek@rijksoverheid.nl" text="platformbeheerder"></nldd-link>' in html
    )


def test_de_twee_gevallen_tonen_een_andere_kop(mock_settings: object, test_client: TestClient) -> None:
    """Het onderscheid dat de template zelf claimt te bewaren."""
    zonder_lijst = test_client.get("/permission-denied").text
    niet_geverifieerd = _pagina_met_denied_reason(mock_settings)

    assert "Geen Toegang tot ZAD" in zonder_lijst
    assert "Geen Toegang: e-mailadres niet geverifieerd" in niet_geverifieerd
    assert "Geen Toegang: e-mailadres niet geverifieerd" not in zonder_lijst
