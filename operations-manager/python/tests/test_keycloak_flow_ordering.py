"""The realm's browser flow may only be pointed at a flow that already exists.

Keycloak rejects a ``browserFlow`` naming an unknown alias with a bare
``500 {"errorMessage":"Failed to update realm"}``. ``_ensure_realm_authentication_flow``
used to set the browser flow first and create the flows second, so switching an existing
realm from sso-support to sso-only always failed: "External IDP Redirector" is created by
the very call that ran after it (toets-hn7, 2026-08-05).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from opi.handlers.keycloak_yaml_handler import KeycloakYamlHandler
from opi.manager.keycloak_manager import KeycloakManager


@pytest.fixture
def call_order() -> list[str]:
    return []


@pytest.fixture
def keycloak_connector(call_order: list[str]) -> AsyncMock:
    connector = AsyncMock()
    connector.ensure_browser_flow.side_effect = lambda *_args, **_kwargs: call_order.append("set-browser-flow")
    return connector


async def _run(template: str, connector: AsyncMock, call_order: list[str]) -> None:
    async def record_flows(*_args: Any, **_kwargs: Any) -> None:
        call_order.append("create-flows")

    with (
        patch("opi.manager.keycloak_manager.create_keycloak_connector", return_value=connector),
        patch.object(KeycloakYamlHandler, "ensure_authentication_flows", new=AsyncMock(side_effect=record_flows)),
    ):
        await KeycloakManager(project_manager=AsyncMock())._ensure_realm_authentication_flow(
            realm_name="toets-hn7-odcn-production",
            keycloak_url="https://keycloak.example.nl",
            config={"template": template},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("template", ["sso-only", "sso-support"])
async def test_flows_are_created_before_the_browser_flow_points_at_them(
    template: str, keycloak_connector: AsyncMock, call_order: list[str]
) -> None:
    await _run(template, keycloak_connector, call_order)

    assert call_order == ["create-flows", "set-browser-flow"], (
        f"Browser flow was set before the flows existed for template {template}: {call_order}"
    )


@pytest.mark.asyncio
async def test_sso_only_targets_the_redirector_flow(keycloak_connector: AsyncMock, call_order: list[str]) -> None:
    await _run("sso-only", keycloak_connector, call_order)

    keycloak_connector.ensure_browser_flow.assert_awaited_once_with(
        "toets-hn7-odcn-production", "External IDP Redirector"
    )


@pytest.mark.asyncio
async def test_sso_support_targets_the_builtin_browser_flow(
    keycloak_connector: AsyncMock, call_order: list[str]
) -> None:
    await _run("sso-support", keycloak_connector, call_order)

    keycloak_connector.ensure_browser_flow.assert_awaited_once_with("toets-hn7-odcn-production", "browser")
