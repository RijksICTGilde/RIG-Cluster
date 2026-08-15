"""send-email service: an SMTP account on the platform mail relay.

Named after what it does, like ``publish-on-web`` -- not ``smtp-mail`` (a protocol name in
a user-facing thing, which we do nowhere else) and emphatically not ``sendmail``, which is
an existing MTA and a well-known command; a service by that name would leave readers
guessing for years which of the three was meant. ``plans/mailrelay.md`` argues it.

What it hands a component is credentials, nothing more: host, port, username, password and
the sender address. Everything that decides whether the mail actually ARRIVES -- the
rewritten envelope sender, the pinned ``From:`` domain, the DKIM signature, the stripped
``Received`` chain -- is enforced on the relay and is deliberately not per project. A
project that could set its own ``From:`` domain would simply produce mail that fails DMARC.

The project-level block is small on purpose: a display name, the local part of the sender
address, an optional own domain (kept in the model so the later domain flow does not need a
schema change) and a daily budget. ``accounts`` is the platform's side and carries
``PLATFORM_MANAGED``.

Deliberately absent, so the next reader does not go looking:

- ``allows_implicit_project_selection`` -- left on the default (False). Ticking send-email
  on a component means the project starts sending mail under the platform's name and eats
  part of the volume agreed with the mail team. That is a project-level decision, so an
  implicit enrolment with an invented sender identity is refused rather than guessed.
- ``config_approvals`` -- no approval: the budget is capped by the model
  (``MAX_MESSAGES_PER_DAY``) and by the relay's global ceiling, so there is nothing for a
  human to weigh per project.
- component/deployment-level config -- an account belongs to the project; a component
  only decides whether it gets the credentials.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from opi.core.cluster_config import (
    get_mail_relay_host,
    get_mail_relay_namespace,
    get_mail_relay_port,
)
from opi.services.catalog.base import (
    ConfigLayer,
    DeploymentManifestContext,
    DeploymentManifestSpec,
    ManifestContext,
    ProvisionContext,
    SecretFileSpec,
    Service,
    config_path,
)
from opi.services.catalog.send_email.config_model import SendEmailConfig
from opi.services.catalog.send_email.variables import SendEmailVariables
from opi.services.services import ServiceDefinition, service_entry_name
from opi.services.services_enums import CleanupStrategy, ManagerKey, ServiceBinding, ServiceType
from opi.utils.naming import generate_network_policy_name, generate_unique_name
from opi.utils.secrets import SendEmailSecret

logger = logging.getLogger(__name__)

#: Pod label the relay's Deployment carries, so the NetworkPolicy peer is that one
#: workload and not the whole RON namespace (which also hosts the VLAM gateway).
RELAY_POD_LABELS = {"app": "rig-mail-relay"}


class SendEmailService(Service):
    service_type = ServiceType.SEND_EMAIL
    definition = ServiceDefinition(
        name="E-mail versturen",
        description=(
            "Verstuur e-mail vanuit je applicatie via de mailrelay van het platform. "
            "Je krijgt een eigen SMTP-account met een eigen dagbudget; het afzenderadres "
            "ligt vast op het maildomein van het platform."
        ),
        help_template="send_email/help.md",
        icon="envelop",
        color="donkerblauw",
        # Per component: elk onderdeel beslist zelf of het de SMTP-gegevens krijgt. De
        # configuratie is er maar een, op projectniveau -- net als bij keycloak.
        binding=ServiceBinding.COMPONENT,
        secret_class="SendEmailSecret",
        variables=[var.value for var in SendEmailVariables],
        cleanup_strategy=CleanupStrategy.IMMEDIATE,
    )
    config_model = SendEmailConfig
    config_schema_version = "1.0"
    cleanup_manager_key = ManagerKey.MAIL
    provision_order = 50
    manifest_secret_class = SendEmailSecret
    # Last of the contributors: appended after the existing envFrom services and the two
    # override services, so no already-rendered manifest changes order because of us.
    manifest_order = 70

    config_section_id: ClassVar[str] = "send-email-config"
    modal_flow_id = "modal-edit-send-email-config"

    def config_api_fields(self, layer: ConfigLayer) -> list[str]:
        if layer is ConfigLayer.PROJECT:
            return self.config_model_field_names()
        return []

    def config_editables(self, layer: ConfigLayer):
        if layer is not ConfigLayer.PROJECT:
            return []
        from opi.services.catalog.send_email.editables import (
            SEND_EMAIL_FROM_LOCAL_PART_EDITABLE,
            SEND_EMAIL_FROM_NAME_EDITABLE,
            SEND_EMAIL_MESSAGES_PER_DAY_EDITABLE,
        )

        return [
            SEND_EMAIL_FROM_NAME_EDITABLE,
            SEND_EMAIL_FROM_LOCAL_PART_EDITABLE,
            SEND_EMAIL_MESSAGES_PER_DAY_EDITABLE,
        ]

    def config_form_section(self, layer: ConfigLayer):
        if layer is not ConfigLayer.PROJECT:
            return super().config_form_section(layer)
        # Cached: consumers compare section identity (EDIT_SECTIONS[...] is X).
        cached = getattr(self, "_config_section_cache", None)
        if cached is None:
            from opi.forms.visualizers.sections import FormSection
            from opi.services.catalog.send_email.visualizers import (
                SEND_EMAIL_FROM_LOCAL_PART,
                SEND_EMAIL_FROM_NAME,
                SEND_EMAIL_MESSAGES_PER_DAY,
            )

            cached = FormSection(
                section_id=self.config_section_id,
                title="E-mail versturen",
                icon="envelop",
                description="Afzender en dagbudget van de e-mail die dit project verstuurt",
                visible=self._config_selected,
                post_save_action="process_project",
                editables=[SEND_EMAIL_FROM_NAME, SEND_EMAIL_FROM_LOCAL_PART, SEND_EMAIL_MESSAGES_PER_DAY],
                layout=[
                    config_path(ConfigLayer.PROJECT, self.service_type, "config", "from-name"),
                    config_path(ConfigLayer.PROJECT, self.service_type, "config", "from-local-part"),
                    config_path(ConfigLayer.PROJECT, self.service_type, "config", "messages-per-day"),
                ],
            )
            self._config_section_cache = cached
        return cached

    def _config_selected(self, project_data: dict[str, Any]) -> bool:
        """Section visibility, derived from this service's own service_type."""
        from opi.services.services import service_entry_name

        return self.service_type.value in [
            service_entry_name(entry) for entry in project_data.get("services", []) or []
        ]

    async def provision(self, ctx: ProvisionContext) -> None:
        await ctx.mail_manager.create_resources_for_deployment(ctx.project_data, ctx.deployment)

    def build_secret_files(self, ctx: ManifestContext) -> list[SecretFileSpec]:
        creds = ctx.get_secret(ctx.deployment_name, ServiceType.SEND_EMAIL.value, SendEmailSecret)
        if creds is None:
            logger.warning(f"Deployment '{ctx.deployment_name}' gebruikt send-email maar heeft geen SMTP-gegevens")
            return []
        # host/port are cluster-specific; the account itself comes from the provisioned creds.
        secret = SendEmailSecret(
            host=get_mail_relay_host(ctx.cluster),
            port=get_mail_relay_port(ctx.cluster),
            username=creds.username,
            password=creds.password,
            from_address=creds.from_address,
        )
        return [
            SecretFileSpec(
                secret_name=SendEmailSecret.get_secret_name(ctx.deployment_name),
                secret_pairs=secret.to_k8s_secret_data(),
                secret_type=ServiceType.SEND_EMAIL.value,
                resolve_aliases=True,
            )
        ]

    def contribute_deployment_manifests(self, ctx: DeploymentManifestContext) -> list[DeploymentManifestSpec]:
        """One egress NetworkPolicy per component that uses the service.

        This is why the policy comes from the service and not from a hand-applied YAML
        (``plans/mailrelay.md``, aanvulling 3): the relay lives in its OWN namespace
        because the Calico egress annotation takes one value, and the tenant baseline
        only opens the operations namespace. Without a rule here a pod resolves the
        relay and then hangs on connect. The service is the only thing that knows it is
        switched on and for which component, so it is the only thing that can keep the
        rule in step -- and the prune removes the file again when it is switched off,
        which is exactly what a hand-applied policy does not do (see the 10 June
        incident, ``project_incident_20260610_netpol``).
        """
        deployment_name = ctx.deployment["name"]
        components = self._components_using_service(ctx)
        if not components:
            return []

        relay_namespace = get_mail_relay_namespace(ctx.cluster)
        relay_port = get_mail_relay_port(ctx.cluster)
        return [
            DeploymentManifestSpec(
                filename=f"{deployment_name}-{self.service_type.value}-{component}-network-policy",
                template_path="service-network-policy.yaml.jinja",
                values={
                    "name": generate_network_policy_name(f"{self.service_type.value}-{component}", deployment_name),
                    "namespace": ctx.namespace,
                    "pod_selector": {"app": generate_unique_name(deployment_name, component)},
                    "ingress": [],
                    "egress": [
                        {
                            "peer": {"namespace": relay_namespace, "pod_labels": RELAY_POD_LABELS},
                            "ports": [relay_port],
                        }
                    ],
                },
            )
            for component in components
        ]

    def _components_using_service(self, ctx: DeploymentManifestContext) -> list[str]:
        """The names of this deployment's components that ticked send-email, sorted.

        Read from the project's component definitions (that is where a component's
        ``services`` list lives), restricted to the components this deployment actually
        rolls out.
        """
        local: set[str] = {
            component.get("reference")
            for component in ctx.deployment.get("components", []) or []
            if isinstance(component, dict) and component.get("reference")
        }
        using: set[str] = set()
        for component in ctx.project_data.get("components", []) or []:
            name = component.get("name")
            if name not in local:
                continue
            names = [service_entry_name(entry) for entry in component.get("services", []) or []]
            if self.service_type.value in names:
                using.add(name)
        return sorted(using)
