"""The dangerous project actions are addressed by key, never by endpoint.

What matters here is the negative half: a key plus a target that this project does not
have must produce nothing at all. The confirmation renders the endpoint it gets back,
and one of these endpoints deletes an entire project.
"""

import inspect
import re
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from opi.web.project_actions import build_project_action
from opi.web.router import project_action_confirm
from starlette.requests import Request

PROJECT = {
    "name": "demo",
    "display-name": "Demo Project",
    "deployments": [{"name": "prod"}, {"name": "acc"}],
    "components": [{"name": "web"}, {"name": "worker"}],
    "services": [
        {
            "attachments": {
                "data": [
                    {"id": "keystore", "filename": "keystore.p12", "content": "x"},
                    {"id": "unused", "filename": "unused.pem", "content": "y"},
                ]
            }
        }
    ],
}


@pytest.mark.parametrize(
    ("action_key", "target", "endpoint"),
    [
        ("refresh-project", None, "/projects/demo/refresh"),
        ("delete-project", None, "/projects/delete/demo"),
        ("refresh-deployment", "prod", "/projects/demo/refresh/prod"),
        ("delete-deployment", "acc", "/projects/demo/delete-deployment/acc"),
        ("delete-component", "worker", "/projects/demo/delete-component/worker"),
        ("delete-attachment", "unused", "/projects/demo/attachments/unused/delete"),
    ],
)
def test_every_action_builds_its_own_endpoint(action_key: str, target: str | None, endpoint: str) -> None:
    action = build_project_action("demo", PROJECT, action_key, target)

    assert action is not None
    assert action.endpoint == endpoint
    assert action.key == action_key
    assert action.message


@pytest.mark.parametrize(
    ("action_key", "target"),
    [
        # A target this project does not have.
        ("delete-deployment", "other-project-deployment"),
        ("refresh-deployment", "nope"),
        ("delete-component", "nope"),
        ("delete-attachment", "nope"),
        # A target where none belongs: the project-wide actions take no target, so a
        # target must not be able to steer them.
        ("delete-project", "prod"),
        ("refresh-project", "prod"),
        # A missing target where one is required.
        ("delete-deployment", None),
        ("delete-component", ""),
        # An unknown action.
        ("delete-everything", None),
        ("", None),
    ],
)
def test_unknown_action_or_foreign_target_yields_nothing(action_key: str, target: str | None) -> None:
    assert build_project_action("demo", PROJECT, action_key, target) is None


def test_target_is_url_encoded_into_the_endpoint() -> None:
    """A target is data, not a path: it can never add path segments of its own."""
    project = {"name": "demo", "components": [{"name": "a/../b"}]}

    action = build_project_action("demo", project, "delete-component", "a/../b")

    assert action is not None
    assert action.endpoint == "/projects/demo/delete-component/a%2F..%2Fb"


def test_attachment_in_use_is_refused_up_front() -> None:
    """An attachment a component still couples cannot be deleted; say so in the dialog."""
    project = {
        "name": "demo",
        "components": [
            {
                "name": "web",
                "services": [{"attachments": {"config": [{"reference": "keystore", "provide-as": "file"}]}}],
            }
        ],
        "services": [{"attachments": {"data": [{"id": "keystore", "filename": "keystore.p12"}]}}],
    }

    action = build_project_action("demo", project, "delete-attachment", "keystore")

    assert action is not None
    assert action.blocked_reason is not None
    assert "web" in action.blocked_reason


def test_a_component_in_use_says_what_goes_with_it() -> None:
    """The portal deletes with the confirmation, so the dialog is where the user learns
    which deployments and components change along with it (RC-73)."""
    project = {
        "name": "demo",
        "components": [{"name": "web"}, {"name": "worker", "uses-components": ["web"]}],
        "deployments": [{"name": "prod", "components": [{"reference": "web"}]}],
    }

    action = build_project_action("demo", project, "delete-component", "web")

    assert action is not None
    assert action.blocked_reason is None
    assert "deployment 'prod'" in action.message
    assert "component 'worker'" in action.message


def test_a_component_a_web_address_is_built_around_is_refused_up_front() -> None:
    """That one is refused by the delete guard itself, so offering the button would offer a
    deletion that cannot happen."""
    project = {
        "name": "demo",
        "components": [{"name": "web"}],
        "deployments": [
            {
                "name": "prod",
                "components": [{"reference": "web"}],
                "services": [{"reference": "publish-on-web", "config": {"root-component": "web"}}],
            }
        ],
    }

    action = build_project_action("demo", project, "delete-component", "web")

    assert action is not None
    assert action.blocked_reason is not None
    assert "webadres" in action.blocked_reason


def test_a_free_component_is_confirmed_without_a_list() -> None:
    action = build_project_action("demo", PROJECT, "delete-component", "worker")

    assert action is not None
    assert action.blocked_reason is None
    assert "gebruikt door" not in action.message


def test_free_attachment_has_no_blocked_reason() -> None:
    action = build_project_action("demo", PROJECT, "delete-attachment", "unused")

    assert action is not None
    assert action.blocked_reason is None


def test_message_names_the_project_by_its_display_name() -> None:
    """The user confirms what they recognise, not the internal name."""
    action = build_project_action("demo", PROJECT, "delete-project", None)

    assert action is not None
    assert "Demo Project" in action.message


# ---------------------------------------------------------------------------
# The route that renders the dialog
# ---------------------------------------------------------------------------


CSRF = "csrf-token-value"


def _request() -> Request:
    """A request carrying only what the fragment needs: the CSRF token."""
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""})
    request.state.csrf_token = CSRF
    return request


async def _confirm(action_key: str, target: str | None = None, *, role: str = "admin") -> Any:
    store = MagicMock()
    store.get.return_value = MagicMock(data=PROJECT)
    with (
        patch("opi.web.router.get_current_user", return_value={"email": "boss@example.com"}),
        patch("opi.web.router.is_user_authorized_for_project", return_value=True),
        patch("opi.web.router.get_user_role_for_project", return_value=role),
        patch("opi.web.router.get_project_store", return_value=store),
    ):
        return await project_action_confirm(_request(), "demo", action_key, target)


async def test_the_dialog_posts_to_the_endpoint_of_the_action() -> None:
    response = await _confirm("delete-deployment", "acc")
    body = response.body.decode()

    assert response.status_code == 200
    assert "acc" in body
    # Exactly one POST target, and it is the one built server-side.
    assert re.findall(r'hx-post="([^"]*)"', body) == ["/projects/demo/delete-deployment/acc"]


async def test_a_target_this_project_does_not_have_is_a_404() -> None:
    with pytest.raises(HTTPException) as exc:
        await _confirm("delete-deployment", "somebody-elses-deployment")
    assert exc.value.status_code == 404


async def test_an_unknown_action_is_a_404() -> None:
    with pytest.raises(HTTPException) as exc:
        await _confirm("delete-everything")
    assert exc.value.status_code == 404


@pytest.mark.parametrize("role", ["member", "developer", "viewer"])
async def test_a_non_privileged_role_gets_no_dialog(role: str) -> None:
    with pytest.raises(HTTPException) as exc:
        await _confirm("delete-project", role=role)
    assert exc.value.status_code == 403


async def test_a_blocked_action_offers_no_button() -> None:
    """An attachment still in use: the dialog explains, and there is nothing to post."""
    project = {
        "name": "demo",
        "components": [
            {
                "name": "web",
                "services": [{"attachments": {"config": [{"reference": "keystore", "provide-as": "file"}]}}],
            }
        ],
        "services": [{"attachments": {"data": [{"id": "keystore", "filename": "keystore.p12"}]}}],
    }
    store = MagicMock()
    store.get.return_value = MagicMock(data=project)
    with (
        patch("opi.web.router.get_current_user", return_value={"email": "boss@example.com"}),
        patch("opi.web.router.is_user_authorized_for_project", return_value=True),
        patch("opi.web.router.get_user_role_for_project", return_value="admin"),
        patch("opi.web.router.get_project_store", return_value=store),
    ):
        response = await project_action_confirm(_request(), "demo", "delete-attachment", "keystore")

    body = response.body.decode()
    assert "in gebruik" in body
    assert "hx-post" not in body


def test_the_route_takes_no_endpoint_from_the_request() -> None:
    """The signature is the guard: project, key and target, nothing that holds a URL."""
    parameters = set(inspect.signature(project_action_confirm).parameters)
    assert parameters == {"request", "project_name", "action_key", "target"}
