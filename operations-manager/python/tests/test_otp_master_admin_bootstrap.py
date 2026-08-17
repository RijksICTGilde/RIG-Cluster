"""A fresh cluster should get a human master admin that already has OTP.

Keycloak creates the shared ``KEYCLOAK_ADMIN`` itself at first boot from the environment,
so there is no moment where a second factor could be attached to it, and Keycloak 25
imports an OTP credential only at user creation -- retrofitting means delete-and-recreate,
which is not something to do to the break-glass account OPI is authenticated as.

Creating a second admin does work, and it is the better end state anyway: named accounts
instead of one shared login.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.bootstrap.keycloak_setup import KeycloakSetup


def _setup(existing_user: dict | None = None) -> tuple[KeycloakSetup, MagicMock]:
    setup = object.__new__(KeycloakSetup)
    keycloak = MagicMock()
    keycloak.get_user_by_username = AsyncMock(return_value=existing_user)
    keycloak.create_user = AsyncMock(return_value={"id": "user-1"})
    keycloak.assign_realm_roles_to_user = AsyncMock(return_value=True)
    setup.keycloak = keycloak
    return setup, keycloak


def _settings(**overrides):
    values = {
        "KEYCLOAK_OTP_ADMIN_USERNAME": "admin-otp",
        "KEYCLOAK_OTP_ADMIN_PASSWORD": "pw",
        "KEYCLOAK_OTP_ADMIN_TOTP_SECRET": "SEED",
    }
    values.update(overrides)
    return patch.multiple("opi.bootstrap.keycloak_setup.settings", **values)


async def test_the_admin_is_created_with_its_otp_credential() -> None:
    setup, keycloak = _setup()

    with _settings():
        await setup.ensure_otp_master_admin()

    keycloak.create_user.assert_awaited_once()
    kwargs = keycloak.create_user.await_args.kwargs
    assert kwargs["realm_name"] == "master"
    assert kwargs["totp_secret"] == "SEED", "the seed must go in at creation; it cannot be added later"


async def test_it_gets_the_admin_role() -> None:
    setup, keycloak = _setup()

    with _settings():
        await setup.ensure_otp_master_admin()

    keycloak.assign_realm_roles_to_user.assert_awaited_once_with("master", "user-1", ["admin"])


async def test_an_existing_admin_is_left_alone() -> None:
    """Recreating it would rotate the operator's OTP out from under them."""
    setup, keycloak = _setup(existing_user={"id": "already-there"})

    with _settings():
        await setup.ensure_otp_master_admin()

    keycloak.create_user.assert_not_awaited()


@pytest.mark.parametrize("missing", ["KEYCLOAK_OTP_ADMIN_USERNAME", "KEYCLOAK_OTP_ADMIN_TOTP_SECRET"])
async def test_without_a_complete_configuration_nothing_happens(missing: str) -> None:
    """Opt-in: a cluster whose secret predates this keeps booting unchanged."""
    setup, keycloak = _setup()

    with _settings(**{missing: ""}):
        await setup.ensure_otp_master_admin()

    keycloak.create_user.assert_not_awaited()
