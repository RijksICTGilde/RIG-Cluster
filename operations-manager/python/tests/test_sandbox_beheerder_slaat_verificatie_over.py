"""De sandbox-beheerder komt bevestigd binnen (RC-267).

``admin@sandbox.rijksapp.dev`` uit ``opi/configs/projects/operations-manager-local.yaml``
bestaat niet als postbus. Kreeg hij ``VERIFY_EMAIL`` mee, dan was de eerste login op een
verse sandbox alleen te doen door de mail uit de SMTP-sink te vissen.

Dat deze uitzondering de enige blijft, meet
``test_keycloak_master_admins_slaan_verificatie_over.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.bootstrap.keycloak_setup import OPERATIONS_REALM_NAME, KeycloakSetup


async def _run(bootstrap_config: str) -> MagicMock:
    setup = object.__new__(KeycloakSetup)
    keycloak = MagicMock()
    keycloak.create_user = AsyncMock(return_value={"id": "user-1"})
    setup.keycloak = keycloak

    handler = MagicMock()
    handler.execute_config = AsyncMock()

    with (
        patch.multiple("opi.bootstrap.keycloak_setup.settings", KEYCLOAK_BOOTSTRAP_CONFIG=bootstrap_config),
        patch("opi.bootstrap.keycloak_setup.KeycloakYamlHandler", return_value=handler),
    ):
        await setup.setup_operations_realm()

    return keycloak


async def test_de_beheerder_wordt_aangemaakt_zonder_bevestiging() -> None:
    keycloak = await _run("sandbox")

    keycloak.create_user.assert_awaited_once()
    kwargs = keycloak.create_user.await_args.kwargs
    assert kwargs["username"] == "admin"
    assert kwargs["realm_name"] == "operations-manager"
    assert kwargs["skip_email_verification"] is True, (
        "admin@sandbox.rijksapp.dev is geen postbus: bevestigen houdt de beheerder buiten"
    )


async def test_productie_maakt_geen_gebruiker_aan() -> None:
    """``operations-manager.yaml`` heeft geen ``users``, en zo hoort het te blijven."""
    keycloak = await _run("default")

    keycloak.create_user.assert_not_awaited()


async def test_een_gebruiker_zonder_wachtwoord_is_een_fout() -> None:
    setup = object.__new__(KeycloakSetup)
    setup.keycloak = MagicMock(create_user=AsyncMock())

    handler = MagicMock(execute_config=AsyncMock())
    config = {"template": "operations-manager-realm", "variables": {}, "users": [{"username": "admin"}]}

    with (
        patch.multiple("opi.bootstrap.keycloak_setup.settings", KEYCLOAK_BOOTSTRAP_CONFIG="sandbox"),
        patch("opi.bootstrap.keycloak_setup.KeycloakYamlHandler", return_value=handler),
        patch.object(KeycloakSetup, "_extract_keycloak_service_config", return_value=config),
        pytest.raises(ValueError, match="no password credential"),
    ):
        await setup.setup_operations_realm()


def test_de_letterlijke_realm_blijft_gelijk_aan_de_constante() -> None:
    """De aanroep noemt de realm letterlijk, want de guard eist dat.

    Daardoor zou een hernoeming van de constante de gebruiker in een realm zetten die niet
    bestaat, zonder dat iets anders het ziet.
    """
    assert OPERATIONS_REALM_NAME == "operations-manager"
