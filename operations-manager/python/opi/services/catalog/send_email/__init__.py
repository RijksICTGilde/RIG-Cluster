"""send-email service: an SMTP account on the platform mail relay.

Named after what it does, like ``publish-on-web`` -- not ``smtp-mail`` (a protocol name in
a user-facing thing, which we do nowhere else) and emphatically not ``sendmail``, which is
an existing MTA and a well-known command; a service by that name would leave readers
guessing for years which of the three was meant. ``plans/mailrelay.md`` argues it.

What it hands a component is credentials, nothing more: host, port, username, password and
the sender address. Everything that decides whether the mail actually ARRIVES -- the
envelope sender, the whole ``From:`` header, the stripped ``Received`` chain -- is enforced
on the relay and is never the application's to choose. A project that could set its own
``From:`` DOMAIN would simply produce mail that fails DMARC, so the domain stays ours.

The project-level block is small on purpose: a display name, a sender address and a daily
budget. Two of those do not take effect by being saved: a platform administrator approves
THAT VALUE first. For the address (RC-399) that holds always; for the budget (RC-400) only
above the platform default, because lowering your own ceiling asks nobody. Until the address
is approved (and again after a withdrawal) the platform composes it from the account name
(``noreply-rijksapp+<project>-<deployment>-<hash>@rijksoverheid.nl``) and the relay derives
it itself. The envelope keeps that composed address either way, so a bounce stays traceable;
the relay adds an ``X-ZAD-Project`` header so a message stays traceable to a project even
when the ``From:`` no longer says so. The approval blocks are the platform's side and carry
``PLATFORM_MANAGED``.

Since RC-401 the same three fields exist on the DEPLOYMENT layer, because the account is per
deployment and so is its budget: a test deployment sends its hundred messages a day without
anyone approving anything, production its hundred thousand after approval. A deployment that
says nothing inherits the project's values, including an approved address or budget, without
a request of its own. A deployment that overrides asks for ITSELF: its approval is separate
from the project's, so it keeps standing when the project lowers its own value, and until it
is granted the inherited value keeps applying.

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
- component-level config -- the account belongs to the deployment; a component only
  decides whether it gets the credentials.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

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
from opi.services.services import ServiceDefinition, deployment_index, service_entry_config, service_entry_name
from opi.services.services_enums import CleanupStrategy, ManagerKey, ServiceType
from opi.utils.naming import (
    generate_deployment_mail_sender_address,
    generate_network_policy_name,
    generate_unique_name,
)
from opi.utils.secrets import SendEmailSecret

logger = logging.getLogger(__name__)

#: Pod label the relay's Deployment carries, so the NetworkPolicy peer is that one
#: workload and not the whole RON namespace (which also hosts the VLAM gateway).
RELAY_POD_LABELS = {"app": "rig-mail-relay"}

#: The relay's POD port (containerPort ``submission``), for the egress NetworkPolicy.
#: NOT ``get_mail_relay_port()``: that is the Service port (587), which is what an
#: application dials and what goes into SMTP_PORT. A NetworkPolicy is evaluated AFTER
#: the Service DNAT, so it sees the pod port; a rule on 587 matches nothing and the
#: connect times out (measured 20 August 2026 on ai1-uit). Same trap as the relay ->
#: sink hop (1025, not 25) and the ingress side of the relay's own policy (2525).
#: Change the containerPort in mail/controller/base/deployment.yaml, change this too.
RELAY_POD_PORT = 2525

#: De goedkeuring van deze dienst, in de gedeelde vorm: EEN besluit per project over de
#: vraag of het de dienst mag gebruiken. Alles wat die vorm nodig heeft -- de status
#: lezen, het item voor de beheerpagina, het oordeel vastleggen, de mededeling aan de
#: aanvrager en de aanvraag zelf -- komt uit ``service_use_approval``; hier staat alleen
#: nog WAT er goedgekeurd wordt en wat het betekent zolang dat niet gebeurd is.
APPROVAL = service_use_approval(
    ServiceType.SEND_EMAIL,
    label="E-mail versturen",
    activity="Het versturen van e-mail",
    consequence=(
        "Er is nog geen SMTP-account, geen netwerktoegang naar de relay en geen SMTP_-variabelen in deze deployment."
    ),
)

#: DE POORT. Alles wat de dienst aanzet hangt hieraan, zodat de vier dingen die tegelijk
#: aangaan (account, netwerkbeleid, envFrom-secret, secretbestand) het nooit oneens kunnen
#: zijn -- de halve toestand die aanvulling 6 uitsluit.
is_approved = APPROVAL.is_approved


#: Sleutel van de weergavenaam in het configblok van de dienst.
FROM_NAME_KEY = "from-name"

#: Sleutel van het aangevraagde afzenderadres in het configblok van de dienst.
FROM_ADDRESS_KEY = "from-address"

#: Opschrift van de adresaanvraag in de beheerpagina. De vorm komt uit
#: ``config_value_approval``: de goedkeuring hangt aan de WAARDE, dus een goedgekeurd adres
#: dat het project daarna wijzigt is een nieuwe aanvraag (RC-399).
FROM_ADDRESS_APPROVAL_LABEL = "Afzenderadres"


def _selected(project_data: dict[str, Any]) -> bool:
    """Whether the project has the service in its services list at all."""
    return ServiceType.SEND_EMAIL.value in [
        service_entry_name(entry) for entry in project_data.get("services", []) or []
    ]


def _request_approval_on_save(project_data: dict[str, Any], wizard_data: dict[str, Any]) -> None:
    """Hook op de configuratiestap: leg de aanvraag vast zodra de dienst is aangezet."""
    from opi.services.approvals import ensure_approval_requests

    ensure_approval_requests(project_data)


def _service_entry(project_data: dict[str, Any]) -> dict[str, Any] | None:
    """De service-entry van deze dienst, als hij als dict in het project staat."""
    for entry in project_data.get("services", []) or []:
        if service_entry_name(entry) == ServiceType.SEND_EMAIL.value and isinstance(entry, dict):
            return entry
    return None


def _config(project_data: dict[str, Any]) -> dict[str, Any]:
    """Het configblok van de dienst, leeg als het er niet is."""
    entry = _service_entry(project_data)
    config = entry.get("config") if entry else None
    return config if isinstance(config, dict) else {}


def _requested_from_address(project_data: dict[str, Any]) -> str:
    """Het adres dat het project vraagt, of leeg voor het afgeleide adres."""
    return str(_config(project_data).get(FROM_ADDRESS_KEY) or "").strip()


def _from_address_consequence(project_data: dict[str, Any], deployment: dict[str, Any]) -> str:
    """Onder welk adres de post ondertussen vertrekt.

    Zonder dit weet de aanvrager wel dat hij wacht, maar niet onder welk adres zijn post
    de deur uitgaat.
    """
    # Een clusternaam die het platform niet kent is schemageldig (``project_validation``
    # slaat zo'n deployment over), en deze mededeling is informatief: zonder deze controle
    # haalde een onbekende naam met een ValueError de hele projectpagina omver. Zelfde
    # vorm als ``display_blocks._get_default_domain``.
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
    activity=lambda adres: f"Het afzenderadres {adres}",
    consequence=_from_address_consequence,
    subject=lambda _project_data, adres: adres,
)

#: DE POORT op de schrijfweg: het goedgekeurde afzenderadres, of leeg.
#:
#: Leeg betekent dat de relay het adres afleidt uit de accountnaam, en het wist meteen een
#: adres dat er eerder stond. Zo heeft intrekken geen eigen trigger nodig: elke verwerking
#: schrijft opnieuw wat er nu geldt.
approved_from_address = FROM_ADDRESS_APPROVAL.approved_value


#: Sleutel van het gevraagde dagbudget in het configblok van de dienst.
MESSAGES_PER_DAY_KEY = "messages-per-day"

#: Opschrift van de budgetaanvraag in de beheerpagina.
MESSAGES_PER_DAY_APPROVAL_LABEL = "Dagbudget e-mail"


def _chosen_messages_per_day(project_data: dict[str, Any]) -> int | None:
    """Het getal dat het project zelf heeft ingevuld, of None voor de standaard."""
    waarde = _config(project_data).get(MESSAGES_PER_DAY_KEY)
    if isinstance(waarde, bool) or not isinstance(waarde, int) or waarde < 1:
        return None
    return waarde


def _last_approved_number(project_data: dict[str, Any]) -> str:
    """Het laatst goedgekeurde getal als tekst, leeg als er geen bruikbaar getal staat.

    ``isdigit`` is ook waar voor cijfertekens buiten ASCII, zoals een superscript twee,
    waar ``int()`` omvalt; ``isascii`` erbij houdt alleen een gewoon decimaal getal over.
    """
    laatst = MESSAGES_PER_DAY_APPROVAL.last_approved_value(project_data)
    return laatst if laatst.isdigit() and laatst.isascii() else ""


def _granted_messages_per_day(project_data: dict[str, Any]) -> int:
    """Het hoogste getal dat het project zonder nieuwe goedkeuring mag gebruiken.

    De standaard, of hoger zodra een beheerder zo'n getal goedkeurde: binnen wat het al
    mag kiest het project zelf.
    """
    standaard = settings.MAIL_PROJECT_DEFAULT_MESSAGES_PER_DAY
    laatst = _last_approved_number(project_data)
    return max(standaard, int(laatst)) if laatst else standaard


def _requested_messages_per_day(project_data: dict[str, Any]) -> str:
    """Het getal waarover een beheerder moet oordelen. Leeg is geen aanvraag.

    Verlagen vraagt niets, ook niet tot boven de standaard: blijft het project binnen een
    eerder goedgekeurd getal, dan geeft dit dat getal terug, zodat de bestaande goedkeuring
    blijft staan. Zou hier leeg staan, dan verdwijnt het goedkeuringsblok en daarmee het
    laatst goedgekeurde getal, en valt het project terug op de standaard.
    """
    chosen = _chosen_messages_per_day(project_data)
    if chosen is None:
        return ""
    if chosen > _granted_messages_per_day(project_data):
        return str(chosen)
    return _last_approved_number(project_data)


def effective_messages_per_day(project_data: dict[str, Any]) -> int:
    """DE POORT op de schrijfweg: het dagbudget dat de relay echt afdwingt.

    Een verhoging geldt alleen als een beheerder PRECIES DAT getal heeft goedgekeurd.
    Zolang dat niet zo is, geldt wat het project al mocht: een openstaande aanvraag
    verlaagt nooit wat er gold, en een verlaging binnen dat getal geldt meteen.
    """
    chosen = _chosen_messages_per_day(project_data)
    if chosen is None:
        return settings.MAIL_PROJECT_DEFAULT_MESSAGES_PER_DAY
    if MESSAGES_PER_DAY_APPROVAL.spec.is_approved(project_data, str(chosen)):
        return chosen
    return min(chosen, _granted_messages_per_day(project_data))


def _messages_per_day_consequence(project_data: dict[str, Any], deployment: dict[str, Any]) -> str:
    """Hoeveel het project ondertussen mag versturen."""
    return f"Je verstuurt voorlopig hoogstens {effective_messages_per_day(project_data)} berichten per dag."


def _messages_per_day_subject(project_data: dict[str, Any], value: str) -> str:
    """Wat de beheerpagina toont: het gevraagde getal en wat er nu geldt."""
    return f"{value} berichten per dag (nu {effective_messages_per_day(project_data)})"


MESSAGES_PER_DAY_APPROVAL = config_value_approval(
    ServiceType.SEND_EMAIL,
    field=MESSAGES_PER_DAY_KEY,
    label=MESSAGES_PER_DAY_APPROVAL_LABEL,
    requested_value=_requested_messages_per_day,
    activity=lambda aantal: f"Een dagbudget van {aantal} berichten per dag",
    consequence=_messages_per_day_consequence,
    subject=_messages_per_day_subject,
)


# --- de deploymentlaag (RC-401) -------------------------------------------------
#
# Dezelfde drie velden, maar van EEN deployment, want het account is van de deployment.
# Wat een deployment niet invult, erft het; wat het wel invult, is zijn eigen aanvraag.


def _deployment(project_data: dict[str, Any], deployment_name: str) -> dict[str, Any]:
    """De deployment met deze naam, of een leeg blok als het project hem niet kent."""
    index = deployment_index(project_data, deployment_name)
    if index is None:
        return {}
    deployment = project_data["deployments"][index]
    return deployment if isinstance(deployment, dict) else {}


def _deployment_config(project_data: dict[str, Any], deployment_name: str) -> dict[str, Any]:
    """Het configblok van de dienst BIJ DEZE DEPLOYMENT, leeg als het er niet is."""
    for entry in _deployment(project_data, deployment_name).get("services", []) or []:
        if service_entry_name(entry) == ServiceType.SEND_EMAIL.value:
            config = service_entry_config(entry)
            return config if isinstance(config, dict) else {}
    return {}


def _deployment_requested_from_address(project_data: dict[str, Any], deployment_name: str) -> str:
    """Het adres dat deze deployment vraagt. Elke override is een aanvraag, net als op
    projectniveau (RC-399): een zelfgekozen afzender is een spoofingknop."""
    return str(_deployment_config(project_data, deployment_name).get(FROM_ADDRESS_KEY) or "").strip()


def _deployment_from_address_consequence(project_data: dict[str, Any], deployment: dict[str, Any]) -> str:
    """Onder welk adres de post van deze deployment ondertussen vertrekt."""
    cluster = deployment.get("cluster") or settings.CLUSTER_MANAGER
    if cluster not in CLUSTER_CONFIG:
        return ""
    ondertussen = deployment_from_address(
        project_data, deployment.get("name", "")
    ) or generate_deployment_mail_sender_address(
        get_mail_from_address(cluster), project_data.get("name", ""), deployment.get("name", "")
    )
    return f"Deze deployment verstuurt voorlopig onder {ondertussen}." if ondertussen else ""


DEPLOYMENT_FROM_ADDRESS_APPROVAL = deployment_value_approval(
    ServiceType.SEND_EMAIL,
    field=FROM_ADDRESS_KEY,
    label=f"{FROM_ADDRESS_APPROVAL_LABEL} per deployment",
    requested_value=_deployment_requested_from_address,
    activity=lambda adres: f"Het afzenderadres {adres}",
    consequence=_deployment_from_address_consequence,
    subject=lambda _project_data, deployment_name, adres: f"{adres} ({deployment_name})",
)


def deployment_from_address(project_data: dict[str, Any], deployment_name: str) -> str:
    """DE POORT op de schrijfweg: het afzenderadres dat de relay voor deze deployment pint.

    De laatst voor DEZE deployment goedgekeurde override, anders het goedgekeurde adres van
    het project, anders leeg -- en leeg betekent dat de relay het adres uit de accountnaam
    afleidt. Een openstaande aanvraag verandert dus niets aan wat er al gold.
    """
    return (
        DEPLOYMENT_FROM_ADDRESS_APPROVAL.approved_value(project_data, deployment_name)
        or DEPLOYMENT_FROM_ADDRESS_APPROVAL.last_approved_value(project_data, deployment_name)
        or approved_from_address(project_data)
    )


def _deployment_chosen_messages_per_day(project_data: dict[str, Any], deployment_name: str) -> int | None:
    """Het getal dat deze deployment zelf heeft ingevuld, of None als zij erft."""
    waarde = _deployment_config(project_data, deployment_name).get(MESSAGES_PER_DAY_KEY)
    if isinstance(waarde, bool) or not isinstance(waarde, int) or waarde < 1:
        return None
    return waarde


def _deployment_last_approved_messages_per_day(project_data: dict[str, Any], deployment_name: str) -> int | None:
    """Het laatst voor deze deployment goedgekeurde getal, of None.

    ``isdigit`` is ook waar voor cijfertekens buiten ASCII, waar ``int()`` omvalt; met
    ``isascii`` erbij blijft alleen een gewoon decimaal getal over.
    """
    laatst = DEPLOYMENT_MESSAGES_PER_DAY_APPROVAL.last_approved_value(project_data, deployment_name)
    return int(laatst) if laatst.isdigit() and laatst.isascii() else None


def _deployment_requested_messages_per_day(project_data: dict[str, Any], deployment_name: str) -> str:
    """Het getal waarover een beheerder voor deze deployment moet oordelen. Leeg is geen aanvraag.

    De grens is de PLATFORMSTANDAARD en niet de projectwaarde: de goedkeuring van een
    override staat los van die van het project, zodat ze blijft staan als het project zijn
    eigen getal later verlaagt. Tot en met de standaard kiest de deployment zelf.

    Blijft de deployment binnen de standaard terwijl er eerder iets is goedgekeurd, dan
    geeft dit dat goedgekeurde getal terug: zou hier leeg staan, dan verdwijnt het
    goedkeuringsblok en daarmee het geheugen van wat zij al mocht.
    """
    chosen = _deployment_chosen_messages_per_day(project_data, deployment_name)
    if chosen is None:
        return ""
    if chosen > settings.MAIL_PROJECT_DEFAULT_MESSAGES_PER_DAY:
        return str(chosen)
    laatst = _deployment_last_approved_messages_per_day(project_data, deployment_name)
    return "" if laatst is None else str(laatst)


def _deployment_messages_per_day_consequence(project_data: dict[str, Any], deployment: dict[str, Any]) -> str:
    """Hoeveel deze deployment ondertussen mag versturen."""
    aantal = deployment_messages_per_day(project_data, deployment.get("name", ""))
    return f"Deze deployment verstuurt voorlopig hoogstens {aantal} berichten per dag."


def _deployment_messages_per_day_subject(project_data: dict[str, Any], deployment_name: str, value: str) -> str:
    """Wat de beheerpagina toont: het gevraagde getal, de deployment en wat er nu geldt."""
    nu = deployment_messages_per_day(project_data, deployment_name)
    return f"{value} berichten per dag voor {deployment_name} (nu {nu})"


DEPLOYMENT_MESSAGES_PER_DAY_APPROVAL = deployment_value_approval(
    ServiceType.SEND_EMAIL,
    field=MESSAGES_PER_DAY_KEY,
    label=f"{MESSAGES_PER_DAY_APPROVAL_LABEL} per deployment",
    requested_value=_deployment_requested_messages_per_day,
    activity=lambda aantal: f"Een dagbudget van {aantal} berichten per dag",
    consequence=_deployment_messages_per_day_consequence,
    subject=_deployment_messages_per_day_subject,
)


def deployment_messages_per_day(project_data: dict[str, Any], deployment_name: str) -> int:
    """DE POORT op de schrijfweg: het dagbudget dat de relay voor deze deployment afdwingt.

    Vier gevallen, in deze volgorde:

    * de deployment vult niets in: het getal van het project (dat zijn eigen poort heeft);
    * zij vult de platformstandaard of minder in: dat getal, want dat vraagt niemand iets;
    * haar getal is voor PRECIES die waarde goedgekeurd: dat getal;
    * anders: haar laatst goedgekeurde override, en zonder die het geerfde getal. Een
      openstaande aanvraag verlaagt nooit wat er al gold.
    """
    inherited = effective_messages_per_day(project_data)
    chosen = _deployment_chosen_messages_per_day(project_data, deployment_name)
    if chosen is None:
        return inherited
    if chosen <= settings.MAIL_PROJECT_DEFAULT_MESSAGES_PER_DAY:
        return chosen
    if DEPLOYMENT_MESSAGES_PER_DAY_APPROVAL.is_approved(project_data, deployment_name):
        return chosen
    laatst = _deployment_last_approved_messages_per_day(project_data, deployment_name)
    return inherited if laatst is None else laatst


def project_from_name(project_data: dict[str, Any]) -> str:
    """De weergavenaam van het project, die elke deployment erft die er geen eigen heeft."""
    return str(_config(project_data).get(FROM_NAME_KEY) or "").strip()


def deployment_from_name(project_data: dict[str, Any], deployment_name: str) -> str:
    """De weergavenaam van deze deployment: de hare, anders die van het project.

    Vrij, net als op projectniveau: een naam vraagt niemand iets. Leeg is een geldige
    uitkomst en betekent dat de post zonder naam vertrekt.
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
        secret_class="SendEmailSecret",  # noqa: S106  # naam van de sleutel of de secretklasse, geen secret
        variables=[var.value for var in SendEmailVariables],
        cleanup_strategy=CleanupStrategy.IMMEDIATE,
        # De knop op de deploymentkaart naar de afzender en het budget van die deployment.
        actions_provider=send_email_actions,
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

    def config_model_for(self, layer: ConfigLayer):
        # Twee lagen, twee vormen: het project beslist voor al zijn deployments (en draagt
        # de goedkeuring van de dienst zelf), een deployment overschrijft drie velden.
        if layer is ConfigLayer.PROJECT:
            return SendEmailConfig
        if layer is ConfigLayer.DEPLOYMENT:
            return SendEmailDeploymentConfig
        return None

    def config_api_fields(self, layer: ConfigLayer) -> list[str]:
        if layer in (ConfigLayer.PROJECT, ConfigLayer.DEPLOYMENT):
            return self.config_model_field_names(layer)
        return []

    def config_editables(self, layer: ConfigLayer):
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

    def config_form_section(self, layer: ConfigLayer):
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
                # Doorlopen van deze stap IS de aanvraag. Via de generieke functie en niet
                # via de eigen hook, zodat er geen tweede weg naar een aanvraag ontstaat;
                # de API-kant roept dezelfde functie aan (project_manager, config-schrijf).
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

    def config_approvals(self, layer: ConfigLayer):
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

    def deployment_form_section(self, deployment_index: int | None = None, inherited: dict[str, str] | None = None):
        """De afzender en het dagbudget van EEN deployment; zonder index beschrijft het de laag.

        ``inherited`` zijn de waarden van het project, die een leeg veld hier gebruikt. Ze
        gaan als placeholder het veld in: zonder dat ziet wie dit formulier opent een leeg
        vak en niet wat er nu geldt.
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
            # Doorlopen van deze stap IS de aanvraag, via dezelfde generieke functie als de
            # projectstap: geen tweede weg naar een aanvraag.
            post_merge=_request_approval_on_save,
            editables=fields,
            layout=[Fieldset(legend="Afzender en budget", children=[field.editable.yaml_path for field in fields])],
        )

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
                            # De PODpoort, niet de servicepoort: zie RELAY_POD_PORT.
                            "ports": [RELAY_POD_PORT],
                        }
                    ],
                },
            )
            for component in components
        ]
