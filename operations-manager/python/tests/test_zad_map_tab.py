"""Het tabblad Kaart en zijn graafroute: achter ``Action.PROJECT_READ``, per project.

Dezelfde poort als de andere projecttabbladen, en dus dezelfde uitkomst voor wie geen lid
is: een 404 of een 403, nooit een plaat. De graaf komt van een eigen route, zodat het
tabblad zelf geen ontsleutelwerk vraagt.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from opi.core.authz.actions import Action, action_of
from opi.core.leden import LidSoort
from opi.services.project_service import ProjectSummary, ProjectUser
from opi.web.authz import enforce_route_action
from opi.web.lotc_switch import PROJECT_TABS, project_tab_url, tab_from_path
from opi.web.router import project_details, project_map_json

MEMBER = "lid@rijksoverheid.nl"
OUTSIDER = "buiten@rijksoverheid.nl"

ROOT = Path(__file__).resolve().parent.parent
TABS = ROOT / "opi" / "templates_lotc" / "bg" / "project-tabs.html.j2"


def _project(name: str = "demo") -> ProjectSummary:
    return ProjectSummary(
        name=name,
        filename=f"{name}.yaml",
        data={"name": name},
        users=[ProjectUser(email=MEMBER, roles=["admin"], type=LidSoort.PERSOON)],
    )


def _request(email: str, project_name: str, path: str, endpoint: Any) -> Any:
    return SimpleNamespace(
        state=SimpleNamespace(user={"email": email}),
        session={},
        headers={},
        method="GET",
        url=SimpleNamespace(path=path),
        scope={"route": SimpleNamespace(endpoint=endpoint)},
        path_params={"project_name": project_name},
        query_params={},
    )


async def _gate(email: str, project_name: str, path: str, endpoint: Any, existing: str = "demo") -> None:
    store = MagicMock()
    store.get.side_effect = lambda name: _project(name) if name == existing else None
    users = MagicMock()
    users.is_platform_admin.return_value = False
    with (
        patch("opi.web.authz.get_project_store", return_value=store),
        patch("opi.web.authz.get_user_service", return_value=users),
    ):
        await enforce_route_action(_request(email, project_name, path, endpoint))


def test_map_is_a_tab_with_its_own_path() -> None:
    assert PROJECT_TABS["kaart"] == {"label": "Kaart", "path": "kaart", "icon": "centralized-network"}
    assert project_tab_url("demo", "kaart") == "/projects/demo/kaart"
    assert tab_from_path("/projects/demo/kaart") == "kaart"


def test_both_routes_carry_the_read_action() -> None:
    assert action_of(project_details) is Action.PROJECT_READ
    assert action_of(project_map_json) is Action.PROJECT_READ


def test_template_carries_the_element() -> None:
    content = TABS.read_text(encoding="utf-8")
    assert "{% elif active_tab == 'kaart' %}" in content
    assert '<zad-map id="zad-map" src="/projects/{{ project.name }}/kaart.json">' in content
    # De keuzelijst zet de deployment in de querystring van de graafroute.
    assert "kaart.json' + (this.value ? '?deployment=' + encodeURIComponent(this.value)" in content


def test_the_route_reads_the_deployment_from_the_query() -> None:
    """FastAPI negeert een onbekende queryparameter stil; dit toetst dat hij bekend is."""
    from opi.web.router import web_router

    (route,) = [r for r in web_router.routes if getattr(r, "path", "") == "/projects/{project_name}/kaart.json"]
    assert [param.name for param in route.dependant.query_params] == ["deployment"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "endpoint"),
    [("/projects/demo/kaart", project_details), ("/projects/demo/kaart.json", project_map_json)],
)
async def test_member_may_read_the_map(path: str, endpoint: Any) -> None:
    await _gate(MEMBER, "demo", path, endpoint)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "endpoint"),
    [("/projects/demo/kaart", project_details), ("/projects/demo/kaart.json", project_map_json)],
)
async def test_non_member_is_denied(path: str, endpoint: Any) -> None:
    with pytest.raises(HTTPException) as error:
        await _gate(OUTSIDER, "demo", path, endpoint)
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_unknown_project_gives_404() -> None:
    with pytest.raises(HTTPException) as error:
        await _gate(MEMBER, "bestaat-niet", "/projects/bestaat-niet/kaart.json", project_map_json)
    assert error.value.status_code == 404
