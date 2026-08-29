"""Tests for the auto-link first-broker-login flow builder.

KeycloakConnector.ensure_auto_link_first_broker_login_flow builds a custom
first-broker-login flow that silently links a brokered SSO identity to a
pre-existing local account (idp-auto-link), replacing the stock flow's
confirm-link + verify-by-email steps.

The fake models Keycloak's execution ordering: siblings are ordered by priority,
a new execution/subflow gets getNextPriority (max existing + 1) unless an explicit
priority is sent in the create body (Keycloak >= 25 honors it). This is what makes
idp-create-user-if-unique (explicit priority) land before the handle-existing
subflow deterministically; without explicit priorities everything ties at 0 and the
order is non-deterministic (the fp-unj "Invalid username or password" bug).
"""

from typing import Any

import pytest
from opi.connectors.keycloak import AUTO_LINK_FIRST_BROKER_LOGIN_FLOW, KeycloakConnector


class FakeAdmin:
    """In-memory model of the KeycloakAdmin authentication-flow API, priority-aware.

    Executions are stored per flow/subflow alias. Subflows are both an execution in
    their parent's list (authenticationFlow=True, keyed by displayName) and a queryable
    alias with their own execution list. Reads return siblings ordered by priority.
    """

    def __init__(self) -> None:
        self.top_level: set[str] = set()
        self.flows: dict[str, list[dict[str, Any]]] = {}
        self.configs: dict[str, dict[str, Any]] = {}
        self._next_id = 0

    def _new_id(self) -> str:
        self._next_id += 1
        return f"exec-{self._next_id}"

    def _next_priority(self, flow_alias: str) -> int:
        kids = self.flows.get(flow_alias, [])
        return max((k["priority"] for k in kids), default=-1) + 1

    def change_current_realm(self, realm_name: str) -> None:
        return None

    def create_authentication_flow(self, payload: dict[str, Any]) -> None:
        self.top_level.add(payload["alias"])
        self.flows.setdefault(payload["alias"], [])

    def get_authentication_flows(self) -> list[dict[str, Any]]:
        return [{"id": f"flow-{alias}", "alias": alias} for alias in sorted(self.top_level)]

    def create_authentication_flow_subflow(
        self, payload: dict[str, Any], flow_alias: str, skip_exists: bool = False
    ) -> None:
        alias = payload["alias"]
        parent = self.flows.setdefault(flow_alias, [])
        if skip_exists and any(e["displayName"] == alias for e in parent):
            return
        parent.append(
            {
                "id": self._new_id(),
                "providerId": None,
                "displayName": alias,
                "requirement": "DISABLED",
                "priority": self._next_priority(flow_alias),
                "authenticationFlow": True,
            }
        )
        self.flows.setdefault(alias, [])

    def create_authentication_flow_execution(self, payload: dict[str, Any], flow_alias: str) -> None:
        provider = payload["provider"]
        priority = payload.get("priority", self._next_priority(flow_alias))
        self.flows.setdefault(flow_alias, []).append(
            {
                "id": self._new_id(),
                "providerId": provider,
                "displayName": provider,
                "requirement": "DISABLED",
                "priority": priority,
                "authenticationFlow": False,
            }
        )

    def get_authentication_flow_executions(self, flow_alias: str) -> list[dict[str, Any]]:
        kids = self.flows.get(flow_alias, [])
        # Keycloak orders siblings by priority; on a tie the subflow sorts first (this reproduces
        # the all-zero-priority bug where handle-existing jumped ahead of idp-create-user-if-unique).
        ordered = sorted(kids, key=lambda e: (e["priority"], 0 if e["authenticationFlow"] else 1))
        for pos, e in enumerate(ordered):
            e["index"] = pos
        return ordered

    def update_authentication_flow_executions(self, payload: dict[str, Any], flow_alias: str) -> None:
        for execution in self.flows.get(flow_alias, []):
            if execution["id"] == payload["id"]:
                execution["requirement"] = payload["requirement"]
                # Keycloak's PUT resets priority to 0 when the payload omits it, so a caller that
                # sends a partial dict silently loses the explicit ordering (the fresh-build bug).
                execution["priority"] = payload.get("priority", 0)
                return

    def create_execution_config(self, payload: dict[str, Any], execution_id: str) -> None:
        config_id = f"config-{execution_id}"
        self.configs[config_id] = payload
        for executions in self.flows.values():
            for execution in executions:
                if execution["id"] == execution_id:
                    execution["authenticationConfig"] = config_id

    def update_authenticator_config(self, payload: dict[str, Any], config_id: str) -> None:
        self.configs[config_id] = payload

    def delete_authentication_flow_execution(self, execution_id: str) -> None:
        for executions in self.flows.values():
            executions[:] = [e for e in executions if e["id"] != execution_id]


def _connector() -> tuple[KeycloakConnector, FakeAdmin]:
    connector = KeycloakConnector.__new__(KeycloakConnector)
    admin = FakeAdmin()
    connector.admin = admin  # type: ignore[assignment]
    return connector, admin


def _providers(admin: FakeAdmin, alias: str) -> list[str | None]:
    return [e["providerId"] for e in admin.get_authentication_flow_executions(alias)]


def _by_provider(admin: FakeAdmin, alias: str, provider: str) -> dict[str, Any]:
    return next(e for e in admin.get_authentication_flow_executions(alias) if e["providerId"] == provider)


def _by_display(admin: FakeAdmin, alias: str, display: str) -> dict[str, Any]:
    return next(e for e in admin.get_authentication_flow_executions(alias) if e["displayName"] == display)


@pytest.mark.asyncio
async def test_builds_automatic_flow_tree() -> None:
    connector, admin = _connector()
    top = AUTO_LINK_FIRST_BROKER_LOGIN_FLOW
    uco = f"{top} user creation or linking"
    hea = f"{top} handle existing account"

    await connector.ensure_auto_link_first_broker_login_flow("realm-a", require_confirmation=False)

    assert top in admin.top_level
    # Review profile is skipped
    assert _by_provider(admin, top, "idp-review-profile")["requirement"] == "DISABLED"
    # User creation or linking subflow is REQUIRED
    assert _by_display(admin, top, uco)["requirement"] == "REQUIRED"
    # create-user-if-unique must run BEFORE handle-existing (its explicit priority guarantees this)
    assert _providers(admin, uco)[0] == "idp-create-user-if-unique"
    assert _by_provider(admin, uco, "idp-create-user-if-unique")["requirement"] == "ALTERNATIVE"
    assert _by_display(admin, uco, hea)["requirement"] == "ALTERNATIVE"
    assert _by_provider(admin, uco, "idp-create-user-if-unique")["priority"] < _by_display(admin, uco, hea)["priority"]
    # Automatic: only idp-auto-link, REQUIRED, no confirmation screen
    assert _providers(admin, hea) == ["idp-auto-link"]
    assert _by_provider(admin, hea, "idp-auto-link")["requirement"] == "REQUIRED"


@pytest.mark.asyncio
async def test_builds_confirm_flow_tree() -> None:
    connector, admin = _connector()
    hea = f"{AUTO_LINK_FIRST_BROKER_LOGIN_FLOW} handle existing account"

    await connector.ensure_auto_link_first_broker_login_flow("realm-a", require_confirmation=True)

    # Confirmation: confirm-link precedes auto-link, both REQUIRED
    assert _providers(admin, hea) == ["idp-confirm-link", "idp-auto-link"]
    assert _by_provider(admin, hea, "idp-confirm-link")["requirement"] == "REQUIRED"
    assert _by_provider(admin, hea, "idp-auto-link")["requirement"] == "REQUIRED"


@pytest.mark.asyncio
async def test_toggle_confirm_to_automatic_removes_confirm_link() -> None:
    connector, admin = _connector()
    hea = f"{AUTO_LINK_FIRST_BROKER_LOGIN_FLOW} handle existing account"

    await connector.ensure_auto_link_first_broker_login_flow("realm-a", require_confirmation=True)
    assert _providers(admin, hea) == ["idp-confirm-link", "idp-auto-link"]

    # Switching the realm back to automatic must drop the stale confirm-link
    await connector.ensure_auto_link_first_broker_login_flow("realm-a", require_confirmation=False)
    assert _providers(admin, hea) == ["idp-auto-link"]


@pytest.mark.asyncio
async def test_idempotent_rerun_does_not_duplicate() -> None:
    connector, admin = _connector()
    top = AUTO_LINK_FIRST_BROKER_LOGIN_FLOW
    uco = f"{top} user creation or linking"
    hea = f"{top} handle existing account"

    await connector.ensure_auto_link_first_broker_login_flow("realm-a", require_confirmation=True)
    await connector.ensure_auto_link_first_broker_login_flow("realm-a", require_confirmation=True)

    # No duplicated executions on the second run
    assert _providers(admin, top).count("idp-review-profile") == 1
    assert [e["displayName"] for e in admin.flows[top]].count(uco) == 1
    assert _providers(admin, uco).count("idp-create-user-if-unique") == 1
    assert [e["displayName"] for e in admin.flows[uco]].count(hea) == 1
    assert _providers(admin, hea) == ["idp-confirm-link", "idp-auto-link"]
    # Order stays correct across reconciles
    assert _providers(admin, uco)[0] == "idp-create-user-if-unique"
