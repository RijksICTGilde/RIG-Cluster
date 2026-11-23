"""A deviation on a deployment is visible in the component form (RC-498).

At ``algor-odc`` the registry choice sat on the deployment-component layer, and that layer
does not appear in the component form. The field there read "Automatisch", which was true for
its own layer, and the consumer saw nothing to change while the image did come from a
registry.

The notice hangs off the empty option of the option list: that is precisely the state that
confused, and the option list is the one place that, while rendering, knows both the component
(through the path) and the deployments (through the project data).
"""

from __future__ import annotations

from typing import Any

from opi.forms.i18n import get_default_nl_translator
from opi.forms.renderer import FormRenderer
from opi.forms.visualizers.wizard_sections import build_component_edit_section
from opi.forms.widgets.lotc import LOTCWidgetAdapter
from opi.services.services_enums import ServiceType

_SVC = ServiceType.IMAGE_REGISTRIES.value
_REGISTRY = {"name": "x", "display-name": "GitHub", "upstream": "ghcr.io/org"}
_NOTICE = "Pas die aan in de betreffende deployment"


def _project(deployment_registry: str | None, component_registry: str | None = None) -> dict[str, Any]:
    component: dict[str, Any] = {"name": "component-1", "image": "ghcr.io/org/app:1"}
    if component_registry:
        component["services"] = [{"name": _SVC, "config": {"registry": component_registry}}]
    deployment_component: dict[str, Any] = {"reference": "component-1"}
    if deployment_registry:
        deployment_component["services"] = {_SVC: {"config": {"registry": deployment_registry}}}
    return {
        "name": "demo",
        "services": [{"name": _SVC, "config": {"registries": [_REGISTRY]}}],
        "components": [component],
        "deployments": [{"name": "deployment-1", "components": [deployment_component]}],
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


def test_the_notice_names_the_deployment_and_the_registry() -> None:
    html = _render(_project("x"))

    assert "deployment-1" in html
    assert "GitHub" in html
    assert _NOTICE in html


def test_without_a_deviation_there_is_no_notice() -> None:
    assert _NOTICE not in _render(_project(None))


def test_a_choice_on_the_component_itself_leaves_no_notice() -> None:
    """The field is then not empty and does point at where the image comes from."""
    assert _NOTICE not in _render(_project("x", component_registry="x"))


def test_two_deployments_use_the_plural() -> None:
    project = _project("x")
    project["deployments"].append(
        {
            "name": "deployment-2",
            "components": [{"reference": "component-1", "services": {_SVC: {"config": {"registry": "x"}}}}],
        }
    )
    html = _render(project)

    assert "deployment-1" in html
    assert "deployment-2" in html
    assert "Pas die aan in de betreffende deployments" in html
