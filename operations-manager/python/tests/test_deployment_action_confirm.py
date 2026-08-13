"""A service's deployment action asks in the page's own popup, not in window.confirm().

Two failures this guards:

* The confirmation used to be ``window.confirm()`` inside ``runDeploymentAction``, with
  ``window.alert()`` for the failure -- a browser dialog in the middle of a page that
  has a modal of its own. Every service that hands the page a ``DeploymentAction`` with
  a ``confirm_message`` must get that shared modal, and get it WITHOUT a line of code
  per service. So the guard runs the whole chain for a service the page has never heard
  of, mounted on its definition the way a real service mounts its provider.
* The POST target must never come from the request. The dialog is addressed by an
  action key and re-derives the action from the services the project actually uses, so
  a key nobody offers is a 404 and a query parameter cannot smuggle an endpoint into
  the rendered page. An endpoint in the URL would be an open POST target.
"""

from __future__ import annotations

import inspect
import pathlib
import re
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import opi
import pytest
from fastapi import HTTPException
from opi.core.templates_lotc import templates_lotc as templates
from opi.services.registry import (
    SERVICES,
    collect_deployment_actions,
    deployment_action_key,
    find_deployment_action,
)
from opi.services.services import DeploymentAction
from opi.services.services_enums import ServiceType
from opi.web.router import deployment_action_confirm
from starlette.requests import Request

if TYPE_CHECKING:
    from collections.abc import Iterator

PROJECT = "demo"
DEPLOYMENT = "dep-1"
CSRF = "csrf-token-value"

_TEMPLATE_ROOT = pathlib.Path(opi.__file__).parent / "templates_lotc"
# static/ staat naast opi/, niet erin.
_STATIC_ROOT = pathlib.Path(opi.__file__).parent.parent / "static"

#: The action our stand-in service offers. Nothing in the page or the route knows it.
FAKE_ACTION = DeploymentAction(
    label="Cache legen",
    icon="verwijderen",
    kind="warning",
    endpoint=f"/projects/{PROJECT}/deployments/{DEPLOYMENT}/cache-legen",
    confirm_message="Weet u zeker dat u de cache wilt legen?",
)


@pytest.fixture
def service_with_an_action() -> Iterator[None]:
    """Mount an actions_provider on a service, the way a real service mounts its own.

    Attached to a service that has none today, so the test measures the generic chain
    rather than sleep-mode's or PostgreSQL's own behaviour.
    """
    definition = SERVICES[ServiceType.TEMP_STORAGE].definition
    assert definition.actions_provider is None, "pick a service without a provider for this test"
    definition.actions_provider = lambda project_data, deployment_name: [FAKE_ACTION]
    try:
        yield
    finally:
        definition.actions_provider = None


def _request() -> Request:
    """A request carrying only what the fragment needs: the CSRF token."""
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""})
    request.state.csrf_token = CSRF
    return request


def _project_data() -> dict[str, Any]:
    return {"name": PROJECT, "services": ["temp-storage"], "deployments": [{"name": DEPLOYMENT}]}


async def _confirm(action_key: str, *, role: str = "admin") -> Any:
    store = MagicMock()
    store.get.return_value = MagicMock(data=_project_data())
    with (
        patch("opi.web.router.get_current_user", return_value={"email": "boss@example.com"}),
        patch("opi.web.router.is_user_authorized_for_project", return_value=True),
        patch("opi.web.router.get_user_role_for_project", return_value=role),
        patch("opi.web.router.get_project_store", return_value=store),
    ):
        return await deployment_action_confirm(_request(), PROJECT, DEPLOYMENT, action_key)


# ---------------------------------------------------------------------------
# The key: what the dialog is addressed by
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Applicatie wekken", "applicatie-wekken"),
        ("Deployment slapen", "deployment-slapen"),
        ("Databaseconsole", "databaseconsole"),
        ("Job uitvoeren!", "job-uitvoeren"),
        ("  Cache  legen  ", "cache-legen"),
    ],
)
def test_the_key_is_a_url_safe_slug_of_the_label(label: str, expected: str) -> None:
    """It travels in a path segment, so anything that would need escaping is out."""
    action = DeploymentAction(label=label, icon="i", kind="secondary", endpoint="/x")
    key = deployment_action_key(action)
    assert key == expected
    assert re.fullmatch(r"[a-z0-9-]+", key)


def test_the_actions_of_one_deployment_do_not_share_a_key() -> None:
    """Two buttons with one key would let the confirmation post the wrong action."""
    actions = collect_deployment_actions({"name": PROJECT, "services": ["postgresql-database"]}, DEPLOYMENT)
    keys = [deployment_action_key(action) for action in actions]
    assert len(keys) > 1
    assert len(keys) == len(set(keys))


# ---------------------------------------------------------------------------
# Only what a service really offered
# ---------------------------------------------------------------------------


def test_the_key_resolves_to_the_action_the_service_offered(service_with_an_action: None) -> None:
    action = find_deployment_action(_project_data(), DEPLOYMENT, "cache-legen")
    assert action is not None
    assert action.endpoint == FAKE_ACTION.endpoint


def test_an_unknown_key_resolves_to_nothing(service_with_an_action: None) -> None:
    assert find_deployment_action(_project_data(), DEPLOYMENT, "cache-legen-x") is None


def test_a_key_of_a_service_this_project_does_not_use_resolves_to_nothing() -> None:
    """The console button exists, but not for a project without a database."""
    without_database = {"name": PROJECT, "services": ["publish-on-web"], "deployments": [{"name": DEPLOYMENT}]}
    assert find_deployment_action(without_database, DEPLOYMENT, "databaseconsole") is None


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------


async def test_the_dialog_posts_to_the_endpoint_of_the_service(service_with_an_action: None) -> None:
    response = await _confirm("cache-legen")
    body = response.body.decode()

    assert response.status_code == 200
    assert FAKE_ACTION.confirm_message in body
    assert f'hx-post="{FAKE_ACTION.endpoint}"' in body
    assert re.findall(r'hx-post="([^"]*)"', body) == [FAKE_ACTION.endpoint]


async def test_a_key_nobody_offers_is_a_404(service_with_an_action: None) -> None:
    with pytest.raises(HTTPException) as exc:
        await _confirm("iets-anders")
    assert exc.value.status_code == 404


async def test_a_modal_action_has_no_confirmation_of_its_own() -> None:
    """A ``modal_endpoint`` action opens its service's own body; there is nothing to
    post here, so its key must not resolve into a POST button."""
    data = {"name": PROJECT, "services": ["postgresql-database"], "deployments": [{"name": DEPLOYMENT}]}
    store = MagicMock()
    store.get.return_value = MagicMock(data=data)
    with (
        patch("opi.web.router.get_current_user", return_value={"email": "boss@example.com"}),
        patch("opi.web.router.is_user_authorized_for_project", return_value=True),
        patch("opi.web.router.get_user_role_for_project", return_value="admin"),
        patch("opi.web.router.get_project_store", return_value=store),
        pytest.raises(HTTPException) as exc,
    ):
        await deployment_action_confirm(_request(), PROJECT, DEPLOYMENT, "databaseconsole")
    assert exc.value.status_code == 404


@pytest.mark.parametrize("role", ["member", "developer", "viewer"])
async def test_a_non_privileged_role_gets_no_dialog(role: str, service_with_an_action: None) -> None:
    with pytest.raises(HTTPException) as exc:
        await _confirm("cache-legen", role=role)
    assert exc.value.status_code == 403


async def test_the_route_takes_no_endpoint_from_the_request(service_with_an_action: None) -> None:
    """The signature is the guard: project, deployment and key, nothing else. A route
    that accepted an endpoint (query parameter or body) would be an open POST target."""
    parameters = set(inspect.signature(deployment_action_confirm).parameters)
    assert parameters == {"request", "project_name", "deployment_name", "action_key"}


# ---------------------------------------------------------------------------
# The fragment and the page
# ---------------------------------------------------------------------------


def _fragment(action: DeploymentAction) -> str:
    # bg/_action-confirm.html.j2 is wat de route rendert (zie deployment_action_confirm).
    # Ernaast stond project-details/action-confirm.html.j2, de eerste automatische omzetting
    # van het oude sjabloon; die hing aan geen enkele route en is in RC-97 weggehaald.
    template = templates.env.get_template("bg/_action-confirm.html.j2")
    return template.render(request=_request(), action=action, key="fake-key", message=action.confirm_message)


def test_the_csrf_header_is_not_an_attribute_of_a_component_button() -> None:
    """Een componentknop schrijft attribuutwaarden opnieuw uit met dubbele aanhalingstekens,
    en dat verminkt de JSON van hx-headers (dat kostte ons drie keer een 403). De header
    staat daarom op de kale wikkel, waar htmx hem van erft."""
    html = _fragment(FAKE_ACTION)

    assert f'hx-headers=\'{{"X-CSRF-Token": "{CSRF}"}}\'' in html
    for button in re.findall(r"<button[^>]*>", html):
        assert "hx-headers" not in button


def test_the_dialog_swaps_the_running_task_in_and_offers_a_way_out() -> None:
    """The question is replaced by the progress of the task it started.

    These endpoints commit to git and then reprocess, ArgoCD sync included. As an inline
    call the dialog sat unchanged for tens of seconds, which reads as a button that did
    nothing; the endpoints now answer with the shared progress fragment and it swaps in
    here, so there is something to follow.
    """
    html = _fragment(FAKE_ACTION)

    assert 'hx-target="this"' in html
    assert 'hx-swap="innerHTML"' in html
    assert 'data-confirm-action="fake-key"' in html
    assert "closeEditModal()" in html
    assert 'id="confirm-action-error"' in html


def test_sleeping_and_waking_run_as_followable_tasks() -> None:
    """Not an inline call: a task the page can poll, like reprocessing a deployment.

    Pinned on the router because this is the property that was missing -- the endpoints
    used to answer JSON straight from ``flow.sleep`` with the request open the whole time.
    """
    from opi.web import router

    source = inspect.getsource(router.sleep_deployment_web) + inspect.getsource(router.wake_deployment_web)

    assert "create_task_and_render_progress" in source
    assert "await flow.sleep(" not in source
    assert "await flow.wake(" not in source


def test_the_page_no_longer_confirms_in_the_browser_dialog() -> None:
    page = (_TEMPLATE_ROOT / "bg" / "project-tabs.html.j2").read_text(encoding="utf-8")
    section = (_TEMPLATE_ROOT / "bg" / "_deployment-actions.html.j2").read_text(encoding="utf-8")

    assert "runDeploymentAction" not in page
    assert "runDeploymentAction" not in section
    assert "window.confirm(" not in page
    assert "window.alert(" not in page
    # De afhandeling van een mislukte POST staat sinds de omzetting naar de nieuwe
    # vormgeving in /static/js/edit_modal.js, bij de rest van het dialoog-gedrag. Ze hoort
    # niet bij deze pagina maar bij de dialoog: beide vormgevingen openen dezelfde, en een
    # tweede kopie loopt uit de pas. De eis is ongewijzigd - de uitkomst wordt afgehandeld,
    # en niet in een browser-dialoog.
    modal_js = (_STATIC_ROOT / "js" / "edit_modal.js").read_text(encoding="utf-8")
    assert "data-confirm-action" in modal_js, "de uitkomst van de htmx-POST moet afgehandeld worden"
    assert "window.confirm(" not in modal_js or "niet-opgeslagen wijzigingen" in modal_js


def test_the_button_opens_the_confirmation_in_the_shared_modal() -> None:
    """Rendered through the app env, so the component handling of the click is exercised:
    the URL must come out intact.

    De aanhalingstekens om het adres staan als ``&#39;`` in het attribuut. Dat is de
    escaping van het componentensysteem en geen verminking: de browser leest er weer een
    apostrof van. Wat hier telt is dat het ADRES heel blijft.
    """
    template = templates.env.get_template("bg/_deployment-actions.html.j2")
    html = template.render(
        request=_request(),
        project={"name": PROJECT, "deployments": [{"name": DEPLOYMENT}]},
        # De acties horen bij de deployment die de pagina toont; die komt uit het pad
        # (RC-92) en staat als ``deployment_geopend`` in de context.
        deployment_geopend={"name": DEPLOYMENT},
        user_role="admin",
        deployment_service_actions={DEPLOYMENT: [FAKE_ACTION]},
    )

    expected = f"/projects/{PROJECT}/deployments/{DEPLOYMENT}/actions/cache-legen/confirm"
    assert f"openServiceModal(&#39;{expected}&#39;" in html
    assert FAKE_ACTION.endpoint not in html, "the endpoint belongs in the confirmation, not on the page"
