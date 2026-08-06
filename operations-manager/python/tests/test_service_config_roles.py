"""Every layer a service carries config on says what that config IS (RC-38).

"Service config" was one word for three things: defining something (putting it into the
project), using it (this component uses this service, this thing), and binding it (how it
reaches the workload). Attachments is the first service where the three visibly come
apart -- a catalog entry at project level, a reference plus a delivery at component level
-- and the API showed it: the project layer had a model but no route, because the route
generator asked the wizard's editables rather than the service what it can do.

The invariant here is not "every service declares roles" (the default answers for the
whole catalog) but "no layer with config is silent about what it is": a layer the service
carries config on names at least one role, and a layer it does not carry config on names
none. A role on an empty layer is stale bookkeeping in the same way a form exemption for
a layer without config is.
"""

from __future__ import annotations

import pytest
from opi.services.catalog.base import ConfigLayer, ConfigRole, Service
from opi.services.registry import SERVICES
from opi.services.services_enums import ServiceType

_SERVICES = list(SERVICES.values())
_IDS = [s.service_type.value for s in _SERVICES]


class TestEveryConfigLayerHasARole:
    @pytest.mark.parametrize("service", _SERVICES, ids=_IDS)
    def test_layer_with_config_names_a_role(self, service) -> None:
        for layer in service.config_layers():
            roles = service.config_roles(layer)
            assert roles, (
                f"Service '{service.service_type.value}' carries config at layer '{layer.value}' "
                f"but says nothing about what that config is. Declare config_roles({layer}) as "
                f"some combination of DEFINE / USE / BIND."
            )
            assert all(isinstance(role, ConfigRole) for role in roles), (
                f"'{service.service_type.value}' returns non-ConfigRole values at '{layer.value}': {roles}"
            )

    @pytest.mark.parametrize("service", _SERVICES, ids=_IDS)
    def test_no_role_on_a_layer_without_config(self, service) -> None:
        # A layer counts when the service declares it (editables / API fields / layout)
        # or when it narrows a model onto it. The second is not the inherited default:
        # ``config_model_for`` answers everywhere for a service that never overrode it,
        # so this only widens for a service that says per layer what it carries -- which
        # is how attachments reaches the deployment-component overrides that have no
        # form of their own.
        layers = set(service.config_layers())
        for layer in ConfigLayer:
            if service.data_model_for(layer) is not None:
                layers.add(layer)
            if type(service).config_model_for is not Service.config_model_for and (
                service.config_model_for(layer) is not None
            ):
                layers.add(layer)
        stray = {layer for layer in ConfigLayer if layer not in layers and service.config_roles(layer)}
        assert not stray, (
            f"'{service.service_type.value}' claims a role on layers it carries no config on: "
            f"{[layer.value for layer in stray]}"
        )

    @pytest.mark.parametrize("service", _SERVICES, ids=_IDS)
    def test_binding_never_stands_alone(self, service) -> None:
        # A binding says HOW the used thing reaches the workload, so a layer that binds
        # without using has nothing to bind: the reference it would describe is absent.
        for layer in service.config_layers():
            roles = service.config_roles(layer)
            if ConfigRole.BIND in roles:
                assert ConfigRole.USE in roles, (
                    f"'{service.service_type.value}' binds at '{layer.value}' without using anything there"
                )


class TestAttachmentsIsExpressedInTheThreeKinds:
    """The service the vocabulary was introduced for, spelled out."""

    def test_project_level_defines(self) -> None:
        service = SERVICES[ServiceType.ATTACHMENTS]
        assert service.config_roles(ConfigLayer.PROJECT) == (ConfigRole.DEFINE,)

    def test_component_level_uses_and_binds(self) -> None:
        service = SERVICES[ServiceType.ATTACHMENTS]
        assert service.config_roles(ConfigLayer.COMPONENT) == (ConfigRole.USE, ConfigRole.BIND)

    def test_deployment_component_overrides_the_same_use_and_binding(self) -> None:
        service = SERVICES[ServiceType.ATTACHMENTS]
        assert service.config_roles(ConfigLayer.DEPLOYMENT_COMPONENT) == (ConfigRole.USE, ConfigRole.BIND)

    def test_nothing_at_deployment_level(self) -> None:
        service = SERVICES[ServiceType.ATTACHMENTS]
        assert service.config_roles(ConfigLayer.DEPLOYMENT) == ()
        assert service.config_model_for(ConfigLayer.DEPLOYMENT) is None


class TestTheRestOfTheCatalogUses:
    def test_default_is_use_for_every_configured_layer(self) -> None:
        # Not a tautology check: it measures that no service was left with a role set
        # that contradicts the default it never overrode. A service that carries config
        # somewhere and answers () there would fail the first test in this module; this
        # one pins that the unchanged catalog reads as "use", which is what it means.
        others = [s for s in _SERVICES if s.service_type is not ServiceType.ATTACHMENTS]
        for service in others:
            for layer in service.config_layers():
                assert ConfigRole.USE in service.config_roles(layer), (
                    f"'{service.service_type.value}' at '{layer.value}' claims no use"
                )
