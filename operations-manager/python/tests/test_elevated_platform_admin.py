"""A platform admin is an ordinary user until a fresh step-up opens their rights.

One state on both paths: ``step_up_at`` in the portal session, ``api_step_ups`` for a
bearer token. The window itself is tested in ``test_step_up_until.py``.
"""

from __future__ import annotations

from contextvars import Context
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from opi.api import step_up_router as step_up_router_module
from opi.api import user_token_auth
from opi.api.otp_step_up import verify_api_step_up
from opi.core.auth_decorators import require_platform_admin
from opi.core.step_up import STEP_UP_WINDOW, StepUpAction, is_elevated_admin, step_up_fresh
from opi.services import project_authorization
from opi.services.api_step_up_service import ApiStepUpService, personal_token_credential, sso_session_credential
from opi.services.user_otp_service import OtpVerification
from opi.services.user_service import UserService
from opi.utils.url_paths import local_redirect_path
from opi.web import menu as menu_module
from opi.web import router_step_up
from opi.web.menu import get_menu_items

if TYPE_CHECKING:
    from collections.abc import Iterator

ADMIN = "beheer@rijksoverheid.nl"
#: conftest patches ``is_fresh`` for every test; this is the method itself.
_REAL_IS_FRESH = ApiStepUpService.is_fresh
USER = {"email": ADMIN, "sub": "sub-1"}


@pytest.fixture
def users() -> UserService:
    service = UserService()
    service.add_platform_admins([ADMIN])
    return service


@pytest.fixture
def elevated() -> Iterator[None]:
    token = step_up_fresh.set(True)
    yield
    step_up_fresh.reset(token)


@pytest.fixture
def not_elevated() -> Iterator[None]:
    token = step_up_fresh.set(False)
    yield
    step_up_fresh.reset(token)


def _portal_request(*, method: str = "GET", headers: dict[str, str] | None = None) -> Any:
    return SimpleNamespace(
        method=method,
        headers=headers or {},
        state=SimpleNamespace(user=USER),
        url=SimpleNamespace(path="/admin/users", query="q=1"),
    )


def _require_admin(request: Any, users: UserService) -> dict[str, Any]:
    with patch("opi.services.user_service.get_user_service", return_value=users):
        return require_platform_admin(request)


@pytest.mark.usefixtures("elevated")
def test_only_a_platform_admin_is_elevated(users: UserService) -> None:
    assert is_elevated_admin(users, ADMIN) is True
    assert is_elevated_admin(users, "iemand@rijksoverheid.nl") is False


@pytest.mark.usefixtures("not_elevated")
def test_without_a_step_up_a_platform_admin_is_not_elevated(users: UserService) -> None:
    assert users.is_platform_admin(ADMIN) is True
    assert is_elevated_admin(users, ADMIN) is False


def test_the_state_is_closed_by_default() -> None:
    assert Context().run(step_up_fresh.get) is False


@pytest.mark.usefixtures("elevated")
def test_require_platform_admin_passes_with_a_step_up(users: UserService) -> None:
    assert _require_admin(_portal_request(), users) == USER


@pytest.mark.usefixtures("not_elevated")
def test_a_page_redirects_to_the_activation_and_comes_back(users: UserService) -> None:
    with pytest.raises(HTTPException) as refused:
        _require_admin(_portal_request(), users)

    assert refused.value.status_code == 303
    assert refused.value.headers == {"Location": "/dashboard?verhoog=%2Fadmin%2Fusers%3Fq%3D1"}


@pytest.mark.usefixtures("not_elevated")
def test_an_htmx_request_redirects_the_whole_page(users: UserService) -> None:
    request = _portal_request(
        method="POST", headers={"HX-Request": "true", "HX-Current-URL": "https://zad.test/admin/approvals?x=1"}
    )

    with pytest.raises(HTTPException) as refused:
        _require_admin(request, users)

    assert refused.value.status_code == 403
    assert refused.value.headers == {"HX-Redirect": "/dashboard?verhoog=%2Fadmin%2Fapprovals"}


@pytest.mark.usefixtures("not_elevated")
def test_a_plain_post_is_refused_without_a_redirect(users: UserService) -> None:
    with pytest.raises(HTTPException) as refused:
        _require_admin(_portal_request(method="POST"), users)

    assert refused.value.status_code == 403
    assert refused.value.headers is None


@pytest.mark.usefixtures("elevated")
def test_a_non_admin_gets_no_activation_route(users: UserService) -> None:
    request = _portal_request()
    request.state.user = {"email": "iemand@rijksoverheid.nl"}

    with pytest.raises(HTTPException) as refused:
        _require_admin(request, users)

    assert refused.value.status_code == 403
    assert refused.value.headers is None


def _project_users(*_: Any) -> list[Any]:
    return [SimpleNamespace(email="lid@rijksoverheid.nl", roles=["viewer"])]


@pytest.mark.usefixtures("not_elevated")
def test_without_a_step_up_an_admin_only_reaches_their_own_projects(users: UserService) -> None:
    with (
        patch.object(project_authorization, "get_user_service", return_value=users),
        patch.object(project_authorization, "get_project_users", _project_users),
    ):
        assert project_authorization.is_user_authorized_for_project("demo", ADMIN) is False
        assert project_authorization.get_user_roles_for_project("demo", ADMIN) == []


@pytest.mark.usefixtures("elevated")
def test_with_a_step_up_an_admin_reaches_every_project(users: UserService) -> None:
    with (
        patch.object(project_authorization, "get_user_service", return_value=users),
        patch.object(project_authorization, "get_project_users", _project_users),
    ):
        assert project_authorization.is_user_authorized_for_project("demo", ADMIN) is True
        assert project_authorization.get_user_roles_for_project("demo", ADMIN) == ["admin", "ops"]


def _menu_links(users: UserService) -> set[str]:
    with patch.object(menu_module, "get_user_service", return_value=users):
        return {item["link"] for item in get_menu_items(USER)}


@pytest.mark.usefixtures("not_elevated")
def test_the_sidebar_hides_admin_items_without_a_step_up(users: UserService) -> None:
    assert "/admin/users" not in _menu_links(users)


@pytest.mark.usefixtures("elevated")
def test_the_sidebar_shows_admin_items_with_a_step_up(users: UserService) -> None:
    assert "/admin/users" in _menu_links(users)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/admin/users?x=1", "/admin/users?x=1"),
        ("//evil.example", "/"),
        ("/\\evil.example", "/"),
        ("https://evil.example", "/"),
        ("", "/"),
    ],
)
def test_the_return_path_stays_on_this_site(value: str, expected: str) -> None:
    assert local_redirect_path(value) == expected


def _api_request(*, fresh: bool) -> Any:
    state = SimpleNamespace(user={"email": ADMIN}, step_up_fresh=fresh, step_up_credential="sid:sid-1")
    return SimpleNamespace(headers={}, state=state, url=SimpleNamespace(path="/api/v2/admin/x"))


async def test_a_fresh_api_state_needs_no_code() -> None:
    with patch("opi.api.otp_step_up.UserOtpService") as otp:
        result = await verify_api_step_up(
            _api_request(fresh=True), project_name="demo", actie=StepUpAction.TOKEN_ISSUE, doelwit="x"
        )

    assert result is None
    otp.assert_not_called()


async def test_without_the_api_state_a_code_is_still_asked() -> None:
    result = await verify_api_step_up(
        _api_request(fresh=False), project_name="demo", actie=StepUpAction.TOKEN_ISSUE, doelwit="x"
    )

    assert result is not None
    assert result.cause.value == "otp-missing"


async def _admin_route(*, request: Any, **_: Any) -> str:
    return "opened"


def _bearer(*, sid: object, fresh: bool, users: UserService) -> Any:
    claims = {"email": ADMIN, "email_verified": True, "sub": "sub-1", "sid": sid}
    service = MagicMock(is_fresh=AsyncMock(return_value=fresh))
    users.add_allowed_emails([ADMIN])
    return (
        patch.object(user_token_auth, "extract_bearer_token", return_value="token"),
        patch.object(user_token_auth, "verify_user_token", AsyncMock(return_value=claims)),
        patch.object(user_token_auth, "get_user_service", return_value=users),
        patch.object(user_token_auth, "ApiStepUpService", return_value=service),
    )


def _bearer_request() -> Any:
    return SimpleNamespace(headers={}, state=SimpleNamespace(), url=SimpleNamespace(path="/api/v2/admin/x"))


async def test_an_admin_read_needs_the_step_up_state(users: UserService) -> None:
    route = user_token_auth.validate_platform_admin_token(_admin_route)
    a, b, c, d = _bearer(sid="sid-1", fresh=False, users=users)
    with a, b, c, d:
        response = await route(request=_bearer_request())

    assert response.status_code == 403
    assert b"urn:zad:step-up-required" in response.body


async def test_an_admin_read_opens_with_the_step_up_state(users: UserService) -> None:
    route = user_token_auth.validate_platform_admin_token(_admin_route)
    a, b, c, d = _bearer(sid="sid-1", fresh=True, users=users)
    with a, b, c, d:
        assert await route(request=_bearer_request()) == "opened"


async def test_a_token_without_sid_is_never_fresh(users: UserService) -> None:
    request = _bearer_request()
    a, b, c, d = _bearer(sid=None, fresh=True, users=users)
    with a, b, c, d:
        await user_token_auth._authenticate_bearer(request, "route")

    assert request.state.step_up_fresh is False
    assert step_up_fresh.get() is False


async def test_activating_records_the_window_for_this_sso_session() -> None:
    valid_until = datetime.now(UTC) + STEP_UP_WINDOW
    service = MagicMock(record=AsyncMock(return_value=valid_until))
    with (
        patch.object(step_up_router_module, "verify_step_up_code", AsyncMock(return_value=None)),
        patch.object(step_up_router_module, "ApiStepUpService", return_value=service),
        patch.object(step_up_router_module, "record_step_up_attempt", AsyncMock()),
        patch.object(step_up_router_module, "client_ip", return_value="10.0.0.1"),
    ):
        response = await step_up_router_module._activate(_api_request(fresh=False), "123456")

    assert response.status_code == 200
    service.record.assert_awaited_once_with("sid:sid-1", ADMIN)


async def test_activating_without_sid_is_refused() -> None:
    request = _api_request(fresh=False)
    request.state.step_up_credential = None

    with pytest.raises(HTTPException) as refused:
        await step_up_router_module._activate(request, "123456")

    assert refused.value.status_code == 400


class _Session:
    """Just enough of an AsyncSession for one row."""

    def __init__(self, row: Any) -> None:
        self.row = row

    async def get(self, *_: Any) -> Any:
        return self.row

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None


@pytest.mark.parametrize(
    ("email", "age", "expected"),
    [
        (ADMIN, timedelta(0), True),
        (ADMIN, STEP_UP_WINDOW + timedelta(seconds=1), False),
        ("ander@rijksoverheid.nl", timedelta(0), False),
    ],
    ids=["fresh", "expired", "another-user"],
)
async def test_the_api_state_holds_for_this_user_within_the_window(email: str, age: timedelta, expected: bool) -> None:
    row = SimpleNamespace(email=email, stepped_up_at=datetime.now(UTC) - age)
    with patch("opi.services.api_step_up_service.session_scope", return_value=_Session(row)):
        assert await _REAL_IS_FRESH(ApiStepUpService(), "sid:sid-1", ADMIN) is expected


def _elevate_request(code: str, terug: str) -> Any:
    request = MagicMock()
    request.form = AsyncMock(return_value={"otp": code, "terug": terug})
    request.session = {}
    request.state.user = USER
    return request


async def test_a_valid_code_activates_and_returns_to_the_page() -> None:
    request = _elevate_request("123456", "/admin/users")
    with (
        patch.object(router_step_up, "UserOtpService") as otp,
        patch.object(router_step_up, "record_step_up_attempt", AsyncMock()),
        patch.object(router_step_up, "client_ip", return_value="10.0.0.1"),
    ):
        otp.return_value.verify = AsyncMock(return_value=OtpVerification.OK)
        response = await router_step_up.elevate.__wrapped__(request)

    assert response.headers["HX-Redirect"] == "/admin/users"
    assert "step_up_at" in request.session


async def test_a_wrong_code_activates_nothing() -> None:
    request = _elevate_request("000000", "/admin/users")
    with (
        patch.object(router_step_up, "UserOtpService") as otp,
        patch.object(router_step_up, "record_step_up_attempt", AsyncMock()),
        patch.object(router_step_up, "client_ip", return_value="10.0.0.1"),
        patch.object(router_step_up, "_elevate_content", AsyncMock(return_value="page")) as page,
    ):
        otp.return_value.verify = AsyncMock(return_value=OtpVerification.INVALID)
        response = await router_step_up.elevate.__wrapped__(request)

    assert response == "page"
    assert "step_up_at" not in request.session
    page.assert_awaited_once()


async def test_ending_clears_the_state() -> None:
    request = MagicMock()
    request.session = {"step_up_at": datetime.now(UTC).isoformat()}
    request.state.user = USER

    response = await router_step_up.end_elevation.__wrapped__(request)

    assert response.headers["HX-Redirect"] == "/dashboard"
    assert request.session == {}


def test_credentials_of_the_two_kinds_never_collide() -> None:
    assert sso_session_credential("abc") == "sid:abc"
    assert personal_token_credential("zad_geheim").startswith("token:")
    assert "zad_geheim" not in personal_token_credential("zad_geheim")
