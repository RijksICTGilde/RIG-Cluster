"""Keycloak cleanup on project deletion must not depend on the project file.

A project's realm and its master-realm admin user are named deterministically from the
project and cluster, so they can be removed even when the file no longer carries a
keycloak config entry. Skipping in that case left both behind, and an orphaned admin
account carrying an OTP credential is not something to leave lying around because a
config block went missing. Observed in the RC-22 run: realms and master-realm admin
users survived a project delete.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.manager.delete_project_manager import DeleteProjectManager


def _manager() -> tuple[DeleteProjectManager, MagicMock]:
    mgr = object.__new__(DeleteProjectManager)
    mgr.project_manager = MagicMock()
    keycloak = MagicMock()
    keycloak.delete_realm = AsyncMock(return_value=True)
    keycloak.delete_user_by_username = AsyncMock(return_value=True)
    keycloak.delete_deployment_client = AsyncMock(return_value=True)
    keycloak.realm_exists = AsyncMock(return_value=True)
    return mgr, keycloak


async def _run(keycloak: MagicMock, *, only_if_present: bool) -> dict:
    mgr, _ = _manager()
    # The cleanup also rewrites the project file at the end; an AsyncMock keeps that path
    # awaitable without this test caring what it writes.
    mgr.project_manager = AsyncMock()
    results: dict = {"operations": [], "errors": [], "success": True}
    with (
        patch("opi.connectors.keycloak.create_keycloak_connector", AsyncMock(return_value=keycloak)),
        patch("opi.manager.delete_project_manager.Project", MagicMock()),
    ):
        await mgr._cleanup_project_keycloak_realm(
            project_name="demo",
            cluster="odcn-production",
            kc_config={"realm": "demo-odcn-production", "username": "demo_odcn_production_admin", "host": "https://kc"},
            deletion_results=results,
            only_if_present=only_if_present,
        )
    return results


async def test_the_master_realm_admin_user_is_deleted() -> None:
    """The account carries the shared OTP credential, so leaving it behind matters."""
    _, keycloak = _manager()
    await _run(keycloak, only_if_present=False)

    keycloak.delete_user_by_username.assert_awaited_once()
    realm, username = keycloak.delete_user_by_username.await_args.args[:2]
    assert realm == "master"
    assert username == "demo_odcn_production_admin"


async def test_the_realm_is_deleted() -> None:
    _, keycloak = _manager()
    await _run(keycloak, only_if_present=False)

    keycloak.delete_realm.assert_awaited_once_with("demo-odcn-production")


async def test_nothing_is_touched_when_the_realm_is_absent() -> None:
    """Derived names are a guess for a project that never used Keycloak.

    Without this the delete report would carry three failed operations for such a project,
    burying the failures that do matter.
    """
    _, keycloak = _manager()
    keycloak.realm_exists = AsyncMock(return_value=False)

    results = await _run(keycloak, only_if_present=True)

    keycloak.delete_realm.assert_not_awaited()
    keycloak.delete_user_by_username.assert_not_awaited()
    assert results["operations"] == []


async def test_an_absent_realm_is_still_cleaned_when_the_config_named_it() -> None:
    """Config-driven path keeps its old behaviour: it was told the realm exists."""
    _, keycloak = _manager()
    keycloak.realm_exists = AsyncMock(return_value=False)

    await _run(keycloak, only_if_present=False)

    keycloak.delete_realm.assert_awaited_once()


@pytest.mark.parametrize("failing", ["delete_realm", "delete_user_by_username"])
async def test_one_failing_step_does_not_stop_the_others(failing: str) -> None:
    """Each step has its own guard, so a failed realm delete still removes the account."""
    _, keycloak = _manager()
    getattr(keycloak, failing).side_effect = RuntimeError("keycloak down")

    await _run(keycloak, only_if_present=False)

    keycloak.delete_realm.assert_awaited()
    keycloak.delete_user_by_username.assert_awaited()
