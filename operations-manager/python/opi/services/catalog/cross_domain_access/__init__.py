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

import logging
from typing import Any, ClassVar

from opi.services.catalog.base import (
    ConfigLayer,
    DeploymentManifestContext,
    DeploymentManifestSpec,
    Service,
    config_path,
)
from opi.services.catalog.cross_domain_access.config_model import CrossDomainAccessConfig
from opi.services.catalog.cross_domain_access.merge import IncompleteRuleError, merge_rules, to_merged_rule
from opi.services.catalog.cross_domain_access.resolve import ResolvedRule, resolve_rules
from opi.services.services import service_entry_config, service_entry_name
from opi.services.services_enums import ServiceType
from opi.utils.naming import generate_network_policy_name, generate_unique_name

logger = logging.getLogger(__name__)


def _direction_rules(config: Any, direction: str) -> list[dict]:
    """The raw ``inbound``/``outbound`` rule dicts from a stored config block, or []."""
    if not isinstance(config, dict):
        return []
    return [rule for rule in (config.get(direction) or []) if isinstance(rule, dict)]


def _group_by_peer(rules: list[ResolvedRule]) -> list[dict]:
    """Fold rules with the same peer into one entry with a sorted ``ports`` list.

    Two rules from one own component to the same peer become one policy peer entry with two
    ports, so the rendered YAML stays compact. Sorted for a stable render.
    """
    groups: dict[tuple, dict] = {}
    for rule in rules:
        key = (rule.peer.namespace, tuple(sorted(rule.peer.pod_labels.items())))
        entry = groups.setdefault(
            key, {"peer": {"namespace": rule.peer.namespace, "pod_labels": rule.peer.pod_labels}, "ports": []}
        )
        if rule.port not in entry["ports"]:
            entry["ports"].append(rule.port)
    result = list(groups.values())
    for entry in result:
        entry["ports"].sort()
    result.sort(key=lambda entry: (entry["peer"]["namespace"], sorted(entry["peer"]["pod_labels"].items())))
    return result


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

    def config_editables(self, layer: ConfigLayer):
        if layer is not ConfigLayer.PROJECT:
            return []
        from opi.services.catalog.cross_domain_access.editables import CROSS_DOMAIN_EDITABLES

        return CROSS_DOMAIN_EDITABLES

    # The deployment layer is a per-deployment PATCH on the project rules (see merge.py).
    # It is user config, not OPI state, but it has no form yet: the project-level rule
    # editor is the whole UI today and a patch editor is its own piece of work. Declared
    # rather than left silent so the gap is visible instead of looking like an oversight.
    form_exempt_layers: ClassVar[dict[ConfigLayer, str]] = {
        ConfigLayer.DEPLOYMENT: (
            "per-deployment patch on the project rules; alleen via API/projectbestand, nog geen formulier (RC-25)"
        )
    }

    def config_form_section(self, layer: ConfigLayer):
        if layer is not ConfigLayer.PROJECT:
            return super().config_form_section(layer)
        cached = getattr(self, "_config_section_cache", None)
        if cached is None:
            from opi.forms.layout import Fieldset, Sequence
            from opi.forms.visualizers.sections import FormSection
            from opi.services.catalog.cross_domain_access.visualizers import CROSS_DOMAIN_VISUALIZERS

            def cp(*segments: str) -> str:
                return config_path(ConfigLayer.PROJECT, self.service_type, "config", *segments)

            cached = FormSection(
                section_id="cross-domain-access-config",
                title="Cross-domain toegang",
                icon="netwerk",
                description=(
                    "Netwerktoegang tussen projecten (geen DNS-domeinen). De ontvanger bepaalt wie binnen mag; "
                    "zet aan jouw kant ook de uitgaande regel, anders is alleen verkeer op poort 80 en 443 mogelijk."
                ),
                visible=self._config_selected,
                post_save_action="process_project",
                editables=CROSS_DOMAIN_VISUALIZERS,
                layout=[
                    Fieldset(
                        legend="Inkomend (wie mag bij mij binnen)",
                        description="Elke regel is een toestemming: de ontvanger bepaalt wie toegang krijgt.",
                        children=[Sequence(field_name=cp("inbound"))],
                    ),
                    Fieldset(
                        legend="Uitgaand (waar mag ik heen)",
                        description="Nodig voor poorten buiten 80/443 en als expliciete intentie.",
                        children=[Sequence(field_name=cp("outbound"))],
                    ),
                ],
            )
            self._config_section_cache = cached
        return cached

    # --- config reading ---------------------------------------------------------

    def _project_config(self, project_data: dict[str, Any]) -> Any:
        """This service's project-level config block, or None if not selected."""
        for entry in project_data.get("services", []) or []:
            if service_entry_name(entry) == self.service_type.value:
                return service_entry_config(entry)
        return None

    def _deployment_config(self, deployment: dict[str, Any]) -> Any:
        """This service's deployment-level config block on ``deployment``, or None."""
        for entry in deployment.get("services", []) or []:
            if service_entry_name(entry) == self.service_type.value:
                return service_entry_config(entry)
        return None

    # --- effect: per-deployment NetworkPolicies ---------------------------------

    def _resolve_direction(
        self,
        direction: str,
        project_config: Any,
        deployment_config: Any,
        local_components: set[str | None],
        ctx: DeploymentManifestContext,
    ) -> list[ResolvedRule]:
        """Merge, validate and resolve one direction's rules for this deployment.

        Rules whose own component is not in this deployment, or whose peer deployment is
        still open after the merge, or that reference something that does not exist, are
        logged and skipped -- generation never fails on a broken rule.
        """
        from opi.services.project_store import get_project_store

        def lookup(name: str) -> dict | None:
            summary = get_project_store().get(name)
            return summary.data if summary is not None else None

        merged = merge_rules(
            _direction_rules(project_config, direction), _direction_rules(deployment_config, direction)
        )
        normalized = []
        deployment_name = ctx.deployment.get("name")
        for rule_dict in merged:
            name = rule_dict.get("name")
            try:
                rule = to_merged_rule(rule_dict, direction=direction)
            except IncompleteRuleError as error:
                logger.warning(str(error))
                continue
            if rule is None:
                logger.warning(
                    "cross-domain %s-regel '%s': peer-deployment niet ingesteld voor deployment '%s', overgeslagen",
                    direction,
                    name,
                    deployment_name,
                )
                continue
            if rule.local_component not in local_components:
                logger.warning(
                    "cross-domain %s-regel '%s': eigen component '%s' zit niet in deployment '%s', overgeslagen",
                    direction,
                    rule.name,
                    rule.local_component,
                    deployment_name,
                )
                continue
            normalized.append(rule)
        return resolve_rules(normalized, cluster=ctx.cluster, self_project=ctx.project_name, lookup_project=lookup)

    def contribute_deployment_manifests(self, ctx: DeploymentManifestContext) -> list[DeploymentManifestSpec]:
        project_config = self._project_config(ctx.project_data)
        deployment_config = self._deployment_config(ctx.deployment)
        if project_config is None and deployment_config is None:
            return []

        local_components: set[str | None] = {
            component.get("reference")
            for component in ctx.deployment.get("components", []) or []
            if isinstance(component, dict)
        }

        ingress = self._resolve_direction("inbound", project_config, deployment_config, local_components, ctx)
        egress = self._resolve_direction("outbound", project_config, deployment_config, local_components, ctx)

        deployment_name = ctx.deployment["name"]
        specs: list[DeploymentManifestSpec] = []
        components = sorted({rule.local_component for rule in ingress} | {rule.local_component for rule in egress})
        for component in components:
            component_ingress = _group_by_peer([rule for rule in ingress if rule.local_component == component])
            component_egress = _group_by_peer([rule for rule in egress if rule.local_component == component])
            # No peers means no file: let the prune remove any stale one (2.8).
            if not component_ingress and not component_egress:
                continue
            specs.append(
                DeploymentManifestSpec(
                    filename=f"{deployment_name}-{self.service_type.value}-{component}-network-policy",
                    template_path="service-network-policy.yaml.jinja",
                    values={
                        "name": generate_network_policy_name(f"{self.service_type.value}-{component}", deployment_name),
                        "namespace": ctx.namespace,
                        "pod_selector": {"app": generate_unique_name(deployment_name, component)},
                        "ingress": component_ingress,
                        "egress": component_egress,
                    },
                )
            )
        return specs
