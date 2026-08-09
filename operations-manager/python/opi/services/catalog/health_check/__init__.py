"""health-check service.

A behaviour-only, component-level service: it provisions nothing and owns no secret.
It overrides how Kubernetes probes the component -- scheme, port and the liveness/
readiness paths -- via ``template_vars`` on its manifest contribution.

Absence is not neutral here, unlike every other service: a component without this
service is still probed, but with a plain TCP probe on its first inbound port (the
platform default set in generic code). This service exists to point that probe at an
HTTP(S) endpoint on a separate port, or to switch probing off with ``scheme: none``.
"""

from __future__ import annotations

from typing import Any

from opi.services.catalog.base import ConfigLayer, ManifestContext, ManifestContribution, Service
from opi.services.catalog.health_check.config_model import HealthCheckConfig
from opi.services.services import ServiceDefinition, service_entry_config, service_entry_name
from opi.services.services_enums import ServiceBinding, ServiceType


class HealthCheckService(Service):
    service_type = ServiceType.HEALTH_CHECK
    definition = ServiceDefinition(
        name="Health check",
        description=(
            "Bepaalt hoe Kubernetes de gezondheid van het component controleert (scheme, poort en "
            "paden). Zonder deze service wordt het component ook gecontroleerd, maar alleen op "
            "TCP-niveau op de eerste inbound-poort. Kies deze service om een HTTP(S)-probe op een "
            "aparte poort en paden te richten, of om probes uit te zetten met scheme: none."
        ),
        help_template="health_check/help.md",
        icon="stethoscoop",
        color="rood",
        binding=ServiceBinding.COMPONENT,
        variables=[],
    )
    config_model = HealthCheckConfig
    config_schema_version = "1.0"
    # Runs after the secret services (10-50). This provider only overrides probe_*
    # template vars that no other service touches, so the order is not load-bearing.
    manifest_order = 55
    config_component_order = 45

    # --- config field ownership (component-level; hooks into the component form) ----
    # health-check is a component-level service: no standalone wizard step. It hooks
    # its probe fieldset into the per-component form via config_component_layout().

    def config_api_fields(self, layer: ConfigLayer) -> list[str]:
        # scheme/port/paths live on the component reference's config wrapper.
        return self.config_model_field_names() if layer is ConfigLayer.COMPONENT else []

    def config_editables(self, layer: ConfigLayer):
        if layer is not ConfigLayer.COMPONENT:
            return []
        from opi.services.catalog.health_check.editables import (
            HEALTH_CHECK_LIVENESS_PATH_EDITABLE,
            HEALTH_CHECK_PORT_EDITABLE,
            HEALTH_CHECK_READINESS_PATH_EDITABLE,
            HEALTH_CHECK_SCHEME_EDITABLE,
        )

        return [
            HEALTH_CHECK_SCHEME_EDITABLE,
            HEALTH_CHECK_PORT_EDITABLE,
            HEALTH_CHECK_LIVENESS_PATH_EDITABLE,
            HEALTH_CHECK_READINESS_PATH_EDITABLE,
        ]

    def config_component_visualizers(self):
        from opi.services.catalog.health_check.visualizers import (
            HEALTH_CHECK_LIVENESS_PATH,
            HEALTH_CHECK_PORT,
            HEALTH_CHECK_READINESS_PATH,
            HEALTH_CHECK_SCHEME,
        )

        return [
            HEALTH_CHECK_SCHEME,
            HEALTH_CHECK_PORT,
            HEALTH_CHECK_LIVENESS_PATH,
            HEALTH_CHECK_READINESS_PATH,
        ]

    def config_component_layout(self):
        from opi.forms.layout import Fieldset

        svc = self.service_type.value
        return [
            Fieldset(
                legend="Health check configuratie",
                depends_on="services",
                show_when={"contains": svc},
                children=[
                    f"services{{{svc}}}/config/scheme",
                    f"services{{{svc}}}/config/port",
                    f"services{{{svc}}}/config/liveness-path",
                    f"services{{{svc}}}/config/readiness-path",
                ],
            )
        ]

    def contribute_manifest_context(self, ctx: ManifestContext) -> ManifestContribution:
        # Override only the probe_* keys the user set; the generic base + template keep
        # the defaults (scheme tcp, port application_port, paths "/") for everything
        # left unset, so a half-filled config never renders a broken probe.
        contribution = super().contribute_manifest_context(ctx)
        component_def = ctx.component_def or {}

        # A component without an inbound port cannot be probed by the kubelet: the
        # generic code already forces scheme=none. The service must not resurrect a
        # probe, so contribute nothing -- "no inbound port -> no probe, even with the
        # service selected".
        inbound_ports = (component_def.get("ports") or {}).get("inbound") or []
        if not inbound_ports:
            return contribution

        config: dict[str, Any] | None = None
        for service_item in component_def.get("services", []):
            if service_entry_name(service_item) == ServiceType.HEALTH_CHECK.value:
                candidate = service_entry_config(service_item)
                if isinstance(candidate, dict):
                    config = candidate
                break
        if not config:
            return contribution

        template_vars = contribution.template_vars
        if config.get("scheme") is not None:
            template_vars["probe_scheme"] = config["scheme"]
        if config.get("port") is not None:
            template_vars["probe_port"] = config["port"]
        if config.get("liveness-path") is not None:
            template_vars["probe_liveness_path"] = config["liveness-path"]
        if config.get("readiness-path") is not None:
            template_vars["probe_readiness_path"] = config["readiness-path"]
        return contribution
