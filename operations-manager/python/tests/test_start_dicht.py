"""OPI start dicht: zonder instelling geen debugstand, geen proefroutes, geen open docs.

Wat hier geborgd wordt, valt stil om als het verdwijnt: een vergeten ``DEBUG_MODE`` zette
de debugger of de bestandsbewaking aan, en de LOTC-proefopstelling met haar fixture-data
stond zonder SSO op elke omgeving. Daarom hangt de registratie van die routes aan
``ENVIRONMENT``, en toetst ``test_de_basisdeployment_zet_environment_op_production`` dat de
basisdeployment die waarde echt zet.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from opi.core.config import Settings, settings
from opi.server import create_app
from opi.services.user_service import get_user_service
from tests.inmemory_session_store import InMemorySessionStore

REPO_WORTEL = Path(__file__).resolve().parents[3]
BASISDEPLOYMENT = REPO_WORTEL / "bootstrap/rig-system/kustomize/operations-manager/base/deployment.yaml"
DEV_COMPOSE = REPO_WORTEL / "docker-compose.dev.yaml"

TESTPADEN = ("/test-hero", "/forms/formulier", "/test-template-variables", "/example")

GEBRUIKER = {"sub": "start-dicht", "email": "start-dicht@voorbeeld.nl", "name": "Toets"}
SESSIE_ID = "start-dicht-sessie"


def _app_in(environment: str):
    with patch.object(settings, "ENVIRONMENT", environment):
        return create_app()


def _sessiecookie() -> str:
    """De ondertekende verwijzing naar de server-side sessie (RC-390): alleen een id."""
    return TimestampSigner(settings.SECRET_KEY).sign(SESSIE_ID.encode()).decode()


@pytest.fixture
def ingelogde_client() -> TestClient:
    """Een client met een sessie in een in-memory store: hier is geen database.

    ``login_at`` hoort erbij: ``session_guard`` weigert sinds RC-287 een sessie zonder
    inlogtijdstip, en dan loopt elk verzoek hier op een redirect naar de login uit.
    """
    get_user_service().add_allowed_emails([GEBRUIKER["email"]])
    store = InMemorySessionStore()
    store.seed(SESSIE_ID, {"user": {**GEBRUIKER, "login_at": time.time()}})
    with patch("opi.middleware.server_side_session.get_session_store", return_value=store):
        client = TestClient(_app_in("local"))
        client.cookies.set("session", _sessiecookie())
        yield client


# ---------------------------------------------------------------------------
# Stap 1: de default is de dichte stand
# ---------------------------------------------------------------------------


def test_debug_mode_valt_terug_op_production() -> None:
    assert Settings.model_fields["DEBUG_MODE"].default == "production"


def test_environment_valt_terug_op_production() -> None:
    assert Settings.model_fields["ENVIRONMENT"].default == "production"


def test_settings_zonder_environment_staat_niet_in_ontwikkelstand() -> None:
    """``tests/conftest.py`` zet ENVIRONMENT op "local" voor de hele run, dus hier weg
    ermee: zonder de variabele hoort Settings in productiestand te komen.
    """
    with patch.dict("os.environ", {}, clear=True):
        instellingen = Settings(SECRET_KEY="x" * 32)

    assert instellingen.ENVIRONMENT == "production"
    assert not instellingen.is_development_environment


def test_debugpy_luistert_alleen_op_localhost() -> None:
    """``debugpy.listen`` bindt op 127.0.0.1, niet op 0.0.0.0: de poort moet niet aan het
    netwerk van het cluster hangen, alleen aan wat `kubectl port-forward` lokaal bereikt.
    """
    server_py = (REPO_WORTEL / "operations-manager/python/opi/server.py").read_text()

    assert 'debugpy.listen(("127.0.0.1", 5678))' in server_py
    assert 'debugpy.listen(("0.0.0.0", 5678))' not in server_py


# ---------------------------------------------------------------------------
# Stap 2: poort 5678 alleen in de debug-overlay
# ---------------------------------------------------------------------------

OPERATIONS_MANAGER_KUSTOMIZE = REPO_WORTEL / "bootstrap/rig-system/kustomize/operations-manager"


def test_de_basis_service_publiceert_geen_debugpoort() -> None:
    service = yaml.safe_load((OPERATIONS_MANAGER_KUSTOMIZE / "base/service.yaml").read_text())

    assert 5678 not in {poort["port"] for poort in service["spec"]["ports"]}


def test_de_debug_overlay_patcht_de_debugpoort_op_de_service() -> None:
    patch = yaml.safe_load(
        (OPERATIONS_MANAGER_KUSTOMIZE / "overlays/sandboxed-local/patches/enable-debug-service.yaml").read_text()
    )
    kustomization = (OPERATIONS_MANAGER_KUSTOMIZE / "overlays/sandboxed-local-debug/kustomization.yaml").read_text()

    assert 5678 in {poort["port"] for poort in patch["spec"]["ports"]}
    assert "enable-debug-service.yaml" in kustomization


def test_de_dockerimage_publiceert_geen_debugpoort() -> None:
    dockerfile = (REPO_WORTEL / "operations-manager/Dockerfile").read_text()

    assert "EXPOSE 5678" not in dockerfile
    assert "EXPOSE 8000" in dockerfile


# ---------------------------------------------------------------------------
# Stap 3: proef- en testroutes alleen lokaal en in de sandbox
# ---------------------------------------------------------------------------


def _paden(app) -> set[str]:
    return {getattr(route, "path", "") for route in app.routes}


@pytest.mark.parametrize("environment", ["local", "sandboxed-local"])
def test_de_proefopstelling_bestaat_lokaal_en_in_de_sandbox(environment: str) -> None:
    paden = _paden(_app_in(environment))

    assert any(pad.startswith("/lotc/") for pad in paden)
    assert set(TESTPADEN) <= paden


def test_de_proefopstelling_bestaat_niet_onder_de_default() -> None:
    paden = _paden(_app_in(Settings.model_fields["ENVIRONMENT"].default))

    assert not [pad for pad in paden if pad.startswith("/lotc/")]
    assert not set(TESTPADEN) & paden


def test_de_proefopstelling_bestaat_niet_in_productie() -> None:
    paden = _paden(_app_in("production"))

    assert not [pad for pad in paden if pad.startswith("/lotc/")]
    assert not set(TESTPADEN) & paden


def test_de_assets_van_het_componentensysteem_blijven_in_productie() -> None:
    assert "/static/lotc/{rel:path}" in _paden(_app_in("production"))


# ---------------------------------------------------------------------------
# Stap 4: Swagger en ReDoc achter een sessie, het document publiek
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pad", ["/docs", "/redoc"])
def test_de_api_documentatie_vraagt_een_sessie(pad: str) -> None:
    client = TestClient(_app_in("production"), follow_redirects=False)

    antwoord = client.get(pad)

    assert antwoord.status_code == 302
    assert antwoord.headers["location"] == "/auth/login"


@pytest.mark.parametrize(("pad", "kenmerk"), [("/docs", "swagger-ui"), ("/redoc", "redoc")])
def test_de_api_documentatie_rendert_met_een_sessie(ingelogde_client: TestClient, pad: str, kenmerk: str) -> None:
    antwoord = ingelogde_client.get(pad)

    assert antwoord.status_code == 200
    assert kenmerk in antwoord.text.lower()


def test_het_openapi_document_blijft_publiek() -> None:
    antwoord = TestClient(_app_in("production"), follow_redirects=False).get("/openapi.json")

    assert antwoord.status_code == 200
    assert antwoord.json()["openapi"]


# ---------------------------------------------------------------------------
# Stap 5: de help-route rendert alleen helpteksten
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("template", ["redis/help.md", "publish_on_web/guide.md", "container-image.html.j2"])
def test_de_help_route_rendert_een_helptekst(ingelogde_client: TestClient, template: str) -> None:
    antwoord = ingelogde_client.get(f"/forms/wizard/help/{template}", headers={"HX-Request": "true"})

    assert antwoord.status_code == 200


@pytest.mark.parametrize("template", ["bg/dashboard.html.j2", "image_registries/section-detail.html.j2"])
def test_de_help_route_weigert_een_ander_sjabloon(ingelogde_client: TestClient, template: str) -> None:
    antwoord = ingelogde_client.get(f"/forms/wizard/help/{template}", headers={"HX-Request": "true"})

    assert antwoord.status_code == 404


@pytest.fixture
def niet_whitelisted_help_bestand() -> str:
    """Een .md-bestand dat Jinja/de markdown-lezer prima vindt, maar dat geen help.md of
    guide.md heet. Zonder de bestandsnaamcontrole in service_help zou dit gewoon renderen:
    het bewijst dat de 404 van de whitelist komt, niet van een ontbrekend bestand.
    """
    pad = REPO_WORTEL / "operations-manager/python/opi/services/catalog/redis/notitie.md"
    pad.write_text("# Notitie\n\nGeen officiele help, maar wel een geldig bestand.\n")
    try:
        yield "redis/notitie.md"
    finally:
        pad.unlink()


def test_de_help_route_weigert_een_md_bestand_dat_geen_help_of_guide_heet(
    ingelogde_client: TestClient, niet_whitelisted_help_bestand: str
) -> None:
    antwoord = ingelogde_client.get(
        f"/forms/wizard/help/{niet_whitelisted_help_bestand}", headers={"HX-Request": "true"}
    )

    assert antwoord.status_code == 404


# ---------------------------------------------------------------------------
# Stap 6: de guard onder stap 3
# ---------------------------------------------------------------------------


def test_de_basisdeployment_zet_environment_op_production() -> None:
    deployment = yaml.safe_load(BASISDEPLOYMENT.read_text())
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    omgeving = {variabele["name"]: variabele.get("value") for variabele in container["env"]}

    assert omgeving["ENVIRONMENT"] == "production"


def test_de_dev_compose_zet_een_environment_die_de_proefroutes_houdt() -> None:
    """De docker-compose-dev-weg hoort de proefopstelling te houden, dus zijn ENVIRONMENT
    moet binnen is_development_environment vallen.
    """
    compose = yaml.safe_load(DEV_COMPOSE.read_text())
    omgeving = dict(
        regel.split("=", 1) for regel in compose["services"]["operations-manager"]["environment"] if "=" in regel
    )

    assert Settings(ENVIRONMENT=omgeving["ENVIRONMENT"]).is_development_environment
