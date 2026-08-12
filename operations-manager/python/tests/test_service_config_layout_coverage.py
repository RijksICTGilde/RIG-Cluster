"""Elke editable van een dienstsectie moet ook echt op het scherm staan.

Een ``FormSection`` heeft twee lijsten die verschillende dingen doen. ``editables``
zegt welke velden bij de sectie horen -- en daarmee wat de stroom mag SCHRIJVEN
(``flow_write_paths`` leidt de schrijfrechten daaruit af). ``layout`` zegt wat er
GETEKEND wordt.

Lopen die twee uit elkaar, dan is dat de slechtst denkbare combinatie: het veld telt
mee voor wat de stroom mag schrijven, maar het formulier toont het nooit en dient het
dus altijd leeg in. Elke keer opslaan schrijft dan een lege waarde over wat er stond,
zonder foutmelding en zonder zichtbaar leeg veld.

Zo is ``services/keycloak/config/additional_redirect_uris`` in productie leeggelopen.
Deze toets is daarom datagedreven over de hele catalogus en niet met de hand voor
keycloak: het kan bij elke dienst gebeuren en is bij deze dienst alleen toevallig
opgemerkt.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from opi.forms.editables.editable import WidgetType
from opi.forms.layout import layout_field_names
from opi.forms.visualizers.sections import FormSection
from opi.services.catalog.base import ConfigLayer
from opi.services.registry import SERVICES
from opi.services.services_enums import ServiceType

if TYPE_CHECKING:
    from opi.forms.visualizers.visualizer import EditableVisualizer


def _item_relative_variants(yaml_path: str) -> set[str]:
    """Het pad zelf plus de vormen waarin een layout ernaar kan verwijzen.

    Een sectie van de component- of deploymentlaag wordt gerenderd BINNEN de reeks
    waar hij bij hoort, en de renderer haalt daar het ``components[i]/``-voorvoegsel
    van de veldnamen af (zie de ``item_prefix``-tak in ``_render_layout_element``).
    De layout noemt het veld dus item-relatief, terwijl de editable het volledige pad
    draagt.
    """
    variants = {yaml_path}
    rest = yaml_path
    while "[*]/" in rest:
        rest = rest.split("[*]/", 1)[1]
        variants.add(rest)
    return variants


def _drawable_paths(vis: EditableVisualizer) -> set[str]:
    """De namen waaronder de renderer deze editable in de veldtabel zet.

    Een ``group`` bestaat niet als eigen veld: ``_build_group_fields`` klapt hem plat
    en de layout verwijst naar de kinderen. Voor al het andere is dat het eigen pad.
    """
    if vis.widget == WidgetType.GROUP and vis.children:
        paths: set[str] = set()
        for child in vis.children:
            paths |= _drawable_paths(child)
        return paths
    return _item_relative_variants(vis.editable.yaml_path)


def _config_sections() -> list[tuple[ServiceType, ConfigLayer, FormSection]]:
    """Elke dienstsectie die een eigen layout heeft, over alle configuratielagen."""
    found = []
    for service_type, service in SERVICES.items():
        for layer in ConfigLayer:
            section = service.config_form_section(layer)
            if section is not None and section.layout is not None:
                found.append((service_type, layer, section))
    return found


@pytest.mark.parametrize(
    ("service_type", "layer", "section"),
    _config_sections(),
    ids=lambda value: value.value if isinstance(value, (ServiceType, ConfigLayer)) else "",
)
def test_every_editable_is_drawn(service_type: ServiceType, layer: ConfigLayer, section: FormSection) -> None:
    """Geen enkele editable van de sectie mag buiten de layout vallen."""
    drawn = layout_field_names(section.layout)
    ongetekend = [
        vis.editable.yaml_path for vis in section.editables if not (_drawable_paths(vis) & drawn) and not vis.readonly
    ]
    assert not ongetekend, (
        f"{service_type.value} ({layer.value}, sectie '{section.section_id}') registreert velden die de layout "
        f"niet tekent: {ongetekend}. Zo'n veld dient altijd leeg in en wist bij elke opslag wat er stond -- "
        f"zet het in de layout of haal het uit editables."
    )


def test_the_gate_catches_a_registered_but_undrawn_field() -> None:
    """De poort zelf: een sectie met een veld buiten de layout moet opvallen."""
    from opi.services.catalog.keycloak.visualizers import KEYCLOAK_REDIRECT_URIS, KEYCLOAK_TEMPLATE

    section = FormSection(
        section_id="verzonnen",
        title="Verzonnen",
        editables=[KEYCLOAK_TEMPLATE, KEYCLOAK_REDIRECT_URIS],
        layout=["services/keycloak/config/template"],
    )
    drawn = layout_field_names(section.layout)
    ongetekend = [vis.editable.yaml_path for vis in section.editables if not (_drawable_paths(vis) & drawn)]
    assert ongetekend == ["services/keycloak/config/additional_redirect_uris"]
