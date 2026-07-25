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

from typing import Any

from opi.services.catalog.approval import ApprovalSpec, ApprovalStatus, ApproverScope
from opi.services.catalog.base import ConfigLayer, Service
from opi.services.services_enums import ServiceType


def _to_status(stored: str | None) -> ApprovalStatus:
    """Map a persisted status string onto ApprovalStatus; anything else -> NONE."""
    try:
        return ApprovalStatus(stored)
    except ValueError:
        return ApprovalStatus.NONE


def _domain_status(project_data: dict[str, Any], value: Any) -> ApprovalStatus:
    """Approval status of a requested domain (``value`` is the domain string).

    Reads the stored state via the existing pure predicate -- no rules duplicated here.
    """
    from opi.connectors.subdomain import get_project_allowed_domain_config

    cfg = get_project_allowed_domain_config(project_data, value)
    if not isinstance(cfg, dict):
        return ApprovalStatus.NONE
    return _to_status(cfg.get("status"))


def _subdomain_status(project_data: dict[str, Any], value: Any) -> ApprovalStatus:
    """Approval status of a requested subdomain (``value`` is a ``(domain, subdomain)``)."""
    from opi.connectors.subdomain import get_subdomain_status

    domain, subdomain = value
    return _to_status(get_subdomain_status(project_data, domain, subdomain))


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

    def config_approvals(self, layer: ConfigLayer):
        # A deployment's requested domain / subdomain needs platform-admin approval
        # before ingress is generated for it. The rule (status_of) reuses the existing
        # pure predicates; the state still lives in the project's ``domains:`` block
        # (moving it under this service is a separate schema+data migration).
        if layer is not ConfigLayer.DEPLOYMENT:
            return []
        return [
            ApprovalSpec(
                key="domain",
                label="Domein",
                approver=ApproverScope.PLATFORM_ADMIN,
                status_of=_domain_status,
            ),
            ApprovalSpec(
                key="subdomain",
                label="Subdomein",
                approver=ApproverScope.PLATFORM_ADMIN,
                status_of=_subdomain_status,
            ),
        ]

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
