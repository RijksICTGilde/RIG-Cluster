"""De staging-stand van de Keycloak-bootstrap: upstream-IdP zoals de sandbox, maar geen lokale gebruiker.

Staging is een pentestomgeving. De sandbox-stand maakt in het OPI-realm een ``admin`` met een
bekend wachtwoord aan; die mag op staging niet bestaan.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.bootstrap.keycloak_setup import KeycloakSetup


async def _users_created(bootstrap_config: str) -> list[str]:
    """De gebruikersnamen die de bootstrap in het OPI-realm aanmaakt."""
    handler = MagicMock()
    handler.execute_config = AsyncMock()
    setup = KeycloakSetup()
    setup.keycloak = MagicMock()
    setup.keycloak.create_user = AsyncMock()
    with (
        patch("opi.bootstrap.keycloak_setup.KeycloakYamlHandler", return_value=handler),
        patch("opi.bootstrap.keycloak_setup.settings.KEYCLOAK_BOOTSTRAP_CONFIG", bootstrap_config),
        patch("opi.bootstrap.keycloak_setup.settings.OWN_DOMAIN", "zad.staging.rijksapp.dev"),
    ):
        await setup.setup_operations_realm()
    return [aanroep.kwargs["username"] for aanroep in setup.keycloak.create_user.await_args_list]


@pytest.mark.asyncio
async def test_staging_creates_no_local_user():
    assert await _users_created("staging") == []


@pytest.mark.asyncio
async def test_sandbox_still_creates_its_admin():
    assert await _users_created("sandbox") == ["admin"]
