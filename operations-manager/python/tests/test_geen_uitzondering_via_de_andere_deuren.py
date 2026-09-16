"""De drie deuren naast ``detail=``, gemeten op de route zelf.

De grendel (``test_geen_uitzondering_in_foutmelding.py``) leest de bron en zegt wat er
NIET meer in staat. Deze tests doen het andersom: ze laten de laag eronder omvallen op de
manier waarop hij dat in productie deed, en meten wat de aanroeper dan terugkrijgt.

Elke test hoort bij een deur die de grendel eerst niet kende, en achter elke deur stond
een levend lek:

* een sjabloon-render -- het resourcegebruik zette de melding van Prometheus (hostnaam,
  poort, errno) in ``ctx["usage_error"]``, en de kaart drukte hem letterlijk af;
* een teruggegeven dict -- het terugzetten gaf ``f"Database restore error: {e}"`` terug,
  die de aanroeper als ``message`` in een 500 kreeg;
* een regel in een lijst -- de logs-route zette ``str(e)`` per onderdeel in een 200-body.

Wat er nu uitkomt is dezelfde zin voor elke storing, met het kenmerk waarmee een beheerder
de volledige fout in de log terugvindt.
"""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

#: De uitzondering zoals hij binnenkwam: de storing uit de melding, en de vorm die
#: Prometheus eromheen zet als hij zijn eigen adres niet kan vinden.
STORING = "[Errno 111] Connect call failed ('172.30.19.11', 5432)"
PROMETHEUS_STORING = (
    "HTTPConnectionPool(host='prometheus.rig-system', port=9090): Max retries exceeded "
    "with url: /api/v1/query (Caused by NameResolutionError('[Errno -2] Name or service not known'))"
)

#: Wat er niet in een antwoord mag staan: een IP-adres, een poort, een hostnaam uit het
#: cluster, of de errno waarmee zo'n melding begint.
LEKT = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}|\b5432\b|\b9090\b|rig-system|Errno|HTTPConnectionPool")

#: Het kenmerk zoals het op het scherm en in de log staat.
KENMERK = re.compile(r"req-[0-9a-f]{8}")

API_KEY = "test-api-key"
PROJECT = "dd-mco"


class _ToegelatenGebruikers:
    def __init__(self, email: str) -> None:
        self._email = email

    def store_user(self, user: dict[str, str]) -> None:
        pass

    def is_email_allowed(self, email: str) -> bool:
        return email == self._email


# ---------------------------------------------------------------- de sjabloon-deur


class TestDeSjabloonDeur:
    """``/projects/details/{naam}/resource-usage`` met een onbereikbare Prometheus.

    Het fragment komt met htmx in het tabblad Project. De melding stond in een
    ``c-alert`` op het scherm van de gebruiker: hostnaam, poort en errno van de
    metingsdienst, uit een ``except Exception``.
    """

    @pytest.fixture
    def fragmentclient(self, mock_settings: object, monkeypatch: pytest.MonkeyPatch) -> TestClient:
        from opi.connectors import prometheus
        from opi.middleware import authorization
        from opi.server import create_app
        from opi.web import router as webrouter

        gebruiker = {"email": "beheerder@voorbeeld.nl", "name": "Beheerder"}
        monkeypatch.setattr(authorization, "get_user", lambda request: gebruiker)
        monkeypatch.setattr(authorization, "get_user_service", lambda: _ToegelatenGebruikers(gebruiker["email"]))
        monkeypatch.setattr(webrouter, "get_current_user", lambda request: gebruiker)
        monkeypatch.setattr(webrouter, "is_user_authorized_for_project", lambda *args, **kwargs: True)

        # Een deployment op DIT cluster, want zonder namespace vraagt de route de
        # meting niet eens op en meet de test niets.
        from opi.core.config import settings

        project = MagicMock()
        project.name = PROJECT
        project.data = {"deployments": [{"name": "main", "cluster": settings.CLUSTER_MANAGER, "namespace": PROJECT}]}
        monkeypatch.setattr(webrouter, "get_project_store", lambda: MagicMock(get=lambda naam: project))

        async def _prometheus_valt_om() -> object:
            raise OSError(PROMETHEUS_STORING)

        monkeypatch.setattr(prometheus, "get_metrics_connector", _prometheus_valt_om)

        app = create_app()
        app.debug = False
        return TestClient(app, raise_server_exceptions=False)

    def test_de_kaart_toont_de_lezerszin_en_niet_de_meting(self, fragmentclient: TestClient) -> None:
        antwoord = fragmentclient.get(f"/projects/details/{PROJECT}/resource-usage", follow_redirects=False)

        assert antwoord.status_code == 200, antwoord.text
        # De kop van de melding staat in het sjabloon; de zin eronder komt uit de route.
        assert "Resourcegebruik is niet op te halen" in antwoord.text
        assert "Probeer het over een minuut opnieuw." in antwoord.text
        assert not LEKT.search(antwoord.text), "de meetdienst staat op het scherm"

    def test_de_kaart_noemt_het_kenmerk(self, fragmentclient: TestClient) -> None:
        """Zonder kenmerk is 'niet op te halen' een doodlopende weg voor wie het meldt."""
        antwoord = fragmentclient.get(f"/projects/details/{PROJECT}/resource-usage", follow_redirects=False)

        assert KENMERK.search(antwoord.text), antwoord.text

    def test_de_log_heeft_wel_de_hele_melding(
        self, fragmentclient: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("ERROR"):
            antwoord = fragmentclient.get(f"/projects/details/{PROJECT}/resource-usage", follow_redirects=False)

        kenmerk = KENMERK.search(antwoord.text)
        assert kenmerk is not None
        regels = [r for r in caplog.records if PROMETHEUS_STORING in (r.exc_text or "")]
        assert regels, "de volledige fout staat niet in de log"
        assert any(getattr(r, "flow_id", None) == kenmerk.group() for r in regels)


# -------------------------------------------------------------- de lijstregel-deur


class _FakeStore:
    def __init__(self, data: dict[str, Any]) -> None:
        project = MagicMock()
        project.name = PROJECT
        project.api_key = API_KEY
        project.filename = f"{PROJECT}.yaml"
        project.data = data
        self._project = project

    def get(self, project_name: str) -> Any:
        return self._project if project_name == PROJECT else None


class TestDeLijstregelDeur:
    """``/api/logs/{project}`` als kubectl niet antwoordt.

    Per onderdeel een regel; de tak die geen logs kreeg zette ``str(e)`` in het
    ``error``-veld. Dat veld blijft bestaan -- een client leest eraan af dat dit
    onderdeel niets opleverde -- maar draagt de uitzondering niet meer.
    """

    @pytest.fixture
    def logsclient(self, mock_settings: Any) -> Any:
        from opi.api.logs_router import logs_router

        data = {
            "deployments": [
                {
                    "name": "main",
                    "cluster": mock_settings.CLUSTER_MANAGER,
                    "namespace": PROJECT,
                    "components": [{"reference": "web"}],
                }
            ]
        }
        store = _FakeStore(data)
        kubectl = MagicMock()
        kubectl.get_deployment_logs = AsyncMock(side_effect=OSError(STORING))

        app = FastAPI()
        app.include_router(logs_router)
        with (
            patch("opi.api.endpoint_util.get_project_store", return_value=store),
            patch("opi.api.logs_router.get_project_store", return_value=store),
            patch("opi.api.logs_router.KubectlConnector", return_value=kubectl),
            patch("opi.api.logs_router.get_prefixed_namespace", side_effect=lambda cluster, ns: f"rig-{ns}"),
        ):
            yield TestClient(app)

    def test_het_foutveld_draagt_de_uitzondering_niet_meer(self, logsclient: TestClient) -> None:
        antwoord = logsclient.get(f"/api/logs/{PROJECT}", headers={"X-API-Key": API_KEY})

        assert antwoord.status_code == 200, antwoord.text
        (regel,) = antwoord.json()["results"]
        assert "niet op te halen" in regel["error"]
        assert not LEKT.search(antwoord.text), "de uitzondering staat in de body"

    def test_het_foutveld_blijft_bestaan(self, logsclient: TestClient) -> None:
        """Een client leest hieraan af dat dit onderdeel geen logs opleverde."""
        antwoord = logsclient.get(f"/api/logs/{PROJECT}", headers={"X-API-Key": API_KEY})

        (regel,) = antwoord.json()["results"]
        assert regel["lines"] == []
        assert regel["error"]


class TestHetKenmerkZonderRequest:
    """``kenmerk_nu()``: voor een hulpfunctie die geen ``request`` in handen heeft.

    De ArgoCD-kaart wordt gevuld door ``_fetch_argocd_deployment_status``, die alleen de
    connector en de deployment krijgt. Zonder deze weg naar het kenmerk zou de melding op
    die kaart wel zeggen wat er mis is, maar niet wat je moet doorgeven.
    """

    def test_binnen_een_verzoek_is_het_het_kenmerk_van_dat_verzoek(self) -> None:
        from opi.core.errors import kenmerk_nu
        from opi.core.flow_id import set_flow_id

        gezet = set_flow_id("req")

        assert kenmerk_nu() == gezet
        assert KENMERK.fullmatch(gezet), gezet

    def test_buiten_een_verzoek_is_er_geen_kenmerk_en_geen_staart(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Buiten een verzoek staat de contextvar op ``-``; dat is niets om te melden,
        dus krijgt de zin er ook geen staart bij die naar een leeg kenmerk wijst."""
        from opi.core import errors

        monkeypatch.setattr(errors, "get_flow_id", lambda: "-")

        assert errors.kenmerk_nu() == ""
        assert errors.met_kenmerk("Er ging iets mis.", errors.kenmerk_nu()) == "Er ging iets mis."
