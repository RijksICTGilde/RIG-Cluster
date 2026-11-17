"""The outage mode of SSO Rijk: the admin page, its guards, and the reconcile.

Three things that can break independently:

- the page and the switch let only a platform administrator with a fresh step-up through,
  and set only the two flows the blueprint creates;
- the page reads the state out of Keycloak, including a state set outside ZAD;
- the reconcile leaves the binding of the platform realm alone, while a project realm
  still gets its browser flow back.

The authenticator itself is covered by the Java tests of
``images/keycloak-zad/extensions/custom-mapper/``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from keycloak.exceptions import KeycloakError
from opi.connectors.keycloak import (
    DEFAULT_BROWSER_FLOW,
    SSO_OUTAGE_BROWSER_FLOW,
    SSO_REDIRECT_BROWSER_FLOW,
)
from opi.core.authz.actions import audit_fields_of
from opi.services.step_up import STEP_UP_SESSION_KEY, StepUpAction
from opi.web import router_platform_admin

ADMIN = "beheerder@rijksoverheid.nl"
DEVELOPER = "ontwikkelaar@rijksoverheid.nl"


class _Form(dict):
    """What ``request.form()`` returns: a mapping with ``getlist``."""

    def getlist(self, name: str) -> list[str]:
        return [self[name]] if name in self else []


def _request(
    *,
    email: str | None = ADMIN,
    fresh_step_up: bool = True,
    wanted: str = "aan",
    path: str = "/admin/platform",
) -> Any:
    session = {STEP_UP_SESSION_KEY: datetime.now(UTC).isoformat()} if fresh_step_up else {}
    request = SimpleNamespace(
        state=SimpleNamespace(user={"email": email} if email else None),
        session=session,
        method="POST",
        url=SimpleNamespace(path=path),
        headers={},
        query_params={},
    )
    request.form = AsyncMock(return_value=_Form({"storingsstand": wanted, "csrf_token": "t"}))
    return request


def _user_service(admins: set[str]) -> Any:
    service = MagicMock()
    service.is_platform_admin.side_effect = lambda email: email in admins
    return service


def _keycloak(browser_flow: str | None) -> Any:
    """A connector that returns a realm with this ``browserFlow``."""
    keycloak = MagicMock()
    keycloak.get_realm = AsyncMock(return_value={"browserFlow": browser_flow} if browser_flow else {})
    keycloak.ensure_browser_flow = AsyncMock(return_value=True)
    return keycloak


# --- the guard on the page and on the switch ---------------------------------------


@pytest.mark.parametrize("route", ["platform_actions", "set_sso_outage"])
async def test_a_non_admin_does_not_get_in(route: str) -> None:
    keycloak = _keycloak(SSO_REDIRECT_BROWSER_FLOW)
    with (
        patch("opi.services.user_service.get_user_service", return_value=_user_service(set())),
        patch("opi.services.sso_outage.create_keycloak_connector", AsyncMock(return_value=keycloak)),
        pytest.raises(HTTPException) as error,
    ):
        await getattr(router_platform_admin, route)(_request(email=DEVELOPER))

    assert error.value.status_code == 403
    keycloak.ensure_browser_flow.assert_not_awaited()


@pytest.mark.parametrize("route", ["platform_actions", "set_sso_outage"])
async def test_without_a_session_nobody_gets_in(route: str) -> None:
    keycloak = _keycloak(SSO_REDIRECT_BROWSER_FLOW)
    with (
        patch("opi.services.user_service.get_user_service", return_value=_user_service(set())),
        patch("opi.services.sso_outage.create_keycloak_connector", AsyncMock(return_value=keycloak)),
        pytest.raises(HTTPException) as error,
    ):
        await getattr(router_platform_admin, route)(_request(email=None))

    assert error.value.status_code == 401
    keycloak.ensure_browser_flow.assert_not_awaited()


async def test_without_a_fresh_step_up_nothing_is_switched() -> None:
    keycloak = _keycloak(SSO_REDIRECT_BROWSER_FLOW)
    with (
        patch("opi.services.user_service.get_user_service", return_value=_user_service({ADMIN})),
        patch("opi.services.sso_outage.create_keycloak_connector", AsyncMock(return_value=keycloak)),
        pytest.raises(HTTPException) as error,
    ):
        await router_platform_admin.set_sso_outage(_request(fresh_step_up=False))

    assert error.value.status_code == 403
    assert StepUpAction.SSO_OUTAGE_SET.phrase in str(error.value.detail)
    keycloak.ensure_browser_flow.assert_not_awaited()


# --- switching on and off -----------------------------------------------------------


@pytest.mark.parametrize(
    ("wanted", "flow"),
    [("aan", SSO_OUTAGE_BROWSER_FLOW), ("uit", SSO_REDIRECT_BROWSER_FLOW)],
)
async def test_switching_calls_the_connector_with_the_right_flow(wanted: str, flow: str) -> None:
    keycloak = _keycloak(SSO_REDIRECT_BROWSER_FLOW)
    with (
        patch("opi.services.user_service.get_user_service", return_value=_user_service({ADMIN})),
        patch("opi.services.sso_outage.create_keycloak_connector", AsyncMock(return_value=keycloak)),
    ):
        answer = await router_platform_admin.set_sso_outage(_request(wanted=wanted))

    assert answer.status_code == 303
    keycloak.ensure_browser_flow.assert_awaited_once()
    assert keycloak.ensure_browser_flow.await_args.args[1] == flow


async def test_anything_but_those_two_words_sets_nothing() -> None:
    """A flow alias never comes out of a request."""
    keycloak = _keycloak(SSO_REDIRECT_BROWSER_FLOW)
    with (
        patch("opi.services.user_service.get_user_service", return_value=_user_service({ADMIN})),
        patch("opi.services.sso_outage.create_keycloak_connector", AsyncMock(return_value=keycloak)),
        pytest.raises(HTTPException) as error,
    ):
        await router_platform_admin.set_sso_outage(_request(wanted="Een Eigen Flow"))

    assert error.value.status_code == 400
    keycloak.ensure_browser_flow.assert_not_awaited()


async def test_every_change_names_its_direction_in_the_audit_row() -> None:
    """Without this field the row only says that something happened, not which way."""
    assert audit_fields_of(router_platform_admin.set_sso_outage) == ("storingsstand",)


async def test_a_failing_keycloak_on_the_switch_is_a_503_and_nothing_else() -> None:
    """Keycloak unreachable may not surface as an unhandled 500 or a silent no-op."""
    keycloak = _keycloak(SSO_REDIRECT_BROWSER_FLOW)
    keycloak.ensure_browser_flow = AsyncMock(side_effect=KeycloakError("Keycloak is weg"))
    with (
        patch("opi.services.user_service.get_user_service", return_value=_user_service({ADMIN})),
        patch("opi.services.sso_outage.create_keycloak_connector", AsyncMock(return_value=keycloak)),
        pytest.raises(HTTPException) as error,
    ):
        await router_platform_admin.set_sso_outage(_request())

    assert error.value.status_code == 503


async def test_a_failing_keycloak_on_the_page_is_a_503_and_not_a_crash() -> None:
    keycloak = _keycloak(SSO_REDIRECT_BROWSER_FLOW)
    keycloak.get_realm = AsyncMock(side_effect=KeycloakError("Keycloak is weg"))
    with (
        patch("opi.services.user_service.get_user_service", return_value=_user_service({ADMIN})),
        patch("opi.services.sso_outage.create_keycloak_connector", AsyncMock(return_value=keycloak)),
        pytest.raises(HTTPException) as error,
    ):
        await router_platform_admin.platform_actions(_request(path="/admin/platform"))

    assert error.value.status_code == 503


# --- the page reads the state out of Keycloak --------------------------------------


@pytest.mark.parametrize(
    ("flow", "active", "recognised"),
    [
        (SSO_REDIRECT_BROWSER_FLOW, False, True),
        (SSO_OUTAGE_BROWSER_FLOW, True, True),
        ("Een eigen flow", False, False),
        (None, False, False),
    ],
)
async def test_the_page_shows_the_state_from_keycloak(flow: str | None, active: bool, recognised: bool) -> None:
    """Including a state set outside ZAD: OPI keeps no copy."""
    keycloak = _keycloak(flow)
    with (
        patch("opi.services.user_service.get_user_service", return_value=_user_service({ADMIN})),
        patch("opi.services.sso_outage.create_keycloak_connector", AsyncMock(return_value=keycloak)),
        patch("opi.http.render.lotc_switch.render", return_value="gerenderd") as render,
        patch("opi.http.render.lotc_switch.build_lotc_admin", return_value={}),
        patch("opi.web.router_platform_admin.get_menu_items", return_value=[]),
        patch("opi.web.router_platform_admin.ensure_csrf_token", return_value="t"),
    ):
        answer = await router_platform_admin.platform_actions(_request(path="/admin/platform"))

    assert answer == "gerenderd"
    status = render.call_args.kwargs["context"]["sso_status"]
    assert status.browser_flow == flow
    assert status.outage_active is active
    assert status.recognised is recognised


# --- the reconcile leaves the binding alone ----------------------------------------


def _handler(browser_flow: str) -> tuple[Any, Any]:
    from opi.manager.bootstrap.keycloak_yaml_handler import KeycloakYamlHandler

    keycloak = MagicMock()
    keycloak.get_realm = AsyncMock(return_value={"browserFlow": browser_flow})
    keycloak.configure_sso_redirect_flow = AsyncMock()
    return KeycloakYamlHandler(keycloak), keycloak


@pytest.mark.parametrize(
    ("standing", "binds"),
    [
        (DEFAULT_BROWSER_FLOW, True),
        (SSO_REDIRECT_BROWSER_FLOW, False),
        (SSO_OUTAGE_BROWSER_FLOW, False),
    ],
)
async def test_the_platform_blueprint_binds_only_from_keycloaks_own_flow(standing: str, binds: bool) -> None:
    handler, _ = _handler(standing)
    item = {"alias": SSO_REDIRECT_BROWSER_FLOW, "setAsBrowserFlow": True, "bindOnlyIfDefault": True}

    assert await handler._may_bind_browser_flow("rig-platform", item) is binds


async def test_a_project_realm_still_gets_its_browser_flow_back() -> None:
    """Without the flag every reconcile binds, the way it always did."""
    handler, _ = _handler(SSO_OUTAGE_BROWSER_FLOW)

    assert await handler._may_bind_browser_flow("rig-demo", {"setAsBrowserFlow": True}) is True


async def test_a_full_run_of_the_blueprint_leaves_the_outage_mode_standing() -> None:
    """Over the real blueprint, so the flag is measured where it has to stand."""
    from pathlib import Path

    from opi.manager.bootstrap import keycloak_yaml_handler as handler_module

    blueprint = Path(handler_module.__file__).parent.parent.parent / "configs" / "keycloak" / "bootstrap.yaml"
    handler, keycloak = _handler(SSO_OUTAGE_BROWSER_FLOW)
    keycloak.ensure_magic_link_browser_flow = AsyncMock(return_value=True)

    await handler.ensure_authentication_flows(blueprint, {"realm_name": "rig-platform"})

    keycloak.configure_sso_redirect_flow.assert_awaited_once()
    assert keycloak.configure_sso_redirect_flow.await_args.kwargs["bind_browser_flow"] is False
    keycloak.ensure_magic_link_browser_flow.assert_awaited_once_with("rig-platform", SSO_OUTAGE_BROWSER_FLOW)


@pytest.mark.parametrize("blueprint", ["bootstrap.yaml", "bootstrap-sandbox.yaml", "bootstrap-local.yaml"])
def test_the_platform_realm_points_at_the_mail_theme_that_holds_the_magic_link(blueprint: str) -> None:
    """Without this field no magic-link mail goes out at all.

    ``EmailTemplateProvider`` looks for ``zad-magic-link.ftl`` in the EMAIL theme of the
    realm. A realm that names no mail theme keeps Keycloak's own, where that template does
    not exist, and sending throws instead of mailing. So the theme on the realm and the
    template in the image are one claim, and this measures both halves of it.
    """
    from pathlib import Path

    from opi.manager.bootstrap import keycloak_yaml_handler as handler_module
    from ruamel.yaml import YAML

    opi_root = Path(handler_module.__file__).parents[2]
    config = YAML().load((opi_root / "configs" / "keycloak" / blueprint).read_text())
    theme = config["realms"][0]["emailTheme"]
    assert theme == "nl-design-system"

    mail_theme = opi_root.parents[2] / "images" / "keycloak-zad" / "theme" / theme / "email"
    assert (mail_theme / "html" / "zad-magic-link.ftl").is_file()
    assert (mail_theme / "text" / "zad-magic-link.ftl").is_file()
    assert "zadMagicLinkSubject" in (mail_theme / "messages" / "messages_nl.properties").read_text()
