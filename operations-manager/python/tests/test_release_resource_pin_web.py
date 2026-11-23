"""``release_component_resource_pin``: the portal button "Laat de tuner dit weer regelen" (RC-518).

Reads fresh from git (never the cache), writes only when something was actually released,
and rejects an unknown field or component with a 404 before anything is read.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from opi.services.project_file_handler import ProjectFileHandler
from opi.web.router import release_component_resource_pin

PROJECT = "demo"
COMPONENT = "api"


def _request(htmx: bool = False) -> SimpleNamespace:
    return SimpleNamespace(headers={"HX-Request": "true"} if htmx else {})


def _store_with_component(component: str = COMPONENT) -> MagicMock:
    project = MagicMock()
    project.data = {"name": PROJECT, "components": [{"name": component}]}
    store = MagicMock()
    store.get.return_value = project
    return store


def _project_manager(project_data: dict) -> MagicMock:
    pm = MagicMock()
    pm.get_contents = AsyncMock(return_value=project_data)
    pm.save_and_commit_project = AsyncMock()
    pm.close = AsyncMock()
    return pm


async def test_an_unknown_field_returns_404_without_looking_up_the_project() -> None:
    with (
        patch("opi.web.router.get_current_user", return_value={"email": "x@example.com"}),
        patch("opi.web.router.get_project_store") as store,
        pytest.raises(HTTPException) as exc,
    ):
        await release_component_resource_pin(_request(), PROJECT, COMPONENT, "limits_disk")

    assert exc.value.status_code == 404
    store.assert_not_called()


async def test_an_unknown_component_returns_404() -> None:
    with (
        patch("opi.web.router.get_current_user", return_value={"email": "x@example.com"}),
        patch("opi.web.router.get_project_store", return_value=_store_with_component("other-component")),
        pytest.raises(HTTPException) as exc,
    ):
        await release_component_resource_pin(_request(), PROJECT, COMPONENT, "limits_memory")

    assert exc.value.status_code == 404


async def test_an_unknown_project_returns_404() -> None:
    store = MagicMock()
    store.get.return_value = None
    with (
        patch("opi.web.router.get_current_user", return_value={"email": "x@example.com"}),
        patch("opi.web.router.get_project_store", return_value=store),
        pytest.raises(HTTPException) as exc,
    ):
        await release_component_resource_pin(_request(), PROJECT, COMPONENT, "limits_memory")

    assert exc.value.status_code == 404


async def test_a_pinned_field_is_released_and_committed() -> None:
    project_data = {
        "name": PROJECT,
        "components": [
            {
                "name": COMPONENT,
                "resources": {
                    "requests": {"memory": "64Mi"},
                    "limits": {"memory": "900Mi"},
                    "history": [
                        {"timestamp": "2026-01-01T00:00:00+00:00", "source": "manual", "limits": {"memory": "900Mi"}}
                    ],
                },
            }
        ],
        "deployments": [],
    }
    pm = _project_manager(project_data)

    with (
        patch("opi.web.router.get_current_user", return_value={"email": "x@example.com"}),
        patch("opi.web.router.get_project_store", return_value=_store_with_component()),
        patch("opi.manager.project_manager.ProjectManager", return_value=pm),
    ):
        response = await release_component_resource_pin(_request(), PROJECT, COMPONENT, "limits_memory")

    assert response.status_code == 303
    pm.save_and_commit_project.assert_awaited_once()
    committed = pm.save_and_commit_project.await_args.args[0]
    assert ProjectFileHandler().get_component_resource_pins(committed, COMPONENT) is None
    pm.close.assert_awaited_once()


async def test_an_unpinned_field_writes_nothing() -> None:
    """The field was never pinned, so there is nothing to release and nothing to commit."""
    project_data = {
        "name": PROJECT,
        "components": [
            {"name": COMPONENT, "resources": {"requests": {"memory": "64Mi"}, "limits": {"memory": "256Mi"}}}
        ],
        "deployments": [],
    }
    pm = _project_manager(project_data)

    with (
        patch("opi.web.router.get_current_user", return_value={"email": "x@example.com"}),
        patch("opi.web.router.get_project_store", return_value=_store_with_component()),
        patch("opi.manager.project_manager.ProjectManager", return_value=pm),
    ):
        await release_component_resource_pin(_request(), PROJECT, COMPONENT, "limits_memory")

    pm.save_and_commit_project.assert_not_awaited()
    pm.close.assert_awaited_once()


async def test_an_htmx_request_gets_an_hx_redirect() -> None:
    project_data = {
        "name": PROJECT,
        "components": [{"name": COMPONENT, "resources": {"limits": {"memory": "256Mi"}}}],
        "deployments": [],
    }
    pm = _project_manager(project_data)

    with (
        patch("opi.web.router.get_current_user", return_value={"email": "x@example.com"}),
        patch("opi.web.router.get_project_store", return_value=_store_with_component()),
        patch("opi.manager.project_manager.ProjectManager", return_value=pm),
    ):
        response = await release_component_resource_pin(_request(htmx=True), PROJECT, COMPONENT, "limits_memory")

    assert response.status_code == 200
    assert "HX-Redirect" in response.headers


async def test_the_project_manager_is_always_closed_even_on_failure() -> None:
    pm = MagicMock()
    pm.get_contents = AsyncMock(side_effect=RuntimeError("git unreachable"))
    pm.close = AsyncMock()

    with (
        patch("opi.web.router.get_current_user", return_value={"email": "x@example.com"}),
        patch("opi.web.router.get_project_store", return_value=_store_with_component()),
        patch("opi.manager.project_manager.ProjectManager", return_value=pm),
        pytest.raises(RuntimeError),
    ):
        await release_component_resource_pin(_request(), PROJECT, COMPONENT, "limits_memory")

    pm.close.assert_awaited_once()
