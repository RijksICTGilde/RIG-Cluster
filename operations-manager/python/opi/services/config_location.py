"""Where a user configures a service, in words, derived from the registry (RC-33).

Two questions look alike and are not the same:

* **Binding** (``ServiceDefinition.binding``): does an individual component tick this
  service, or does a whole deployment get it at once. This is about *selection*.
* **Config layer** (``ConfigLayer``): at which level of the project file the service's
  settings live, and therefore on which screen they are edited.

They differ in practice: keycloak binds per component (each component decides whether it
sits behind login) while its configuration is one realm for the whole project, so its
config lives at ``ConfigLayer.PROJECT``. Anything telling a user "where do I configure
this" must read the layers, never the binding.

The concrete gap this closes: on the project-wide services step a user ticks a service
whose config lives only on the component layer, no configuration screen follows, and
nothing explains why. ``project_step_config_hint`` produces that explanation, per service,
from what the service itself declares -- so no template and no screen carries a service
name.
"""

from __future__ import annotations

from opi.services.catalog.base import ConfigLayer
from opi.services.registry import SERVICES
from opi.services.services_enums import ServiceBinding, ServiceType

#: How a service's binding reads to a user. Rendered on the project-details service card,
#: which previously showed the raw enum value plus the English word "scope" -- read by at
#: least one user as the answer to "where do I configure this", which it is not.
BINDING_LABELS: dict[ServiceBinding, str] = {
    ServiceBinding.COMPONENT: "Per component te kiezen",
    ServiceBinding.DEPLOYMENT: "Gedeeld per deployment",
}

#: The non-project layers, phrased as the place a user goes. Ordered, so a service that
#: carries config on more than one layer always reads the same way.
_LAYER_PHRASES: dict[ConfigLayer, str] = {
    ConfigLayer.COMPONENT: "per component, bij Componenten",
    ConfigLayer.DEPLOYMENT_COMPONENT: "per component binnen een deployment",
    ConfigLayer.DEPLOYMENT: "per deployment",
}


def binding_label(service_type: ServiceType) -> str:
    """The user-facing phrase for how a service is chosen (component vs deployment)."""
    return BINDING_LABELS[SERVICES[service_type].definition.binding]


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

    phrases = [_LAYER_PHRASES[layer] for layer in _LAYER_PHRASES if layer in layers]
    joined = " en ".join(phrases)
    return f"Geen projectbrede instellingen; u stelt deze dienst {joined} in."


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
