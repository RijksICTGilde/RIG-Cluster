"""How much the Keycloak wait and the client deletion actually retry, which is less than
they look like.

- ``wait_for_keycloak_availability`` does not retry the ordinary "not ready yet" answer.
  ``connection_works`` swallows every ``KeycloakError`` into a ``False``, the probe then
  raises ``RuntimeError``, and that type is not in the retry list. A 401 on the other hand
  is an ``HTTPStatusError`` and is repeated for six minutes.
- the client deletion in ``delete_resources_for_deployment`` does not retry at all:
  ``delete_deployment_client`` catches its own failures and answers ``False``.

Both are measurements, not wishes, and are pinned so a change to either is a deliberate
one: repairing them changes the startup behaviour and needs its own card.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from keycloak.exceptions import KeycloakError
from opi.core import startup
from opi.manager.keycloak_manager import KeycloakManager


def _unreachable_connector() -> MagicMock:
    """A connector whose availability check answers False, as it does on a KeycloakError."""
    return MagicMock(connection_works=AsyncMock(return_value=False))


async def test_an_unavailable_keycloak_is_not_retried_at_all():
    attempts = 0

    async def count_and_fail(**_: Any) -> MagicMock:
        nonlocal attempts
        attempts += 1
        return _unreachable_connector()

    with (
        patch.object(startup, "create_keycloak_connector", new=count_and_fail),
        patch("asyncio.sleep", new=AsyncMock()),
        pytest.raises(RuntimeError),
    ):
        await startup.wait_for_keycloak_availability()

    assert attempts == 2, "one client-credentials check and one admin fallback, then it gives up"


async def test_a_401_is_retried_for_minutes():
    delays: list[float] = []

    async def record(seconds: float) -> None:
        delays.append(seconds)

    unauthorized = httpx.HTTPStatusError(
        "unauthorized", request=httpx.Request("GET", "http://keycloak/"), response=httpx.Response(401)
    )
    with (
        patch.object(startup, "create_keycloak_connector", new=AsyncMock(side_effect=unauthorized)),
        patch("asyncio.sleep", new=AsyncMock(side_effect=record)),
        pytest.raises(httpx.HTTPStatusError),
    ):
        await startup.wait_for_keycloak_availability()

    assert sum(delays) == 360.0, "six minutes on a permanent 401"


async def test_a_keycloak_error_on_the_connector_itself_is_retried():
    """Not every unavailability goes through connection_works: a raising connector is retried."""
    delays: list[float] = []

    async def record(seconds: float) -> None:
        delays.append(seconds)

    with (
        patch.object(startup, "create_keycloak_connector", new=AsyncMock(side_effect=KeycloakError("503"))),
        patch("asyncio.sleep", new=AsyncMock(side_effect=record)),
        pytest.raises(KeycloakError),
    ):
        await startup.wait_for_keycloak_availability()

    assert sum(delays) == 360.0


def _manager_deleting_one_sso_component() -> tuple[KeycloakManager, dict[str, Any]]:
    project_manager = MagicMock(
        get_name=AsyncMock(return_value="demo"),
        _get_project_keycloak_config_for_cluster=AsyncMock(return_value=None),
    )
    manager = KeycloakManager(project_manager)
    manager._get_sso_components_for_deployment = AsyncMock(return_value=["component-1"])
    deployment = {"name": "deployment-1", "cluster": "odcn-production"}
    return manager, deployment


async def test_the_client_deletion_is_attempted_exactly_once():
    delete_client = AsyncMock(return_value=False)
    manager, deployment = _manager_deleting_one_sso_component()

    with (
        patch(
            "opi.manager.keycloak_manager.create_keycloak_connector",
            new=AsyncMock(return_value=MagicMock(delete_deployment_client=delete_client)),
        ),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        results = await manager.delete_resources_for_deployment({"name": "demo"}, deployment)

    assert delete_client.await_count == 1, "a False answer is not retried"
    assert results["operations"][0]["status"] == "not_found"
