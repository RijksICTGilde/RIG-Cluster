"""A service looks the same on the project page as where it was chosen.

The components section listed a component's services as ``{{ svc_names|join(", ")
|replace("-", " ")|title }}``: "Keycloak, Publish On Web". No icon, no description, no
question mark, and a name that matches neither the wizard's label ("Keycloak
Authentication") nor the name in the project file ("publish-on-web") -- so the same
thing was called three things in three places.

What is pinned here is what the raw line could not do: the definition behind the name
is looked up (icon, description, help template), whatever shape the entry has in the
project file, and an entry nobody recognises still shows up instead of vanishing.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from opi.core.templates import get_service_definition_for_entry, templates
from opi.services.services import ServiceAdapter
from opi.services.services_enums import ServiceType


def _render(services: list[Any]) -> str:
    template = templates.env.get_template("project-details/section-components.html.j2")
    return template.render(
        request=None,
        project={"name": "demo", "components": [{"name": "app", "type": "single", "services": services}]},
        user_role="viewer",
    )


def _titles(html: str) -> list[str]:
    return re.findall(r'service-card__title">([^<]*)</span>', html)


# ---------------------------------------------------------------------------
# The lookup behind the block
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "entry",
    [
        "keycloak",
        {"reference": "keycloak"},
        {"name": "keycloak", "config": {}},
        {"keycloak": {"config": {}}},
    ],
    ids=["string", "reference-record", "name-record", "legacy-single-key"],
)
def test_the_definition_is_found_whatever_shape_the_entry_has(entry: Any) -> None:
    """A component's services list carries all four shapes; reading the keys of a record
    once rendered a service as "Reference, Config"."""
    definition = get_service_definition_for_entry(entry)
    assert definition is ServiceAdapter.get_service_definition(ServiceType.KEYCLOAK)


def test_an_unknown_name_has_no_definition() -> None:
    """Rather than raising on a project file from an older schema."""
    assert get_service_definition_for_entry("iets-wat-niet-bestaat") is None


# ---------------------------------------------------------------------------
# What the page shows
# ---------------------------------------------------------------------------


def test_a_service_shows_its_icon_description_and_explanation() -> None:
    definition = ServiceAdapter.get_service_definition(ServiceType.KEYCLOAK)
    html = _render([{"reference": "keycloak"}])

    assert definition.name in html
    assert definition.description in html
    assert f"rvo-icon-{definition.icon}" in html
    assert f"openServiceHelp('{definition.help_template}')" in html


def test_every_service_of_a_component_gets_its_own_block() -> None:
    html = _render(["keycloak", "publish-on-web", {"postgresql-database": {"config": {}}}])

    assert _titles(html) == [
        ServiceAdapter.get_service_definition(ServiceType.KEYCLOAK).name,
        ServiceAdapter.get_service_definition(ServiceType.PUBLISH_ON_WEB).name,
        ServiceAdapter.get_service_definition(ServiceType.POSTGRESQL_DATABASE).name,
    ]
    assert html.count("openServiceHelp(") == 3


def test_an_unknown_service_is_still_shown() -> None:
    """It is in the project file, so hiding it would hide the problem."""
    html = _render(["iets-wat-niet-bestaat"])

    assert "iets-wat-niet-bestaat" in html
    assert _titles(html) == []


def test_the_service_names_are_no_longer_a_title_cased_line() -> None:
    """The old rendering turned "publish-on-web" into "Publish On Web", a name that
    exists nowhere else."""
    html = _render(["publish-on-web"])

    assert "Publish On Web" not in html
    assert ServiceAdapter.get_service_definition(ServiceType.PUBLISH_ON_WEB).name in html
