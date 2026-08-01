"""cross-domain-access service: per-deployment NetworkPolicies between projects.

This service is about NETWORK access between projects, NOT DNS domains. "Domain" here is
the security perimeter of another project; the DNS side (hostnames, certificates,
subdomain approvals) belongs to publish-on-web. A project uses this service to declare, per
explicitly named port, which other projects/deployments/components may reach its pods
(``inbound``) and where it itself may connect (``outbound``). Each declared rule becomes a
peer in a service-owned NetworkPolicy, additive next to the tenant baseline.

Two config layers, like env-vars:

* PROJECT -- the shared basis, applies to every deployment of the project.
* DEPLOYMENT -- a partial patch keyed on a rule's ``name``: override a field (typically the
  peer ``deployment``), add a new rule, or ``disabled: true`` an inherited one.

The receiver decides. An inbound rule in project A is the permission; project B cannot grant
itself access. B's outbound list is (a) required for ports other than 80/443 and (b)
explicit intent.

Design notes recorded so the next reader does not go looking for something deliberately
absent:

* ``provision`` / ``cleanup_manager_key`` -- none. The effect is entirely in generated
  manifests; the generic service-manifest prune removes the policy files when the service is
  switched off (see ``project_manager._prune_obsolete_service_manifests``).
* ``manifest_secret_class`` / ``build_secret_files`` -- no secret, no envFrom.
* ``config_approvals`` -- none today. Platform-admin approval of an inbound rule is a
  deliberate future step (this is the only service that loosens tenant isolation), but out of
  scope now.
"""

from __future__ import annotations

from typing import Any

from opi.services.catalog.base import ConfigLayer, Service
from opi.services.catalog.cross_domain_access.config_model import CrossDomainAccessConfig
from opi.services.services import service_entry_name
from opi.services.services_enums import ServiceType


class CrossDomainAccessService(Service):
    service_type = ServiceType.CROSS_DOMAIN_ACCESS
    config_model = CrossDomainAccessConfig
    config_schema_version = "1.0"
    config_section_id = "cross-domain-access-config"
    modal_flow_id = "modal-edit-cross-domain-config"

    def _config_selected(self, project_data: dict[str, Any]) -> bool:
        """Section visibility: whether this project selected the cross-domain-access service."""
        return self.service_type.value in [
            service_entry_name(entry) for entry in project_data.get("services", []) or []
        ]

    # --- config field ownership -------------------------------------------------
    # The same config shape lives at the project and the deployment layer (the deployment
    # layer is a patch on the project layer, but the stored shape is identical), so both
    # layers accept the model's fields and validate against config_model.

    def config_api_fields(self, layer: ConfigLayer) -> list[str]:
        if layer in (ConfigLayer.PROJECT, ConfigLayer.DEPLOYMENT):
            return self.config_model_field_names()
        return []
