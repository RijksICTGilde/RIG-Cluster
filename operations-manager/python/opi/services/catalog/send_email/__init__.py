"""send-email service: an SMTP account on the platform mail relay.

What it hands a component is credentials, nothing more: host, port, username, password and
the sender address. Everything that decides whether the mail actually ARRIVES -- the
envelope sender, the whole ``From:`` header, the stripped ``Received`` chain -- is enforced
on the relay and is never the application's to choose. A project that could set its own
``From:`` DOMAIN would simply produce mail that fails DMARC, so the domain stays ours.

The project-level block is small on purpose: a display name, a sender address and a daily
budget. Two of those do not take effect by being saved: a platform administrator approves
THAT VALUE first. For the address that holds always; for the budget only above the platform
default, because lowering your own ceiling asks nobody. Until the address is approved (and
again after a withdrawal) the platform composes it from the account name
(``noreply-rijksapp+<project>-<deployment>-<hash>@rijksoverheid.nl``) and the relay derives
it itself. The envelope keeps that composed address either way, so a bounce stays traceable;
the relay adds an ``X-ZAD-Project`` header so a message stays traceable to a project even
when the ``From:`` no longer says so. The approval blocks are the platform's side and carry
``PLATFORM_MANAGED``.

The same three fields exist on the DEPLOYMENT layer, because the account is per deployment
and so is its budget: a test deployment sends its hundred messages a day without anyone
approving anything, production its hundred thousand after approval. A deployment that says
nothing inherits the project's values, including an approved address or budget, without a
request of its own. A deployment that overrides asks for ITSELF: its approval is separate
from the project's, so it keeps standing when the project lowers its own value, and until it
is granted the inherited value keeps applying.

**Nothing happens until an administrator says yes.** The service declares its approval
through the generic mechanism -- the same one publish-on-web uses for domains -- so the
request lands in the existing approver interface and there is no second screen and no second
mechanism. The behaviour per status is deliberately boring: no request, pending and denied
all produce exactly NOTHING (no account, no network policy, no credentials), and only
approved switches everything on at once. A half-working state -- an account that may not
send, a network policy without an account -- is one nobody can explain and one that gets
forgotten at cleanup.

Deliberately absent, so the next reader does not go looking:

- ``allows_implicit_project_selection`` -- left on the default (False). Ticking send-email
  on a component means the project starts sending mail under the platform's name and eats
  part of the volume agreed with the mail team. That is a project-level decision, so an
  implicit enrolment with an invented sender identity is refused rather than guessed.
- component-level config -- the account belongs to the deployment; a component only
  decides whether it gets the credentials.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar

from opi.core.cluster_config import (
    CLUSTER_CONFIG,
    get_mail_from_address,
    get_mail_relay_host,
    get_mail_relay_namespace,
    get_mail_relay_port,
)
from opi.core.config import settings
from opi.services.catalog.approval import config_value_approval, deployment_value_approval, service_use_approval
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
from opi.services.catalog.send_email.actions import send_email_actions
from opi.services.catalog.send_email.config_model import SendEmailConfig, SendEmailDeploymentConfig
from opi.services.catalog.send_email.variables import SendEmailVariables
from opi.services.project import Project
from opi.services.secrets import SendEmailSecret
from opi.services.services import ServiceDefinition, service_entry_config, service_entry_name
from opi.services.services_enums import CleanupStrategy, ManagerKey, ServiceType
from opi.utils.naming import (
    generate_deployment_mail_sender_address,
    generate_network_policy_name,
    generate_unique_name,
)

if TYPE_CHECKING:
    from pydantic import BaseModel

    from opi.forms.editables.editable import Editable
    from opi.forms.visualizers.sections import FormSection
    from opi.services.catalog.approval import ApprovalSpec

logger = logging.getLogger(__name__)

#: Pod label the relay's Deployment carries, so the NetworkPolicy peer is that one
#: workload and not the whole RON namespace (which also hosts the VLAM gateway).
RELAY_POD_LABELS = {"app": "rig-mail-relay"}

#: The relay's POD port (containerPort ``submission``), for the egress NetworkPolicy.
#: NOT ``get_mail_relay_port()``: that is the Service port (587), which is what an
#: application dials and what goes into SMTP_PORT. A NetworkPolicy is evaluated AFTER
#: the Service DNAT, so it sees the pod port; a rule on 587 matches nothing and the
#: connect times out. Same trap as the relay -> sink hop (1025, not 25) and the ingress
#: side of the relay's own policy (2525). Change the containerPort in
#: mail/controller/base/deployment.yaml, change this too.
RELAY_POD_PORT = 2525

#: This service's approval, in the shared shape: ONE decision per project about whether it
#: may use the service. Everything that shape needs (reading the status, the item for the
#: approver page, recording the verdict, the notice to the requester and the request itself)
#: comes from ``service_use_approval``; what is left here is only WHAT gets approved and what
#: it means until it does.
APPROVAL = service_use_approval(
    ServiceType.SEND_EMAIL,
    label="E-mail versturen",
    activity="Het versturen van e-mail",
    consequence=(
        "Er is nog geen SMTP-account, geen netwerktoegang naar de relay en geen SMTP_-variabelen in deze deployment."
    ),
)

#: THE GUARD. Everything that switches the service on hangs off this, so the four things that
#: go on at once (account, network policy, envFrom secret, secret file) can never disagree in a
#: half state.
is_approved = APPROVAL.is_approved


#: Key of the display name in the service's config block.
FROM_NAME_KEY = "from-name"

#: Key of the requested sender address in the service's config block.
FROM_ADDRESS_KEY = "from-address"

#: Heading of the address request on the approver page. The shape comes from
#: ``config_value_approval``: the approval hangs on the VALUE, so an approved address the
#: project changes afterwards is a new request.
FROM_ADDRESS_APPROVAL_LABEL = "Afzenderadres"


def _selected(project_data: dict[str, Any]) -> bool:
    """Whether the project has the service in its services list at all."""
    return ServiceType.SEND_EMAIL.value in [
        service_entry_name(entry) for entry in project_data.get("services", []) or []
    ]


def _request_approval_on_save(project_data: dict[str, Any], wizard_data: dict[str, Any]) -> None:
    """Hook on the config step: record the request as soon as the service is switched on."""
    from opi.services.approvals import ensure_approval_requests

    ensure_approval_requests(project_data)


def _service_entry(project_data: dict[str, Any]) -> dict[str, Any] | None:
    """This service's service entry, when the project holds it as a dict."""
    for entry in project_data.get("services", []) or []:
        if service_entry_name(entry) == ServiceType.SEND_EMAIL.value and isinstance(entry, dict):
            return entry
    return None


def _config(project_data: dict[str, Any]) -> dict[str, Any]:
    """The service's config block, empty when there is none."""
    entry = _service_entry(project_data)
    config = entry.get("config") if entry else None
    return config if isinstance(config, dict) else {}


def _requested_from_address(project_data: dict[str, Any]) -> str:
    """The address the project asks for, or empty for the derived address."""
    return str(_config(project_data).get(FROM_ADDRESS_KEY) or "").strip()


def _from_address_consequence(project_data: dict[str, Any], deployment: dict[str, Any]) -> str:
    """Which address the mail leaves under meanwhile.

    Without this the requester knows they are waiting, but not which address their mail goes
    out under.
    """
    # A cluster name the platform does not know is schema valid (``project_validation`` skips
    # such a deployment), and this notice is informational: without this check an unknown name
    # took down the whole project page with a ValueError. Same shape as
    # ``display_blocks._get_default_domain``.
    cluster = deployment.get("cluster") or settings.CLUSTER_MANAGER
    if cluster not in CLUSTER_CONFIG:
        return ""
    derived = generate_deployment_mail_sender_address(
        get_mail_from_address(cluster), project_data.get("name", ""), deployment.get("name", "")
    )
    return f"Je post vertrekt onder {derived}." if derived else ""


FROM_ADDRESS_APPROVAL = config_value_approval(
    ServiceType.SEND_EMAIL,
    field=FROM_ADDRESS_KEY,
    label=FROM_ADDRESS_APPROVAL_LABEL,
    requested_value=_requested_from_address,
    activity=lambda address: f"Het afzenderadres {address}",
    consequence=_from_address_consequence,
    subject=lambda _project_data, address: address,
)

#: THE GUARD on the write path: the approved sender address, or empty.
#:
#: Empty means the relay derives the address from the account name, and it immediately erases
#: an address that stood there before. That way a withdrawal needs no trigger of its own: every
#: run rewrites what holds now.
approved_from_address = FROM_ADDRESS_APPROVAL.approved_value


#: Key of the requested daily budget in the service's config block.
MESSAGES_PER_DAY_KEY = "messages-per-day"

#: Heading of the budget request on the approver page.
MESSAGES_PER_DAY_APPROVAL_LABEL = "Dagbudget e-mail"


def _chosen_messages_per_day(project_data: dict[str, Any]) -> int | None:
    """The number the project filled in itself, or None for the default."""
    value = _config(project_data).get(MESSAGES_PER_DAY_KEY)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _last_approved_number(project_data: dict[str, Any]) -> str:
    """The last approved number as text, empty when there is no usable number.

    ``isdigit`` is also true for digit characters outside ASCII, such as a superscript two,
    where ``int()`` falls over; adding ``isascii`` keeps only a plain decimal number.
    """
    last = MESSAGES_PER_DAY_APPROVAL.last_approved_value(project_data)
    return last if last.isdigit() and last.isascii() else ""


def _granted_messages_per_day(project_data: dict[str, Any]) -> int:
    """The highest number the project may use without a new approval.

    The default, or higher once an administrator approved such a number: within what it is
    already allowed the project chooses for itself.
    """
    default = settings.MAIL_PROJECT_DEFAULT_MESSAGES_PER_DAY
    last = _last_approved_number(project_data)
    return max(default, int(last)) if last else default


def _requested_messages_per_day(project_data: dict[str, Any]) -> str:
    """The number an administrator has to judge. Empty is no request.

    Lowering asks nothing, not even above the default: when the project stays within an
    earlier approved number this returns that number, so the existing approval stays. Were
    this empty, the approval block would disappear and with it the last approved number, and
    the project would fall back on the default.
    """
    chosen = _chosen_messages_per_day(project_data)
    if chosen is None:
        return ""
    if chosen > _granted_messages_per_day(project_data):
        return str(chosen)
    return _last_approved_number(project_data)


def effective_messages_per_day(project_data: dict[str, Any]) -> int:
    """THE GUARD on the write path: the daily budget the relay really enforces.

    An increase only applies when an administrator approved EXACTLY THAT number. Until then
    what the project was already allowed applies: an open request never lowers what held, and a
    decrease within that number applies at once.
    """
    chosen = _chosen_messages_per_day(project_data)
    if chosen is None:
        return settings.MAIL_PROJECT_DEFAULT_MESSAGES_PER_DAY
    if MESSAGES_PER_DAY_APPROVAL.spec.is_approved(project_data, str(chosen)):
        return chosen
    return min(chosen, _granted_messages_per_day(project_data))


def _messages_per_day_consequence(project_data: dict[str, Any], deployment: dict[str, Any]) -> str:
    """How much the project may send meanwhile."""
    return f"Je verstuurt voorlopig hoogstens {effective_messages_per_day(project_data)} berichten per dag."


def _messages_per_day_subject(project_data: dict[str, Any], value: str) -> str:
    """What the approver page shows: the requested number and what holds now."""
    return f"{value} berichten per dag (nu {effective_messages_per_day(project_data)})"


MESSAGES_PER_DAY_APPROVAL = config_value_approval(
    ServiceType.SEND_EMAIL,
    field=MESSAGES_PER_DAY_KEY,
    label=MESSAGES_PER_DAY_APPROVAL_LABEL,
    requested_value=_requested_messages_per_day,
    activity=lambda count: f"Een dagbudget van {count} berichten per dag",
    consequence=_messages_per_day_consequence,
    subject=_messages_per_day_subject,
)


# The deployment layer: the same three fields, but of ONE deployment, because the account
# belongs to the deployment. What a deployment does not fill in it inherits; what it does fill
# in is a request of its own.


def _deployment_config(project_data: dict[str, Any], deployment_name: str) -> dict[str, Any]:
    """The service's config block ON THIS DEPLOYMENT, empty when there is none."""
    deployment = Project(project_data).find("deployments", name=deployment_name) or {}
    for entry in deployment.get("services", []) or []:
        if service_entry_name(entry) == ServiceType.SEND_EMAIL.value:
            config = service_entry_config(entry)
            return config if isinstance(config, dict) else {}
    return {}


def _deployment_requested_from_address(project_data: dict[str, Any], deployment_name: str) -> str:
    """The address this deployment asks for. Every override is a request, as at project level:
    a self-chosen sender is a spoofing button."""
    return str(_deployment_config(project_data, deployment_name).get(FROM_ADDRESS_KEY) or "").strip()


def _deployment_from_address_consequence(project_data: dict[str, Any], deployment: dict[str, Any]) -> str:
    """Which address this deployment's mail leaves under meanwhile."""
    cluster = deployment.get("cluster") or settings.CLUSTER_MANAGER
    if cluster not in CLUSTER_CONFIG:
        return ""
    meanwhile = deployment_from_address(
        project_data, deployment.get("name", "")
    ) or generate_deployment_mail_sender_address(
        get_mail_from_address(cluster), project_data.get("name", ""), deployment.get("name", "")
    )
    return f"Deze deployment verstuurt voorlopig onder {meanwhile}." if meanwhile else ""


DEPLOYMENT_FROM_ADDRESS_APPROVAL = deployment_value_approval(
    ServiceType.SEND_EMAIL,
    field=FROM_ADDRESS_KEY,
    label=f"{FROM_ADDRESS_APPROVAL_LABEL} per deployment",
    requested_value=_deployment_requested_from_address,
    activity=lambda address: f"Het afzenderadres {address}",
    consequence=_deployment_from_address_consequence,
    subject=lambda _project_data, deployment_name, address: f"{address} ({deployment_name})",
)


def deployment_from_address(project_data: dict[str, Any], deployment_name: str) -> str:
    """THE GUARD on the write path: the sender address the relay pins for this deployment.

    The override last approved for THIS deployment, else the project's approved address, else
    empty, and empty means the relay derives the address from the account name. An open request
    therefore changes nothing about what already held.
    """
    return (
        DEPLOYMENT_FROM_ADDRESS_APPROVAL.approved_value(project_data, deployment_name)
        or DEPLOYMENT_FROM_ADDRESS_APPROVAL.last_approved_value(project_data, deployment_name)
        or approved_from_address(project_data)
    )


def _deployment_chosen_messages_per_day(project_data: dict[str, Any], deployment_name: str) -> int | None:
    """The number this deployment filled in itself, or None when it inherits."""
    value = _deployment_config(project_data, deployment_name).get(MESSAGES_PER_DAY_KEY)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _deployment_last_approved_messages_per_day(project_data: dict[str, Any], deployment_name: str) -> int | None:
    """The number last approved for this deployment, or None.

    ``isdigit`` is also true for digit characters outside ASCII, where ``int()`` falls over;
    adding ``isascii`` keeps only a plain decimal number.
    """
    last = DEPLOYMENT_MESSAGES_PER_DAY_APPROVAL.last_approved_value(project_data, deployment_name)
    return int(last) if last.isdigit() and last.isascii() else None


def _deployment_requested_messages_per_day(project_data: dict[str, Any], deployment_name: str) -> str:
    """The number an administrator has to judge for this deployment. Empty is no request.

    The bound is the PLATFORM DEFAULT and not the project value: an override's approval stands
    apart from the project's, so it keeps standing when the project lowers its own number
    later. Up to and including the default the deployment chooses for itself.

    When the deployment stays within the default while something was approved earlier, this
    returns that approved number: were this empty, the approval block would disappear and with
    it the memory of what it was already allowed.
    """
    chosen = _deployment_chosen_messages_per_day(project_data, deployment_name)
    if chosen is None:
        return ""
    if chosen > settings.MAIL_PROJECT_DEFAULT_MESSAGES_PER_DAY:
        return str(chosen)
    last = _deployment_last_approved_messages_per_day(project_data, deployment_name)
    return "" if last is None else str(last)


def _deployment_messages_per_day_consequence(project_data: dict[str, Any], deployment: dict[str, Any]) -> str:
    """How much this deployment may send meanwhile."""
    count = deployment_messages_per_day(project_data, deployment.get("name", ""))
    return f"Deze deployment verstuurt voorlopig hoogstens {count} berichten per dag."


def _deployment_messages_per_day_subject(project_data: dict[str, Any], deployment_name: str, value: str) -> str:
    """What the approver page shows: the requested number, the deployment and what holds now."""
    current = deployment_messages_per_day(project_data, deployment_name)
    return f"{value} berichten per dag voor {deployment_name} (nu {current})"


DEPLOYMENT_MESSAGES_PER_DAY_APPROVAL = deployment_value_approval(
    ServiceType.SEND_EMAIL,
    field=MESSAGES_PER_DAY_KEY,
    label=f"{MESSAGES_PER_DAY_APPROVAL_LABEL} per deployment",
    requested_value=_deployment_requested_messages_per_day,
    activity=lambda count: f"Een dagbudget van {count} berichten per dag",
    consequence=_deployment_messages_per_day_consequence,
    subject=_deployment_messages_per_day_subject,
)


def deployment_messages_per_day(project_data: dict[str, Any], deployment_name: str) -> int:
    """THE GUARD on the write path: the daily budget the relay enforces for this deployment.

    Four cases, in this order:

    * the deployment fills in nothing: the project's number (which has a guard of its own);
    * it fills in the platform default or less: that number, because that asks nobody;
    * its number is approved for EXACTLY that value: that number;
    * otherwise: its last approved override, and without one the inherited number. An open
      request never lowers what already held.
    """
    inherited = effective_messages_per_day(project_data)
    chosen = _deployment_chosen_messages_per_day(project_data, deployment_name)
    if chosen is None:
        return inherited
    if chosen <= settings.MAIL_PROJECT_DEFAULT_MESSAGES_PER_DAY:
        return chosen
    if DEPLOYMENT_MESSAGES_PER_DAY_APPROVAL.is_approved(project_data, deployment_name):
        return chosen
    last = _deployment_last_approved_messages_per_day(project_data, deployment_name)
    return inherited if last is None else last


def project_from_name(project_data: dict[str, Any]) -> str:
    """The project's display name, inherited by every deployment without one of its own."""
    return str(_config(project_data).get(FROM_NAME_KEY) or "").strip()


def deployment_from_name(project_data: dict[str, Any], deployment_name: str) -> str:
    """This deployment's display name: its own, else the project's.

    Free, as at project level: a name asks nobody. Empty is a valid outcome and means the mail
    leaves without a name.
    """
    own = str(_deployment_config(project_data, deployment_name).get(FROM_NAME_KEY) or "").strip()
    return own or project_from_name(project_data)


class SendEmailService(Service):
    service_type = ServiceType.SEND_EMAIL
    definition = ServiceDefinition(
        name="E-mail versturen",
        description=(
            "Verstuur e-mail vanuit je applicatie via de mailrelay van het platform. "
            "Je krijgt een eigen SMTP-account met een eigen dagbudget en een afzenderadres "
            "op naam van je project; de naam die de ontvanger ziet, kies je zelf."
        ),
        help_template="send_email/help.md",
        icon="envelop",
        color="donkerblauw",
        secret_class="SendEmailSecret",  # noqa: S106  # the name of the key or the secret class, not a secret
        variables=[var.value for var in SendEmailVariables],
        cleanup_strategy=CleanupStrategy.IMMEDIATE,
        # The button on the deployment card to that deployment's sender and budget.
        actions_provider=send_email_actions,
    )
    config_model = SendEmailConfig
    config_schema_version = "1.0"
    cleanup_manager_key = ManagerKey.MAIL
    provision_order = 50
    manifest_secret_class = SendEmailSecret
    # Last of the contributors: appended after the existing envFrom services and the two
    # override services.
    manifest_order = 70

    config_section_id: ClassVar[str] = "send-email-config"
    modal_flow_id = "modal-edit-send-email-config"

    def config_model_for(self, layer: ConfigLayer) -> type[BaseModel] | None:
        # Two layers, two shapes: the project decides for all its deployments (and carries the
        # service's own approval), a deployment overrides three fields.
        if layer is ConfigLayer.PROJECT:
            return SendEmailConfig
        if layer is ConfigLayer.DEPLOYMENT:
            return SendEmailDeploymentConfig
        return None

    def config_api_fields(self, layer: ConfigLayer) -> list[str]:
        if layer in (ConfigLayer.PROJECT, ConfigLayer.DEPLOYMENT):
            return self.config_model_field_names(layer)
        return []

    def config_editables(self, layer: ConfigLayer) -> list[Editable]:
        from opi.services.catalog.send_email.editables import (
            SEND_EMAIL_DEPLOYMENT_EDITABLES,
            SEND_EMAIL_FROM_ADDRESS_EDITABLE,
            SEND_EMAIL_FROM_NAME_EDITABLE,
            SEND_EMAIL_MESSAGES_PER_DAY_EDITABLE,
        )

        if layer is ConfigLayer.PROJECT:
            return [
                SEND_EMAIL_FROM_NAME_EDITABLE,
                SEND_EMAIL_FROM_ADDRESS_EDITABLE,
                SEND_EMAIL_MESSAGES_PER_DAY_EDITABLE,
            ]
        if layer is ConfigLayer.DEPLOYMENT:
            return list(SEND_EMAIL_DEPLOYMENT_EDITABLES)
        return []

    def config_form_section(self, layer: ConfigLayer) -> FormSection | None:
        if layer is ConfigLayer.DEPLOYMENT:
            return self.deployment_form_section()
        if layer is not ConfigLayer.PROJECT:
            return super().config_form_section(layer)
        # Cached: consumers compare section identity (EDIT_SECTIONS[...] is X).
        cached = getattr(self, "_config_section_cache", None)
        if cached is None:
            from opi.forms.visualizers.sections import FormSection
            from opi.services.catalog.send_email.visualizers import (
                SEND_EMAIL_FROM_ADDRESS,
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
                editables=[SEND_EMAIL_FROM_NAME, SEND_EMAIL_FROM_ADDRESS, SEND_EMAIL_MESSAGES_PER_DAY],
                # Passing this step IS the request. Through the generic function and not
                # through the own hook, so no second road to a request appears; the API side
                # calls the same function (project_manager, config write).
                post_merge=_request_approval_on_save,
                layout=[
                    config_path(ConfigLayer.PROJECT, self.service_type, "config", "from-name"),
                    config_path(ConfigLayer.PROJECT, self.service_type, "config", FROM_ADDRESS_KEY),
                    config_path(ConfigLayer.PROJECT, self.service_type, "config", MESSAGES_PER_DAY_KEY),
                ],
            )
            self._config_section_cache = cached
        return cached

    def _config_selected(self, project_data: dict[str, Any]) -> bool:
        """Section visibility: the same question as everywhere else in this module."""
        return _selected(project_data)

    def config_approvals(self, layer: ConfigLayer) -> list[ApprovalSpec]:
        """Five approvals through the generic mechanism, on two layers.

        Project: may this project mail, under which address, and how much. They are
        separate because a project may mail without wanting an address or budget increase,
        and the last two hang on a VALUE: changing either after a verdict asks again.

        Deployment: an override of that address or that budget. Its own decision, so it
        survives the project lowering its own value, and a deployment that inherits asks
        nothing.
        """
        if layer is ConfigLayer.PROJECT:
            return [APPROVAL.spec, FROM_ADDRESS_APPROVAL.spec, MESSAGES_PER_DAY_APPROVAL.spec]
        if layer is ConfigLayer.DEPLOYMENT:
            return [DEPLOYMENT_FROM_ADDRESS_APPROVAL.spec, DEPLOYMENT_MESSAGES_PER_DAY_APPROVAL.spec]
        return []

    def ensure_approval_requests(self, project_data: dict[str, Any]) -> None:
        """Switching the service on IS the request; so is an address or a budget above the
        default, on either layer."""
        APPROVAL.ensure_requested(project_data)
        FROM_ADDRESS_APPROVAL.ensure_requested(project_data)
        MESSAGES_PER_DAY_APPROVAL.ensure_requested(project_data)
        DEPLOYMENT_FROM_ADDRESS_APPROVAL.ensure_requested(project_data)
        DEPLOYMENT_MESSAGES_PER_DAY_APPROVAL.ensure_requested(project_data)

    def deployment_form_section(
        self, deployment_index: int | None = None, inherited: dict[str, str] | None = None
    ) -> FormSection:
        """The sender and the daily budget of ONE deployment; without an index it describes the
        layer.

        ``inherited`` are the project's values, which an empty field here uses. They go into the
        field as a placeholder: without that, whoever opens this form sees an empty box instead
        of what holds now.
        """
        from opi.forms.editables.reindex import materialize_wildcard_visualizer
        from opi.forms.layout import Fieldset
        from opi.forms.visualizers.sections import FormSection
        from opi.services.catalog.send_email.visualizers import deployment_visualizers

        index = 0 if deployment_index is None else deployment_index
        fields = [materialize_wildcard_visualizer(field, index) for field in deployment_visualizers(inherited)]
        suffix = "" if deployment_index is None else f"-{deployment_index}"
        return FormSection(
            section_id=f"send-email-deployment-config{suffix}",
            title="E-mail per deployment",
            icon="envelop",
            description=(
                "Afzender en dagbudget van deze deployment. Laat een veld leeg om de waarde van "
                "het project te gebruiken. Een eigen adres, en een budget boven de standaard, "
                "moet een beheerder goedkeuren; tot die tijd blijft gelden wat er al gold."
            ),
            post_save_action="process_project",
            # Passing this step IS the request, through the same generic function as the
            # project step: no second road to a request.
            post_merge=_request_approval_on_save,
            editables=fields,
            layout=[Fieldset(legend="Afzender en budget", children=[field.editable.yaml_path for field in fields])],
        )

    async def provision(self, ctx: ProvisionContext) -> None:
        await ctx.mail_manager.create_resources_for_deployment(ctx.project_data, ctx.deployment)

    def contribute_manifest_context(self, ctx: ManifestContext) -> ManifestContribution:
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

        The policy comes from the service and not from a hand-applied YAML: the relay lives
        in its OWN namespace because the Calico egress annotation takes one value, and the
        tenant baseline only opens the operations namespace. Without a rule here a pod
        resolves the relay and then hangs on connect. The service is the only thing that
        knows it is switched on and for which component, so it is the only thing that can
        keep the rule in step -- and the prune removes the file again when the service is
        switched off, which a hand-applied policy does not do.
        """
        # Not approved means no rule, exactly as it means no account: a network policy
        # towards a relay the project has no account on is a half state. The prune keys on
        # the filename, so an approval that is withdrawn takes the file with it on the next
        # run.
        if not is_approved(ctx.project_data):
            return []

        deployment_name = ctx.deployment["name"]
        components = self.components_using_service(ctx)
        if not components:
            return []

        relay_namespace = get_mail_relay_namespace(ctx.cluster)
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
                            # The POD port, not the service port: see RELAY_POD_PORT.
                            "ports": [RELAY_POD_PORT],
                        }
                    ],
                },
            )
            for component in components
        ]
