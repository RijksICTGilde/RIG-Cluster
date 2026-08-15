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

**Nothing happens until an administrator says yes** (aanvulling 6 in the plan). The service
declares its approval through the generic mechanism -- the same one publish-on-web uses for
domains -- so the request lands in the existing approver interface and there is no second
screen and no second mechanism. The behaviour per status is deliberately boring: no request,
pending and denied all produce exactly NOTHING (no account, no network policy, no
credentials), and only approved switches everything on at once. A half-working state -- an
account that may not send, a network policy without an account -- is one nobody can explain
and one that gets forgotten at cleanup.

Deliberately absent, so the next reader does not go looking:

- ``allows_implicit_project_selection`` -- left on the default (False). Ticking send-email
  on a component means the project starts sending mail under the platform's name and eats
  part of the volume agreed with the mail team. That is a project-level decision, so an
  implicit enrolment with an invented sender identity is refused rather than guessed.
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
from opi.services.catalog.approval import (
    ApprovalItem,
    ApprovalNotice,
    ApprovalSpec,
    ApprovalStatus,
    ApproverScope,
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

#: Key of this service's one approval, used to route a verdict back to the spec.
APPROVAL_KEY = "send-email"


def _approval_block(project_data: dict[str, Any]) -> dict[str, Any] | None:
    """The stored approval block, or None when nothing was ever requested."""
    for entry in project_data.get("services", []) or []:
        if service_entry_name(entry) != ServiceType.SEND_EMAIL.value:
            continue
        config = entry.get("config") if isinstance(entry, dict) else None
        approval = config.get("approval") if isinstance(config, dict) else None
        return approval if isinstance(approval, dict) else None
    return None


def _status_of(project_data: dict[str, Any], value: Any) -> ApprovalStatus:
    """The CHECK. ``value`` is the project name and is not read: there is ONE approval per
    project, not one per requested thing, because what is approved is "this project may
    send mail" and not a particular address."""
    approval = _approval_block(project_data)
    if approval is None:
        return ApprovalStatus.NONE
    try:
        return ApprovalStatus(approval.get("status", ""))
    except ValueError:
        return ApprovalStatus.NONE


def is_approved(project_data: dict[str, Any]) -> bool:
    """The one gate. Everything the service does hangs off this, so the four things that
    switch on (account, network policy, envFrom secret, secret file) can never disagree --
    which is the half state aanvulling 6 rules out."""
    return _status_of(project_data, project_data.get("name", "")) is ApprovalStatus.APPROVED


def _selected(project_data: dict[str, Any]) -> bool:
    """Whether the project has the service in its services list at all."""
    return ServiceType.SEND_EMAIL.value in [
        service_entry_name(entry) for entry in project_data.get("services", []) or []
    ]


def _items(project_data: dict[str, Any]) -> list[ApprovalItem]:
    """The LIST. One item per project that asked, in the shape the approver UI renders."""
    approval = _approval_block(project_data)
    if approval is None:
        return []
    return [
        {
            "type": APPROVAL_KEY,
            "domain": "",
            "name": project_data.get("name", ""),
            "current_status": approval.get("status", ""),
            "status": "skip",
            "history": approval.get("history", []),
        }
    ]


def _record(project_data: dict[str, Any], item: ApprovalItem, history_entry: dict[str, Any]) -> None:
    """The RECORD. Writes the verdict into the service's own config block."""
    for entry in project_data.get("services", []) or []:
        if service_entry_name(entry) != ServiceType.SEND_EMAIL.value or not isinstance(entry, dict):
            continue
        config = entry.setdefault("config", {})
        if not isinstance(config, dict):
            return
        approval = config.setdefault("approval", {})
        approval["status"] = item.get("status", "skip")
        approval.setdefault("history", []).append(history_entry)
        return


def _notices(project_data: dict[str, Any], deployment: dict[str, Any]) -> list[ApprovalNotice]:
    """The NOTICE. What the project owner is told while the approval is not granted.

    This is the half of aanvulling 6 that is easy to skip and expensive to skip: a service
    that is switched on and silently does nothing is exactly the class of fault that was
    removed from the domain request. So the waiting state is reported per deployment, which
    is what puts it on the project page and in the deployment read endpoints.
    """
    if not _selected(project_data):
        return []
    status = _status_of(project_data, project_data.get("name", ""))
    if status is ApprovalStatus.APPROVED:
        return []
    consequence = (
        "Er is nog geen SMTP-account, geen netwerktoegang naar de relay en geen SMTP_-variabelen in deze deployment."
    )
    if status is ApprovalStatus.DENIED:
        text = f"Het versturen van e-mail is afgewezen. {consequence}"
    elif status is ApprovalStatus.REQUESTED:
        text = f"Het versturen van e-mail is aangevraagd en wacht op goedkeuring. {consequence}"
    else:
        text = f"Het versturen van e-mail is nog niet aangevraagd. {consequence}"
    history = (_approval_block(project_data) or {}).get("history") or []
    verdict = history[-1] if isinstance(history, list) and history and isinstance(history[-1], dict) else {}
    return [
        {
            "subject": project_data.get("name", ""),
            "status": status.value,
            "text": text,
            "by": verdict.get("by"),
            "date": verdict.get("date"),
            "message": verdict.get("message"),
        }
    ]


def _request_approval_on_save(project_data: dict[str, Any], wizard_data: dict[str, Any]) -> None:
    """Hook op de configuratiestap: leg de aanvraag vast zodra de dienst is aangezet."""
    from opi.services.approvals import ensure_approval_requests

    ensure_approval_requests(project_data)


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
                description=(
                    "Afzender en dagbudget van de e-mail die dit project verstuurt. "
                    "Een beheerder moet het versturen eerst goedkeuren; tot die tijd "
                    "krijgt je applicatie geen SMTP-gegevens."
                ),
                visible=self._config_selected,
                post_save_action="process_project",
                editables=[SEND_EMAIL_FROM_NAME, SEND_EMAIL_FROM_LOCAL_PART, SEND_EMAIL_MESSAGES_PER_DAY],
                # Doorlopen van deze stap IS de aanvraag. Via de generieke functie en niet
                # via de eigen hook, zodat er geen tweede weg naar een aanvraag ontstaat;
                # de API-kant roept dezelfde functie aan (project_manager, config-schrijf).
                post_merge=_request_approval_on_save,
                layout=[
                    config_path(ConfigLayer.PROJECT, self.service_type, "config", "from-name"),
                    config_path(ConfigLayer.PROJECT, self.service_type, "config", "from-local-part"),
                    config_path(ConfigLayer.PROJECT, self.service_type, "config", "messages-per-day"),
                ],
            )
            self._config_section_cache = cached
        return cached

    def _config_selected(self, project_data: dict[str, Any]) -> bool:
        """Section visibility: the same question as everywhere else in this module."""
        return _selected(project_data)

    def config_approvals(self, layer: ConfigLayer):
        """One approval per project: may this project send mail at all.

        Declared through the generic mechanism (``opi/services/catalog/approval.py``), so
        the request shows up in the SAME approver interface as a domain request and the
        waiting state comes back through the same API field. No second screen.
        """
        if layer is not ConfigLayer.PROJECT:
            return []
        return [
            ApprovalSpec(
                key=APPROVAL_KEY,
                label="E-mail versturen",
                approver=ApproverScope.PLATFORM_ADMIN,
                status_of=_status_of,
                list_items=_items,
                record=_record,
                notices_for=_notices,
            )
        ]

    def ensure_approval_requests(self, project_data: dict[str, Any]) -> None:
        """Switching the service on IS the request.

        State-shaped rather than event-shaped, as the contract asks: read the project as
        it stands and add what is missing. So asking through the API lands on the same
        request the wizard's tick creates, and running it twice changes nothing.
        """
        if not _selected(project_data):
            return
        if _approval_block(project_data) is not None:
            return
        for entry in project_data.get("services", []) or []:
            if service_entry_name(entry) != ServiceType.SEND_EMAIL.value or not isinstance(entry, dict):
                continue
            config = entry.setdefault("config", {})
            if isinstance(config, dict):
                config["approval"] = {"status": ApprovalStatus.REQUESTED.value, "history": []}
            return

    async def provision(self, ctx: ProvisionContext) -> None:
        await ctx.mail_manager.create_resources_for_deployment(ctx.project_data, ctx.deployment)

    def contribute_manifest_context(self, ctx: ManifestContext):
        """No approval, no envFrom secret and no secret file.

        Gated here as well as in the manager, and that is not belt-and-braces: the
        manifest path runs even when provisioning did nothing, so without this a
        deployment would reference a secret that was never written.
        """
        from opi.services.catalog.base import ManifestContribution

        if not is_approved(ctx.project_data):
            return ManifestContribution()
        return super().contribute_manifest_context(ctx)

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
        # Not approved means no rule, exactly as it means no account: a network policy
        # towards a relay the project has no account on is the half state aanvulling 6
        # rules out. The prune keys on the filename, so an approval that is withdrawn
        # takes the file with it on the next run.
        if not is_approved(ctx.project_data):
            return []

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
