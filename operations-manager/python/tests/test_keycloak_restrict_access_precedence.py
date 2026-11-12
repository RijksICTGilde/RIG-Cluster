"""restrict-access gates on one role: an explicit client role replaces the realm role.

The form prefills the realm role with ``allowed-user``. When the realm role won, a client
role entered next to it was silently ignored, and an API-made file with only a client role
got ``allowed-user`` written into it on the next form save, which moved the gate.
"""

from __future__ import annotations

import copy
from typing import Any
from unittest.mock import Mock

from opi.forms.editables.processor import EditableFormProcessor
from opi.forms.visualizers.providers import InviteRealmRoleOptionsProvider
from opi.manager.keycloak_manager import KeycloakManager
from opi.services.catalog.keycloak.visualizers import (
    KEYCLOAK_RESTRICT_ACCESS,
    KEYCLOAK_RESTRICT_ACCESS_CLIENT_ROLE,
    KEYCLOAK_RESTRICT_ACCESS_ROLE,
)

RESTRICT_ACCESS_FIELDS = [KEYCLOAK_RESTRICT_ACCESS, KEYCLOAK_RESTRICT_ACCESS_ROLE, KEYCLOAK_RESTRICT_ACCESS_CLIENT_ROLE]


def _project(restrict_access: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": "demo",
        "services": [{"name": "keycloak", "config": {"template": "sso-support", "restrict-access": restrict_access}}],
    }


def _restrict_access(data: dict[str, Any]) -> dict[str, Any]:
    return data["services"][0]["config"]["restrict-access"]


async def _save(stored: dict[str, Any], submitted: dict[str, Any]) -> dict[str, Any]:
    result, errors = await EditableFormProcessor().process_json_submission(
        submitted, RESTRICT_ACCESS_FIELDS, stored, edit_mode=True
    )
    assert not errors
    return result


def test_client_role_replaces_realm_role_in_the_merged_config() -> None:
    config = KeycloakManager(Mock())._get_keycloak_service_config(
        _project({"enabled": True, "realm-role": "allowed-user", "role": "demo-toegang"})
    )

    assert config["restrict_access"]["role"] == "demo-toegang"
    assert config["restrict_access"]["realm_role"] is None


def test_realm_role_alone_still_gates() -> None:
    config = KeycloakManager(Mock())._get_keycloak_service_config(
        _project({"enabled": True, "realm-role": "allowed-user"})
    )

    assert config["restrict_access"]["realm_role"] == "allowed-user"
    assert config["restrict_access"]["role"] is None


async def test_saving_a_client_role_file_does_not_add_a_realm_role() -> None:
    stored = _project({"enabled": True, "role": "demo-toegang"})
    submitted = _project({"enabled": True, "role": "demo-toegang"})

    result = await _save(stored, submitted)

    assert _restrict_access(result) == {"enabled": True, "role": "demo-toegang"}


async def test_enabling_without_a_role_still_defaults_to_allowed_user() -> None:
    stored = _project({})
    submitted = copy.deepcopy(stored)
    _restrict_access(submitted)["enabled"] = True

    result = await _save(stored, submitted)

    assert _restrict_access(result)["realm-role"] == "allowed-user"


def test_invite_does_not_offer_a_realm_role_that_no_longer_gates() -> None:
    data = _project({"enabled": True, "realm-role": "allowed-user", "role": "demo-toegang"})

    values = [o["value"] for o in InviteRealmRoleOptionsProvider(data).get_options()]

    assert "allowed-user" not in values
