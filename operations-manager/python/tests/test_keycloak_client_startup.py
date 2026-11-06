"""OPI's check on its own OIDC client, on a fresh install and on an existing cluster."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from keycloak.exceptions import KeycloakConnectionError
from opi.bootstrap import keycloak_setup as keycloak_setup_mod
from opi.bootstrap.keycloak_setup import KeycloakSetup
from opi.core import keycloak_client_startup as startup_mod
from opi.core import startup as core_startup
from opi.core.config import settings
from opi.core.keycloak_client_startup import ensure_keycloak_credentials, validate_oidc_credentials
from opi.core.readiness import ReadinessState
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

OPERATIONS_DISCOVERY_URL = "https://keycloak.example/realms/operations-manager/.well-known/openid-configuration"


def _connector(client: dict | None = None, secret: str | None = None) -> MagicMock:
    connector = MagicMock()
    connector.find_client_by_client_id = AsyncMock(return_value=client)
    connector.get_client_secret = AsyncMock(return_value=secret)
    return connector


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "OIDC_CLIENT_ID", "rig-platform-operations-manager")
    monkeypatch.setattr(settings, "OIDC_CLIENT_SECRET", "right")
    monkeypatch.setattr(settings, "OIDC_DISCOVERY_URL", OPERATIONS_DISCOVERY_URL)


def _use_connector(monkeypatch: pytest.MonkeyPatch, connector: MagicMock) -> None:
    monkeypatch.setattr(startup_mod, "create_keycloak_connector", AsyncMock(return_value=connector))


@pytest.mark.usefixtures("configured")
async def test_matching_secret_is_valid_without_a_service_account(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = _connector(client={"id": "internal", "serviceAccountsEnabled": False}, secret="right")
    _use_connector(monkeypatch, connector)

    assert await validate_oidc_credentials() is True

    connector.find_client_by_client_id.assert_awaited_once_with("rig-platform-operations-manager", "operations-manager")
    connector.get_client_secret.assert_awaited_once_with("internal", "operations-manager")


@pytest.mark.usefixtures("configured")
async def test_different_secret_is_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_connector(monkeypatch, _connector(client={"id": "internal"}, secret="other"))

    assert await validate_oidc_credentials() is False


@pytest.mark.usefixtures("configured")
async def test_unknown_client_is_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = _connector(client=None)
    _use_connector(monkeypatch, connector)

    assert await validate_oidc_credentials() is False

    connector.get_client_secret.assert_not_awaited()


async def test_incomplete_settings_never_reach_keycloak(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "OIDC_CLIENT_ID", "rig-platform-operations-manager")
    monkeypatch.setattr(settings, "OIDC_CLIENT_SECRET", None)
    monkeypatch.setattr(settings, "OIDC_DISCOVERY_URL", OPERATIONS_DISCOVERY_URL)
    factory = AsyncMock()
    monkeypatch.setattr(startup_mod, "create_keycloak_connector", factory)

    assert await validate_oidc_credentials() is False

    factory.assert_not_awaited()


@pytest.mark.usefixtures("configured")
async def test_unreachable_keycloak_marks_the_service_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core_startup, "wait_for_keycloak_availability", AsyncMock())
    monkeypatch.setattr(core_startup, "setup_keycloak", AsyncMock(return_value=True))
    monkeypatch.setattr(
        startup_mod, "create_keycloak_connector", AsyncMock(side_effect=KeycloakConnectionError("down"))
    )
    readiness = ReadinessState()

    assert await core_startup._setup_keycloak(readiness, skip_checks=False) is False

    assert readiness.keycloak.error == "down"


async def _update_secret(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, client_info: dict) -> None:
    monkeypatch.setattr(settings, "TEMP_DIR", str(tmp_path))
    monkeypatch.setattr(keycloak_setup_mod, "ManifestGenerator", MagicMock())
    setup = KeycloakSetup()
    setup.kubectl = MagicMock(apply_manifest=AsyncMock())

    assert await setup._update_operations_secret(client_info) is True


async def test_first_start_replaces_what_the_configmap_set(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The overlays set a discovery URL for another realm; only a restart used to pick up the secret."""
    monkeypatch.setattr(settings, "OIDC_CLIENT_ID", "rig-platform-operations-manager")
    monkeypatch.setattr(settings, "OIDC_CLIENT_SECRET", "stale")
    monkeypatch.setattr(
        settings, "OIDC_DISCOVERY_URL", "https://keycloak.example/realms/rig-platform/.well-known/openid-configuration"
    )

    await _update_secret(
        monkeypatch,
        tmp_path,
        {
            "client_id": "rig-platform-operations-manager",
            "client_secret": "right",
            "discovery_url": OPERATIONS_DISCOVERY_URL,
            "base_url": "https://keycloak.example",
            "realm": "operations-manager",
        },
    )

    assert settings.OIDC_CLIENT_SECRET == "right"
    assert settings.OIDC_DISCOVERY_URL == OPERATIONS_DISCOVERY_URL


async def test_fresh_install_passes_the_check_after_setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(settings, "OIDC_CLIENT_ID", "rig-platform-operations-manager")
    monkeypatch.setattr(settings, "OIDC_CLIENT_SECRET", None)
    monkeypatch.setattr(settings, "OIDC_DISCOVERY_URL", None)
    _use_connector(monkeypatch, _connector(client={"id": "internal", "serviceAccountsEnabled": False}, secret="right"))

    await _update_secret(
        monkeypatch,
        tmp_path,
        {
            "client_id": "rig-platform-operations-manager",
            "client_secret": "right",
            "discovery_url": OPERATIONS_DISCOVERY_URL,
            "base_url": "https://keycloak.example",
            "realm": "operations-manager",
        },
    )

    assert await ensure_keycloak_credentials() is True
