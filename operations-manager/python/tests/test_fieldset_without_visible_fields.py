"""A fieldset whose fields are all hidden renders nothing, not a legend around nothing."""

from typing import Any

from opi.forms.layout import Fieldset
from opi.forms.renderer import FormRenderer
from opi.forms.visualizers.wizard_sections import build_deployment_edit_section
from opi.forms.widgets.lotc import LOTCWidgetAdapter
from opi.web.router_wizard import _create_renderer

_LEGEND = "Eigen registry (alleen voor deze deployment)"


def _project(services: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "name": "demo",
        "services": services,
        "components": [{"reference": "web", "image": "nginx:alpine"}],
        "deployments": [{"name": "productie", "cluster": "local", "components": [{"reference": "web"}]}],
    }


def _deployment_edit_html(project: dict[str, Any]) -> str:
    section = build_deployment_edit_section(0)
    return _create_renderer().render_fields_from_editables(
        editables=section.editables, yaml_data=project, layout=section.layout or [], edit_mode=True
    )


def test_a_fieldset_with_only_hidden_fields_renders_nothing() -> None:
    renderer = FormRenderer(LOTCWidgetAdapter())
    fieldset = Fieldset(legend="Eigen registry", description="uitleg", children=["absent/field"])

    assert renderer._render_layout_element(fieldset, {}) == ""


def test_without_image_registries_the_deployment_edit_shows_no_registry_fieldset() -> None:
    assert _LEGEND not in _deployment_edit_html(_project([]))


def test_with_a_registry_the_deployment_edit_offers_the_override() -> None:
    registry = {"name": "code-overheid", "display-name": "Code overheid", "url": "code.overheid.nl/team"}
    project = _project([{"name": "image-registries", "config": {"registries": [registry]}}])

    assert _LEGEND in _deployment_edit_html(project)
