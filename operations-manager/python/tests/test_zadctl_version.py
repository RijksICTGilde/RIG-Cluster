"""De zadctl-versiecheck: 426 onder het minimum, een header onder de aanbevolen versie."""

import pytest
from opi.core.config import Settings
from opi.core.zadctl_version import parse_setting, version_from_user_agent
from opi.middleware.zadctl_version import MINIMUM_HEADER, RECOMMENDED_HEADER, ZadctlVersionMiddleware
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient


def _client(minimum: str = "", recommended: str = "") -> TestClient:
    async def ok(request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/api/v2/projects", ok), Route("/projects", ok)])
    app.add_middleware(ZadctlVersionMiddleware, minimum=minimum, recommended=recommended)
    return TestClient(app)


def _get(client: TestClient, user_agent: str | None, path: str = "/api/v2/projects"):
    headers = {"User-Agent": user_agent} if user_agent is not None else {}
    return client.get(path, headers=headers)


@pytest.mark.parametrize("sent", ["0.15.9", "0.9.99"])
def test_onder_het_minimum_is_426(sent):
    response = _get(_client(minimum="0.16.0"), f"zadctl/{sent}")

    assert response.status_code == 426
    assert response.headers[MINIMUM_HEADER] == "0.16.0"
    body = response.json()
    assert body["client"] == "zadctl"
    assert body["version"] == sent
    assert body["minimum"] == "0.16.0"
    assert body["upgrade"].startswith("https://github.com/RijksICTGilde/zad-cli/releases")


@pytest.mark.parametrize("sent", ["0.16.0", "0.16.1", "1.0.0"])
def test_op_of_boven_het_minimum_gaat_door(sent):
    response = _get(_client(minimum="0.16.0"), f"zadctl/{sent} (darwin arm64)")

    assert response.status_code == 200
    assert RECOMMENDED_HEADER not in response.headers


def test_onder_de_aanbevolen_versie_gaat_door_met_een_header():
    response = _get(_client(minimum="0.16.0", recommended="0.17.0"), "zadctl/0.16.5")

    assert response.status_code == 200
    assert response.headers[RECOMMENDED_HEADER] == "0.17.0"


def test_op_de_aanbevolen_versie_geen_header():
    response = _get(_client(recommended="0.17.0"), "zadctl/0.17.0")

    assert response.status_code == 200
    assert RECOMMENDED_HEADER not in response.headers


@pytest.mark.parametrize("user_agent", [None, "python-httpx/0.28.1", "curl/8.7.1", "zadctl/latest", "xzadctl/0.1.0"])
def test_zonder_zadctl_user_agent_niets(user_agent):
    response = _get(_client(minimum="9.0.0", recommended="9.0.0"), user_agent)

    assert response.status_code == 200
    assert RECOMMENDED_HEADER not in response.headers


def test_buiten_api_niets():
    response = _get(_client(minimum="9.0.0"), "zadctl/0.1.0", path="/projects")

    assert response.status_code == 200


def test_zonder_grenzen_niets():
    response = _get(_client(), "zadctl/0.0.1")

    assert response.status_code == 200
    assert RECOMMENDED_HEADER not in response.headers


def test_version_from_user_agent():
    assert version_from_user_agent("zadctl/0.16.0") == (0, 16, 0)
    assert version_from_user_agent("zadctl/1.2.3 extra") == (1, 2, 3)
    assert version_from_user_agent("python-httpx/0.28.1") is None


def test_parse_setting():
    assert parse_setting("X", "") is None
    assert parse_setting("X", "0.16.0") == (0, 16, 0)
    with pytest.raises(RuntimeError, match=r"X\.Y\.Z"):
        parse_setting("X", "0.16")


@pytest.mark.parametrize("field", ["ZADCTL_MIN_VERSION", "ZADCTL_RECOMMENDED_VERSION"])
def test_settings_weigeren_een_versie_die_geen_semver_is(field):
    with pytest.raises(RuntimeError, match=field):
        Settings(**{field: "v0.16"})


def test_version_noemt_de_grenzen(monkeypatch):
    from opi.core import version

    monkeypatch.setattr(version.settings, "ZADCTL_MIN_VERSION", "0.16.0")
    monkeypatch.setattr(version.settings, "ZADCTL_RECOMMENDED_VERSION", "")

    assert version.get_version_info()["clients"] == {"zadctl": {"minimum": "0.16.0", "recommended": None}}


def test_in_de_app_geweigerd_voor_de_authenticatie_en_gedocumenteerd(mock_settings, monkeypatch):
    """Een te oude zadctl krijgt 426, ook zonder sleutel; elke /api/-operatie noemt de 426."""
    from opi import server

    monkeypatch.setattr(server.settings, "ZADCTL_MIN_VERSION", "0.16.0")
    monkeypatch.setattr(server.settings, "ZADCTL_RECOMMENDED_VERSION", "")
    client = TestClient(server.create_app())

    assert client.get("/api/v2/projects", headers={"User-Agent": "zadctl/0.15.0"}).status_code == 426
    assert client.get("/api/v2/projects", headers={"User-Agent": "zadctl/0.16.0"}).status_code == 401

    document = client.get("/openapi.json").json()
    operations = [
        operation
        for path, methods in document["paths"].items()
        if path.startswith("/api/")
        for operation in methods.values()
        if isinstance(operation, dict) and "operationId" in operation
    ]
    assert operations
    assert all("426" in operation["responses"] for operation in operations)
