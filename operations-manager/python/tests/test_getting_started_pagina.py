"""The Getting started page: public, and it names only env vars that exist."""

import re
from typing import TYPE_CHECKING

from opi.services.catalog.postgresql_database.variables import DatabaseVariables
from opi.services.registry import SERVICE_DEFINITIONS
from opi.web.router import getting_started

if TYPE_CHECKING:
    from fastapi.testclient import TestClient


def test_page_is_public(test_client: TestClient) -> None:
    response = test_client.get("/getting-started")

    assert response.status_code == 200, response.text
    assert "Getting started" in response.text
    assert getattr(getting_started, "_requires_sso", False) is False


def test_named_env_vars_exist_in_the_catalog(test_client: TestClient) -> None:
    """A renamed variable must not leave this page handing out a name that gives nothing."""
    known = {
        name
        for service in SERVICE_DEFINITIONS.values()
        for variable in service.variables
        for name in (variable.name, *variable.aliases)
    }
    html = test_client.get("/getting-started").text

    named = set(re.findall(r"\b(?:DATABASE|PUBLIC)_[A-Z_]+\b", html))

    assert {"DATABASE_SERVER_FULL", "DATABASE_SERVER_HOST", "DATABASE_PASSWORD"} <= named
    assert named <= known


def test_postgres_list_is_complete(test_client: TestClient) -> None:
    html = test_client.get("/getting-started").text

    assert {variable.value.name for variable in DatabaseVariables} <= set(re.findall(r"\bDATABASE_[A-Z_]+\b", html))


def test_named_service_exists_in_the_catalog(test_client: TestClient) -> None:
    labels = {service.name for service in SERVICE_DEFINITIONS.values()}

    assert "Publiceren op het web" in labels
    assert "Publiceren op het web" in test_client.get("/getting-started").text
