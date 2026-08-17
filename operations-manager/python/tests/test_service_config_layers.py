"""Every layer a service carries config on is editable somewhere (RC-25).

The gap this locks: a service declares config at a layer (editables, API fields, layout
nodes) but answers ``config_form_section`` nowhere near it, so the value exists, the API
accepts it, the model validates it -- and no user can reach it. Measured on 4 August 2026
that was true of seven of the fifteen services, plus two that answered at a different
layer than the one their config lives on.

The invariant is deliberately not "every layer has a form": some layers are OPI-written
state and some are API-only on purpose. It is "every layer has an answer" --
``config_form_section`` or an entry in ``form_exempt_layers`` with a reason. A forgotten
layer and a deliberate one then look different in the code, which is the whole point.
"""

from __future__ import annotations

import pytest
from opi.services.catalog.base import ConfigLayer
from opi.services.registry import SERVICES
from opi.services.services_enums import ServiceType

_SERVICES = list(SERVICES.values())
_IDS = [s.service_type.value for s in _SERVICES]


class TestEveryConfigLayerIsReachable:
    @pytest.mark.parametrize("service", _SERVICES, ids=_IDS)
    def test_layer_has_a_form_section_or_a_declared_reason(self, service) -> None:
        for layer in service.config_layers():
            section = service.config_form_section(layer)
            exemption = service.form_exempt_layers.get(layer)
            assert section is not None or exemption, (
                f"Service '{service.service_type.value}' carries config at layer "
                f"'{layer.value}' but offers no form section there and declares no reason. "
                f"Either implement config_form_section({layer}) or add the layer to "
                f"form_exempt_layers with why it has no form."
            )

    @pytest.mark.parametrize("service", _SERVICES, ids=_IDS)
    def test_exemptions_only_cover_layers_that_carry_config(self, service) -> None:
        # An exemption for a layer with no config is stale bookkeeping: it would keep
        # passing after the config it excused moved away.
        stale = set(service.form_exempt_layers) - set(service.config_layers())
        assert not stale, f"'{service.service_type.value}' excuses layers it has no config on: {stale}"

    def test_the_measured_gap_is_closed(self) -> None:
        # The seven services from the RC-25 measurement, by the layer their config is on.
        expected = {
            ServiceType.HEALTH_CHECK: ConfigLayer.COMPONENT,
            ServiceType.METRICS_SCRAPER: ConfigLayer.COMPONENT,
            ServiceType.PERSISTENT_STORAGE: ConfigLayer.COMPONENT,
            ServiceType.PUBLISH_ON_WEB: ConfigLayer.COMPONENT,
            ServiceType.TEMP_STORAGE: ConfigLayer.COMPONENT,
            ServiceType.MINIO_STORAGE: ConfigLayer.PROJECT,
            ServiceType.REDIS: ConfigLayer.PROJECT,
            # The two layer mismatches: config on the component, section on the project.
            ServiceType.ATTACHMENTS: ConfigLayer.COMPONENT,
        }
        for service_type, layer in expected.items():
            service = SERVICES[service_type]
            assert layer in service.config_layers()
            assert service.config_form_section(layer) is not None, (
                f"'{service_type.value}' still has no form section at '{layer.value}'"
            )


class TestComponentLayerSectionsAreDerived:
    def test_built_from_what_the_service_declares(self) -> None:
        # A component-level service needs no config_form_section of its own: the base
        # class builds it from its visualizers + layout, so the section can never show a
        # different field set than the component form does.
        service = SERVICES[ServiceType.HEALTH_CHECK]
        section = service.config_form_section(ConfigLayer.COMPONENT)
        assert section is not None
        assert section.editables == service.config_component_visualizers()
        assert section.layout == service.config_component_layout()

    def test_no_section_when_the_service_has_no_component_fields(self) -> None:
        assert SERVICES[ServiceType.KEYCLOAK].config_form_section(ConfigLayer.COMPONENT) is None

    def test_section_ids_are_unique_across_the_catalog(self) -> None:
        ids: dict[str, str] = {}
        for service in _SERVICES:
            for layer in ConfigLayer:
                section = service.config_form_section(layer)
                if section is None:
                    continue
                assert section.section_id not in ids, (
                    f"section id '{section.section_id}' is used by both "
                    f"'{ids[section.section_id]}' and '{service.service_type.value}'"
                )
                ids[section.section_id] = service.service_type.value


class TestConfigLayersIsDerivedNotDeclared:
    def test_a_layer_with_editables_counts(self) -> None:
        assert ConfigLayer.COMPONENT in SERVICES[ServiceType.TEMP_STORAGE].config_layers()

    def test_a_layer_with_only_api_fields_counts(self) -> None:
        # minio's deployment layer has no editables, only model-derived API fields.
        minio = SERVICES[ServiceType.MINIO_STORAGE]
        assert not minio.config_editables(ConfigLayer.DEPLOYMENT)
        assert ConfigLayer.DEPLOYMENT in minio.config_layers()

    def test_a_behaviour_only_service_carries_no_config(self) -> None:
        assert SERVICES[ServiceType.PLATFORM].config_layers() == []
