"""Faalconditie: zonder SECURITY_PROOF_TOKEN kaatst /health niets terug, en met een gezet
token kaatst /health precies dat token terug op SECURITY_PROOF_HEADER. Dit is de enige plek
waar de aanvalsproef een doel in het cluster van de wegwerp-sandbox onderscheidt.
"""

from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient
from opi.core.config import SECURITY_PROOF_HEADER, settings
from opi.server import create_app


def test_health_draagt_geen_header_zonder_proeftoken() -> None:
    with patch.object(settings, "SECURITY_PROOF_TOKEN", ""):
        response = TestClient(create_app()).get("/health")
    assert response.status_code == 200
    assert SECURITY_PROOF_HEADER not in response.headers


def test_health_kaatst_het_gezette_proeftoken_terug() -> None:
    with patch.object(settings, "SECURITY_PROOF_TOKEN", "het-token-van-deze-run"):
        response = TestClient(create_app()).get("/health")
    assert response.status_code == 200
    assert response.headers[SECURITY_PROOF_HEADER] == "het-token-van-deze-run"
