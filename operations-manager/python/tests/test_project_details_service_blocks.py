"""A service looks the same on the project page as where it was chosen.

The components section listed a component's services as ``{{ svc_names|join(", ")
|replace("-", " ")|title }}``: "Keycloak, Publish On Web". No icon, no description, no
question mark, and a name that matches neither the wizard's label ("Keycloak
Authentication") nor the name in the project file ("publish-on-web") -- so the same
thing was called three things in three places.

What is pinned here is what the raw line could not do: the definition behind the name
is looked up (naam, hulptekst), whatever shape the entry has in the project file, and an
entry nobody recognises still shows up instead of vanishing.

WAAR DIT MEET, EN WAAROM DAT VERANDERD IS (RC-97). Deze test rendeerde
``project-details/section-components.html.j2``, een sjabloon van de vervangen pagina. Het
tabblad Componenten dat de route WEL rendert is ``bg/project-tabs.html.j2``; daar gaat de
lijst nu doorheen, met de voorbeeldgegevens van de proefopstelling en een eigen
componentenlijst erin.

Wat het herontwerp anders doet: de diensten van een component staan in een INGEKLAPTE
lijst met per dienst een rij (naam plus de omschrijving als ondertitel) en een vraagteken
als rijbediening. Dat was eerst een rij chips, en die liep bij veertien diensten over drie
regels door. De naam en de hulptekst zijn dus nog te meten; het icoon niet, en die claim
hoorde bij de oude vormgeving (genoteerd in ``docs/opruiming-inventarisatie-rc97.md``).
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import pytest
from opi.core.template_helpers import get_service_definition_for_entry
from opi.core.templates_lotc import templates_lotc as templates
from opi.services.services import ServiceAdapter
from opi.services.services_enums import ServiceType
from opi.web.lotc_fixtures import page_data


def _render(services: list[Any]) -> str:
    """Het tabblad Componenten zoals de route het rendert, met een eigen componentenlijst.

    De rest van de context komt uit de proefopstelling, want deze pagina heeft er veel
    van nodig en die zijn hier niet het onderwerp.
    """
    request = SimpleNamespace(
        scope={"type": "http"},
        headers={},
        cookies={},
        state=SimpleNamespace(),
        url=SimpleNamespace(path="/"),
        session={},
        query_params={},
    )
    data = page_data("project-tabs")
    data["project"] = {**data["project"], "components": [{"name": "app", "type": "single", "services": services}]}
    data["active_tab"] = "componenten"
    data["tabs"] = {sleutel: {**tab, "url": "#"} for sleutel, tab in data["tabs"].items()}
    return templates.env.get_template("bg/project-tabs.html.j2").render(request=request, navigation=[], **data)


def _dienstnamen(html: str) -> list[str]:
    """De dienstnamen van het component, zoals ze op het tabblad staan.

    Vanaf de kop "Services (", want daar begint de lijst met de diensten van het
    component. Ze stonden hier als ``<c-chip>`` op een rij; veertien chips met achter elke
    chip een los vraagteken liepen over drie regels door en waren niet te lezen, dus het
    is een ingeklapte lijst geworden met per dienst een rij. De namen zitten sindsdien in
    het ``text``-attribuut van een ``<nldd-text-cell>``: die cel tekent zijn tekst in zijn
    SCHADUWBOOM, dus er valt in de uitgestuurde HTML geen tekst tussen tags te vinden.

    Er wordt op het ANTWOORD gemeten en niet op de componenttag, anders zou de meting stil
    meeveranderen met wat het thema ervan maakt.
    """
    start = html.find("Services (")
    if start == -1:
        return []
    return [naam.strip() for naam in re.findall(r'<nldd-text-cell[^>]*?text="([^"]*)"', html[start:])]


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


def test_a_service_shows_its_name_and_its_explanation() -> None:
    definition = ServiceAdapter.get_service_definition(ServiceType.KEYCLOAK)
    html = _render([{"reference": "keycloak"}])

    assert _dienstnamen(html) == [definition.name]
    assert f"openServiceHelp('{definition.help_template}')" in html


def test_every_service_of_a_component_gets_its_own_row() -> None:
    html = _render(["keycloak", "publish-on-web", {"postgresql-database": {"config": {}}}])

    assert _dienstnamen(html) == [
        ServiceAdapter.get_service_definition(ServiceType.KEYCLOAK).name,
        ServiceAdapter.get_service_definition(ServiceType.PUBLISH_ON_WEB).name,
        ServiceAdapter.get_service_definition(ServiceType.POSTGRESQL_DATABASE).name,
    ]
    assert html.count("openServiceHelp(") == 3


def test_an_unknown_service_is_still_shown() -> None:
    """It is in the project file, so hiding it would hide the problem."""
    html = _render(["iets-wat-niet-bestaat"])

    assert _dienstnamen(html) == ["iets-wat-niet-bestaat"]
    # Geen definitie, dus ook geen vraagteken: er valt niets uit te leggen.
    assert "openServiceHelp(" not in html


def test_the_service_names_are_no_longer_a_title_cased_line() -> None:
    """The old rendering turned "publish-on-web" into "Publish On Web", a name that
    exists nowhere else."""
    html = _render(["publish-on-web"])

    assert "Publish On Web" not in html
    assert ServiceAdapter.get_service_definition(ServiceType.PUBLISH_ON_WEB).name in html
