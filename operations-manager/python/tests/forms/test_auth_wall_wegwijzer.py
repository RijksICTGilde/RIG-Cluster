"""Wie de auth wall op een component aanzet, ziet waar hij hem instelt.

DE MELDING

De authorization wall is de enige dienst die je op het ENE niveau aanzet en op het ANDERE
instelt. Aanvinken gebeurt per component, want daar komt de oauth2-proxy voor de pod;
instellen gebeurt projectbreed, want de bannertekst geldt voor de hele toegangspagina. In
het componentformulier zag je daardoor alleen een vinkje en verder niets: geen uitleg van
wat er gebeurt, en geen aanwijzing waar die ene instelling dan wel staat (tabblad Services,
kaart Authorization Wall).

WAT DIT WEL EN NIET IS

Een wegwijzer, geen tweede editor. Het veld is projectbreed, dus een invoerveld per
component zou suggereren dat je het per component kunt zetten.

Het blok haakt ook bewust niet in via ``config_component_layout()``: die haak telt mee in
``config_layers()``, en dan zou er een configroute op componentniveau in de API bijkomen
voor iets wat geen configuratie is. Die kant wordt hieronder ook getoetst, want dat is de
val waar deze wijziging de eerste keer in liep.
"""

from __future__ import annotations

from typing import Any

import pytest
from opi.forms import FormRenderer, get_default_nl_translator
from opi.forms.visualizers.wizard_sections import build_component_edit_section
from opi.forms.widgets.lotc import LOTCWidgetAdapter
from opi.services.catalog.base import ConfigLayer
from opi.services.registry import SERVICES
from opi.services.services_enums import ServiceType

#: Een stuk van de hulptekst, genoeg om hem te herkennen en niet zoveel dat elke
#: herformulering deze test rood maakt.
WEGWIJZER = "tabblad Services"


def _project(*component_services: str) -> dict[str, Any]:
    return {
        "name": "demo",
        "services": ["publish-on-web", "keycloak", "authorization-wall"],
        "components": [{"name": "frontend", "services": list(component_services)}],
    }


def _render(yaml_data: dict[str, Any]) -> str:
    section = build_component_edit_section(0)
    renderer = FormRenderer(widget_adapter=LOTCWidgetAdapter(), translator=get_default_nl_translator())
    return renderer.render_fields_from_editables(
        editables=section.editables,
        yaml_data=yaml_data,
        layout=section.layout,
        edit_mode=True,
    )


def test_de_wegwijzer_staat_er_als_de_auth_wall_aanstaat() -> None:
    html = _render(_project("publish-on-web", "keycloak", "authorization-wall"))

    assert WEGWIJZER in html
    assert "Authorization wall" in html


def test_zonder_auth_wall_staat_hij_er_niet() -> None:
    """Anders leest elk component alsof het achter een inlogpagina staat."""
    html = _render(_project("publish-on-web", "keycloak"))

    assert WEGWIJZER not in html


def test_de_wegwijzer_maakt_geen_configuratielaag() -> None:
    """De val: een layouthaak op componentniveau telt mee als 'hier zit config'.

    Gebeurt dat, dan krijgt de API er een route
    ``/services/authorization-wall/config/component/{component}`` bij voor iets wat je
    niet kunt instellen.
    """
    service = SERVICES[ServiceType.AUTHORIZATION_WALL]

    assert service.config_layers() == [ConfigLayer.PROJECT]
    assert service.config_editables(ConfigLayer.COMPONENT) == []
    assert service.config_form_section(ConfigLayer.COMPONENT) is None


@pytest.mark.parametrize("layer", [ConfigLayer.COMPONENT, ConfigLayer.DEPLOYMENT])
def test_alleen_het_projectniveau_neemt_configuratie_aan(layer: ConfigLayer) -> None:
    assert SERVICES[ServiceType.AUTHORIZATION_WALL].config_api_fields(layer) == []
