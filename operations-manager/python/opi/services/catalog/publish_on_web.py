"""publish-on-web service.

Owns its component-level config (TLS mode + attachment) that it hooks into the
per-component form. NOTE: the rest of the publish-on-web / domain feature is
deliberately NOT here -- it is cross-project platform infrastructure, not per-service
config: the deployment-level "Webadres" domain wizard (DOMAIN_SECTION), the
root-level domain-approval state (`domains:` on the project), the cross-project admin
approver (router_subdomain_admin), the global subdomain registry
(connectors/subdomain.py), and ingress generation (project_manager / naming.py). This
service depends on those; it does not own them.
"""

from __future__ import annotations

from opi.services.catalog.base import ConfigLayer, Service
from opi.services.services_enums import ServiceType


class PublishOnWebService(Service):
    service_type = ServiceType.PUBLISH_ON_WEB
    config_component_order = 30

    # Component-level config: TLS mode + attachment. No config_model yet (tls/attachment
    # are not modelled as Pydantic), so config_api_fields stays default.

    def config_editables(self, layer: ConfigLayer):
        if layer is not ConfigLayer.COMPONENT:
            return []
        from opi.forms.editables.fields.components import (
            PUBLISH_ON_WEB_ATTACHMENT_EDITABLE,
            PUBLISH_ON_WEB_TLS_EDITABLE,
        )

        return [PUBLISH_ON_WEB_TLS_EDITABLE, PUBLISH_ON_WEB_ATTACHMENT_EDITABLE]

    def config_component_layout(self):
        from opi.forms.layout import Fieldset

        svc = self.service_type.value
        return [
            Fieldset(
                legend="Publicatie op het web",
                depends_on="services",
                show_when={"contains": svc},
                children=[f"services{{{svc}}}/config/tls", f"services{{{svc}}}/config/attachment"],
            )
        ]
