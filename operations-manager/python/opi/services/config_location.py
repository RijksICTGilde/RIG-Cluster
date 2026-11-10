"""Where a user configures a service, in words, derived from the registry.

Two questions look alike and are not the same:

* **Selection** (``ServiceDefinition.selectable_per_component``): does an individual
  component tick this service, or does the project's own choice settle it.
* **Config layer** (``ConfigLayer``): at which level of the project file the service's
  settings live, and therefore on which screen they are edited.

They differ in practice: keycloak is ticked per component (each component decides whether
it sits behind login) while its configuration is one realm for the whole project, so its
config lives at ``ConfigLayer.PROJECT``. Anything telling a user "where do I configure
this" must read the layers, never the selection.

``project_step_config_hint`` explains, per service, why a ticked service shows no settings
on the project-wide services step. It derives that from what the service declares, so no
template and no screen carries a service name.
"""

from __future__ import annotations

from opi.services.catalog.base import ConfigLayer
from opi.services.registry import SERVICES
from opi.services.services_enums import ServiceType

#: The non-project layers, as a SHORT phrase saying where the service is configured.
#:
#: THE PHRASE NAMES THE DIMENSIONS, NOT THE LAYERS. There are only two things you configure
#: something on, a component and a deployment, and the middle layer is the combination of
#: those two, so it counts for both and duplicate phrases drop out:
#:
#:     component only                  -> "per component"
#:     component + deployment          -> "per component per deployment"
#:     component + component-in-deploy -> "per component per deployment"
#:
#: Ordered, so a service carrying config on more than one layer always gives the same
#: order.
_LAYER_PHRASES: dict[ConfigLayer, tuple[str, ...]] = {
    ConfigLayer.COMPONENT: ("per component",),
    ConfigLayer.DEPLOYMENT_COMPONENT: ("per component", "per deployment"),
    ConfigLayer.DEPLOYMENT: ("per deployment",),
}


def selection_labels(service_type: ServiceType) -> list[str]:
    """The user-facing phrases for how a service is chosen, for the service card."""
    definition = SERVICES[service_type].definition
    labels = ["Per component aan te zetten" if definition.selectable_per_component else "Geldt voor het hele project"]
    if definition.shared_per_deployment:
        labels.append("Gedeeld per deployment")
    return labels


def project_step_config_hint(service_type: ServiceType) -> str | None:
    """Why a ticked service shows no settings on the project-wide services step.

    Returns None when there is nothing to explain: the service does offer a
    project-level form section (its config screen follows as the user expects), or it
    carries no config at any layer (nothing to point at anywhere).
    """
    service = SERVICES.get(service_type)
    if service is None:
        return None
    if service.config_form_section(ConfigLayer.PROJECT) is not None:
        return None

    layers = [layer for layer in service.config_layers() if layer in _LAYER_PHRASES]
    if not layers:
        return None

    phrases: list[str] = []
    for layer in _LAYER_PHRASES:
        if layer not in layers:
            continue
        for phrase in _LAYER_PHRASES[layer]:
            if phrase not in phrases:
                phrases.append(phrase)
    return " ".join(phrases)


def config_hint_for_value(service_value: str) -> str | None:
    """``project_step_config_hint`` from a raw service name.

    The form layer carries service identity as the plain project-file string (the
    selection field's option values), so a caller there has no enum in hand. An
    unrecognised name yields None rather than raising: the picker is also used with
    stubbed options in tests, and an unknown service has nothing to explain anyway.
    """
    try:
        service_type = ServiceType(service_value)
    except ValueError:
        return None
    return project_step_config_hint(service_type)
