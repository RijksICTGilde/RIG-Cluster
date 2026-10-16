"""``/metrics`` geeft alleen metrieken met het scrape-token van onze Prometheus.

Gemeten op de echte app via de testclient: zonder header, met een ander schema dan Bearer
of met een fout token komt er geen enkele metriek terug, en een niet geconfigureerd token
houdt de route dicht in plaats van open.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import opi.api.endpoint_util as endpoint_util
import pytest

if TYPE_CHECKING:
    from fastapi.testclient import TestClient
    from httpx import Response

SCRAPE_TOKEN = "scrape-token-for-testing-only"


@pytest.fixture
def scrape_token(monkeypatch: pytest.MonkeyPatch) -> str:
    """Zet het token op het settings-object dat de decorator leest."""
    monkeypatch.setattr(endpoint_util.settings, "PROMETHEUS_METRICS_AUTH_TOKEN", SCRAPE_TOKEN)
    return SCRAPE_TOKEN


def _geen_metrieken(response: Response) -> None:
    """Geen enkele metriek en geen hint over het token in het antwoord."""
    assert "# HELP" not in response.text
    assert SCRAPE_TOKEN not in response.text


class TestMetricsScrapeToken:
    def test_zonder_header_401(self, test_client: TestClient, scrape_token: str) -> None:
        response = test_client.get("/metrics")

        assert response.status_code == 401
        _geen_metrieken(response)

    def test_ander_schema_dan_bearer_401(self, test_client: TestClient, scrape_token: str) -> None:
        response = test_client.get("/metrics", headers={"Authorization": f"Basic {scrape_token}"})

        assert response.status_code == 401
        _geen_metrieken(response)

    def test_fout_token_401(self, test_client: TestClient, scrape_token: str) -> None:
        response = test_client.get("/metrics", headers={"Authorization": "Bearer niet-het-token"})

        assert response.status_code == 401
        _geen_metrieken(response)

    def test_token_niet_geconfigureerd_501(self, test_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        """Zonder geconfigureerd token is het endpoint dicht, zoals bij een ontbrekende ADMIN_API_KEY."""
        monkeypatch.setattr(endpoint_util.settings, "PROMETHEUS_METRICS_AUTH_TOKEN", None)

        response = test_client.get("/metrics", headers={"Authorization": f"Bearer {SCRAPE_TOKEN}"})

        assert response.status_code == 501
        _geen_metrieken(response)

    def test_goed_token_200_met_metrieken(self, test_client: TestClient, scrape_token: str) -> None:
        response = test_client.get("/metrics", headers={"Authorization": f"Bearer {scrape_token}"})

        assert response.status_code == 200
        assert "# HELP" in response.text
        assert "process_" in response.text

    def test_metrics_explorer_blijft_naar_login_sturen(self, test_client: TestClient, scrape_token: str) -> None:
        """De pagina ernaast verandert niet: zonder sessie nog steeds een redirect naar login."""
        response = test_client.get("/metrics-explorer", follow_redirects=False)

        assert response.status_code == 302
        assert response.headers["location"] == "/auth/login"

    def test_niet_ascii_byte_in_header_401(self, test_client: TestClient, scrape_token: str) -> None:
        """Een rauwe byte boven 0x7F in de header is een foute credential, geen 500.

        De header gaat als bytes mee: via een str-header weigert httpx hem al voordat de app
        hem ziet.
        """
        response = test_client.get("/metrics", headers={b"Authorization": b"Bearer caf\xe9"})

        assert response.status_code == 401
        _geen_metrieken(response)
