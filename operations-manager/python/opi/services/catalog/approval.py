"""Generic approval capability for the service catalog (RC-5).

A service declares -- as data -- that some value it manages needs approval before it
takes effect ("dit heeft approval nodig"), and supplies the rule that reads the stored
approval state back ("is dit veld approved?"). Generic code (a future catalog-driven
approval interface, and the enforcers today) consumes these ``ApprovalSpec``s uniformly
instead of hard-coding one subsystem per approvable thing.

Grounded in the single real case today: publish-on-web's domain / subdomain approval,
whose state lives in the project's ``domains:`` block and whose rules are the pure
predicates in ``connectors/subdomain.py``. This module does NOT move that state or
rewrite those rules -- it wraps them so the shape (declare + check) is visible and
reusable. Moving the state under the service is a separate schema+data migration
(see features/futures/service-vertical-slice.md).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from opi.services.services import deployment_index, service_entry_config, service_entry_name

if TYPE_CHECKING:
    from collections.abc import Callable

    from opi.services.services_enums import ServiceType


class ApprovalStatus(StrEnum):
    """Where a requested value sits in the approval lifecycle.

    The three non-NONE values match the strings persisted in the project file
    (``requested`` / ``approved`` / ``denied``), so a stored status maps straight onto
    the enum. NONE means "no request on record" -- nothing to approve yet.
    """

    NONE = "none"
    REQUESTED = "requested"
    APPROVED = "approved"
    DENIED = "denied"


class ApproverScope(StrEnum):
    """Who is entitled to approve a value -- what makes the capability reusable.

    Domains are cross-project, so a PLATFORM_ADMIN approves them. A future service
    granting e.g. cross-namespace access would be approved within the project
    (PROJECT_ADMIN / PROJECT_MEMBER). The scope selects which approver a generic
    approval interface routes a pending item to.
    """

    PLATFORM_ADMIN = "platform-admin"
    PROJECT_ADMIN = "project-admin"
    PROJECT_MEMBER = "project-member"


#: Wire shape of an approval **item** -- one approvable thing found in a project, as
#: the approver interface (listing + modal flow) consumes it. Transient: it is seeded
#: into the wizard, never persisted (stripped before the project is saved). A plain
#: dict, not a dataclass, because it is the established contract of the approval
#: editables + ``approval_items.html.j2`` template. Keys:
#:   ``service``        -- the ServiceType value of the owning service (routing)
#:   ``type``           -- the owning ``ApprovalSpec.key`` (routing + display)
#:   ``domain``/``name``-- display identity of the item
#:   ``subject``        -- WHAT is being asked for, written by the service that knows
#:                         (``example.nl``, ``foo.example.nl``, "Gebruik van de dienst").
#:                         Display only: it does not travel back through the modal form.
#:   ``current_status`` -- the stored status string
#:   ``status``         -- the approver's verdict ("skip" until decided)
#:   ``history``        -- prior verdict entries
#:   ``message``        -- optional note (added by the approver)
ApprovalItem = dict[str, Any]


#: Wire shape of an approval **notice** -- what the OWNER of a deployment must be told
#: about an approval that is not granted. The counterpart of ApprovalItem: that one
#: faces the approver ("decide this"), this one faces the applicant ("this is where
#: your request stands, and this is what it means for your deployment"). The consequence
#: is service knowledge (publish-on-web falls back to the cluster address), so the spec
#: writes the sentence and generic code only renders it. Keys:
#:   ``service``  -- the ServiceType value of the owning service
#:   ``type``     -- the owning ``ApprovalSpec.key``
#:   ``label``    -- the spec's human label ("Domein", "Subdomein")
#:   ``subject``  -- what was requested, for display (e.g. "test2.example.nl")
#:   ``status``   -- the stored status ("requested" / "denied" / "none")
#:   ``text``     -- what this means for the deployment, in the owner's words
#:   ``by`` / ``date`` / ``message`` -- the last verdict's approver, date and note
ApprovalNotice = dict[str, Any]


@dataclass(frozen=True)
class ApprovalSpec:
    """A service's declaration that a value it manages requires approval.

    This is the DEFINITION ("dit heeft approval nodig"): a service returns one or more
    of these from ``config_approvals(layer)``. It is pure data plus rule callbacks --
    it carries no forms / manager / DB imports, so the catalog stays load-light.

    Attributes:
        key: Stable identifier for this approvable within the service (e.g. ``"domain"``,
            ``"subdomain"``). Used to look the spec up and to key any UI / state.
        label: Human label for the approver-facing interface.
        approver: Who may approve (see :class:`ApproverScope`).
        status_of: The CHECK ("is dit veld approved?"). Given the project data and the
            value being requested, returns its :class:`ApprovalStatus` by reading the
            stored approval state. The service owns this rule; generic code just calls
            it. The ``value`` is opaque to the generic layer -- its shape is the
            service's business (a domain string, a ``(domain, subdomain)`` pair, ...).
        list_items: The LIST ("wat staat er open voor deze aanvraag?"). Enumerates the
            approvable items this spec currently has in a project, as
            :data:`ApprovalItem` dicts. The generic approver interface concatenates
            these across the catalog instead of hard-coding one subsystem's shape.
            ``None`` for a check-only spec that does not surface in the approver UI.
        record: The RECORD ("leg het oordeel vast"). Applies one approver verdict --
            writes the new status + appends ``history_entry`` to the stored state for
            the given item. ``None`` if the spec is not approver-writable.
        notices_for: The NOTICE ("wat merkt de aanvrager hiervan?"). Given a deployment,
            returns what its owner must be told about this approval when it is not
            granted -- including the consequence, which only the service knows. Returns
            an empty list when there is nothing to report. ``None`` for a spec whose
            state has no visible effect on a deployment.
    """

    key: str
    label: str
    approver: ApproverScope
    status_of: Callable[[dict[str, Any], Any], ApprovalStatus]
    list_items: Callable[[dict[str, Any]], list[ApprovalItem]] | None = None
    record: Callable[[dict[str, Any], ApprovalItem, dict[str, Any]], None] | None = None
    notices_for: Callable[[dict[str, Any], dict[str, Any]], list[ApprovalNotice]] | None = None
    requires_approval_for: Callable[[Any], bool] | None = None

    def status(self, project_data: dict[str, Any], value: Any) -> ApprovalStatus:
        """The approval status of ``value`` given the current project state."""
        return self.status_of(project_data, value)

    def is_approved(self, project_data: dict[str, Any], value: Any) -> bool:
        """Whether ``value`` is approved -- the common yes/no check for gating."""
        return self.status_of(project_data, value) is ApprovalStatus.APPROVED

    def requires_approval(self, config: Any) -> bool:
        """Of een dienst die zich met ``config`` aanmeldt hiervoor een aanvraag nodig heeft.

        Bepaalt of de dienst zich impliciet mag aanmelden (``implicit_project_entry``). De
        standaard is ja: een goedkeuring over de dienst zelf geldt ongeacht de config. Een
        goedkeuring over een WAARDE vult dit met zijn eigen predicaat, zodat alleen de
        toestand die om een besluit vraagt de impliciete aanmelding tegenhoudt.
        """
        return True if self.requires_approval_for is None else self.requires_approval_for(config)


class _ApprovalState:
    """Het goedkeuringsblok van een dienst: waar het staat, en hoe je het leest en schrijft.

    Het blok hangt onder de config van de dienst zelf, op een vaste sleutel, en draagt
    ``status`` en ``history``. De vormen hieronder delen deze toegang zodat lezen,
    aanmaken en aanvullen maar op een plek staan.
    """

    def __init__(
        self,
        service: str,
        config_key: str,
        holder: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.service = service
        self.config_key = config_key
        #: Waar de dienstenlijst staat die het BLOK draagt. Standaard het project zelf; een
        #: goedkeuring op de deploymentlaag geeft hier de deployment, zodat lezen en
        #: schrijven in diens eigen ``services`` landt. De SELECTIE van de dienst blijft een
        #: vraag aan het project: een deployment kiest de dienst niet.
        self._holder = holder or (lambda project_data: project_data)

    def selected(self, project_data: dict[str, Any]) -> bool:
        """Of het project de dienst uberhaupt in zijn dienstenlijst heeft staan."""
        return self.service in [service_entry_name(entry) for entry in project_data.get("services", []) or []]

    def entry(self, project_data: dict[str, Any]) -> dict[str, Any] | None:
        """De service-entry van deze dienst, als hij als dict bij de houder staat."""
        for entry in self._holder(project_data).get("services", []) or []:
            if service_entry_name(entry) == self.service and isinstance(entry, dict):
                return entry
        return None

    def service_config(self, project_data: dict[str, Any]) -> dict[str, Any]:
        """De projectconfig van de dienst, leeg als hij er niet is."""
        config = service_entry_config(self.entry(project_data))
        return config if isinstance(config, dict) else {}

    def read(self, project_data: dict[str, Any]) -> dict[str, Any] | None:
        """Het opgeslagen blok, of None als er nooit iets is aangevraagd."""
        block = self.service_config(project_data).get(self.config_key)
        return block if isinstance(block, dict) else None

    def status(self, project_data: dict[str, Any]) -> ApprovalStatus:
        """De opgeslagen status, NONE zolang er geen blok of geen geldige status is."""
        block = self.read(project_data)
        if block is None:
            return ApprovalStatus.NONE
        try:
            return ApprovalStatus(block.get("status", ""))
        except ValueError:
            return ApprovalStatus.NONE

    def last_verdict(self, project_data: dict[str, Any]) -> dict[str, Any]:
        """Het laatste oordeel uit de history, leeg als er nog geen is."""
        history = (self.read(project_data) or {}).get("history") or []
        last = history[-1] if isinstance(history, list) and history else None
        return last if isinstance(last, dict) else {}

    def _promote_bare_entry(self, project_data: dict[str, Any]) -> dict[str, Any] | None:
        """Maak van een kale-string-entry de recordvorm, zodat er een config is om naar te schrijven.

        Een dienst met alleen optionele config blijft als kale string in de lijst staan en
        heeft dan geen plek voor het blok; de schrijver returnde dan stil en de aanvraag
        verdween. Promoveren naar de recordvorm is wat ``set_service_config`` ook doet: de
        selectie is een set op naam, de vorm van de entry is bijzaak.
        """
        services = self._holder(project_data).get("services")
        if not isinstance(services, list):
            return None
        for index, entry in enumerate(services):
            if entry == self.service:
                record: dict[str, Any] = {"name": self.service}
                services[index] = record
                return record
        return None

    @staticmethod
    def _config_holder(entry: dict[str, Any], service: str) -> dict[str, Any]:
        """Het dict waar de CONFIG van deze dienst in hangt.

        Voor de recordvorm (``{"name": "send-email", "config": ...}``) is dat de entry
        zelf; voor de legacy enkelsleutelvorm (``{"send-email": {"config": ...}}``) is dat
        de binnenste dict. Zonder dat onderscheid zet de schrijver het goedkeuringsblok
        NAAST de dienst in plaats van erin: de lezer vindt het dan niet, en de entry wordt
        een tweesleuteldict waar ``service_entry_name`` juist voor waarschuwt. Gemeten op de
        deploymentlaag, waar het formulier een entry in de legacy vorm aanmaakt.
        """
        inner = entry.get(service)
        if service in entry and isinstance(inner, dict) and "name" not in entry and "reference" not in entry:
            return inner
        return entry

    def write(
        self,
        project_data: dict[str, Any],
        fields: dict[str, Any],
        history_entry: dict[str, Any] | None = None,
    ) -> None:
        """Vul het blok aan, en maak het aan als het er nog niet is."""
        entry = self.entry(project_data)
        if entry is None:
            entry = self._promote_bare_entry(project_data)
        if entry is None:
            return
        config = self._config_holder(entry, self.service).setdefault("config", {})
        if not isinstance(config, dict):
            return
        block = config.setdefault(self.config_key, {})
        if not isinstance(block, dict):
            return
        block.update(fields)
        history = block.setdefault("history", [])
        if history_entry is not None and isinstance(history, list):
            history.append(history_entry)

    def clear(self, project_data: dict[str, Any]) -> None:
        """Haal het blok weg, zodat een volgende aanvraag weer bij nul begint."""
        self.service_config(project_data).pop(self.config_key, None)


def _notice_text(activity: str, consequence: str, status: ApprovalStatus) -> str:
    """De drie zinnen voor de aanvrager, op een plek zodat ze niet uit elkaar lopen."""
    if status is ApprovalStatus.DENIED:
        return f"{activity} is afgewezen. {consequence}".rstrip()
    if status is ApprovalStatus.REQUESTED:
        return f"{activity} is aangevraagd en wacht op goedkeuring. {consequence}".rstrip()
    return f"{activity} is nog niet aangevraagd. {consequence}".rstrip()


def _notice(
    state: _ApprovalState,
    project_data: dict[str, Any],
    *,
    subject: str,
    status: ApprovalStatus,
    text: str,
) -> ApprovalNotice:
    """De mededeling aan de aanvrager, aangevuld met het laatste oordeel."""
    verdict = state.last_verdict(project_data)
    return {
        "subject": subject,
        "status": status.value,
        "text": text,
        "by": verdict.get("by"),
        "date": verdict.get("date"),
        "message": verdict.get("message"),
    }


#: Wat er in de lijst staat als ONDERWERP van een goedkeuring die over de dienst zelf gaat.
#: Er is er per project maar een, en waar het over gaat is niet een waarde maar de dienst:
#: "mag dit project deze dienst gebruiken".
SERVICE_USE_SUBJECT = "Gebruik van de dienst"


@dataclass(frozen=True)
class ServiceUseApproval:
    """De complete vorm van "ja, dit project mag deze dienst gebruiken".

    Wat :func:`service_use_approval` teruggeeft. Drie dingen, en niet meer, want dat is
    precies wat een dienst met deze vorm nodig heeft:

    Attributes:
        spec: de :class:`ApprovalSpec` voor ``config_approvals()`` -- lezen, lijsten,
            vastleggen en de mededeling aan de aanvrager.
        is_approved: DE POORT. Alles wat de dienst aanzet hangt hieraan, zodat de
            onderdelen het nooit oneens kunnen zijn.
        ensure_requested: de dienst aanzetten IS de aanvraag; dit legt hem vast.
            Toestandsvormig en dus idempotent.
    """

    spec: ApprovalSpec
    is_approved: Callable[[dict[str, Any]], bool]
    ensure_requested: Callable[[dict[str, Any]], None]


def service_use_approval(
    service_type: ServiceType,
    *,
    label: str,
    activity: str,
    consequence: str,
    approver: ApproverScope = ApproverScope.PLATFORM_ADMIN,
) -> ServiceUseApproval:
    """De booleaanse goedkeuring: mag dit project deze dienst gebruiken, ja of nee.

    Deze vorm komt vaker terug dan bij een dienst alleen, en hij is met de hand ongeveer
    negentig regels: de status lezen, het item voor de beheerpagina, het oordeel
    vastleggen, de mededeling aan de aanvrager en de aanvraag zelf. De tweede dienst die
    hem nodig heeft kopieert dat, en vanaf dat moment lopen de twee uit elkaar. Vandaar
    een declaratie.

    De toestand staat onder de dienst zelf, op ``services/[<dienst>]/config/approval``,
    met ``status`` en ``history``. Er is er EEN per project: wat wordt goedgekeurd is
    "dit project mag deze dienst gebruiken", niet een afzonderlijke waarde.

    Args:
        service_type: de dienst waar de goedkeuring bij hoort.
        label: het opschrift van de aanvraag in de beheerpagina.
        activity: waar het besluit over gaat, als onderwerp van een zin -- bijvoorbeeld
            "Het versturen van e-mail". De drie zinnen (afgewezen, aangevraagd, nog niet
            aangevraagd) worden ermee gebouwd, zodat ze niet uit elkaar kunnen lopen.
        consequence: wat het voor de deployment BETEKENT zolang er geen goedkeuring is.
            Dit is de helft die je makkelijk overslaat en duur overslaat: een dienst die
            aanstaat en stil niets doet is precies de storing die hier is uitgeroeid.
        approver: wie mag beslissen.
    """
    naam = service_type.value
    state = _ApprovalState(naam, "approval")

    def _status_of(project_data: dict[str, Any], value: Any) -> ApprovalStatus:
        """De CHECK. ``value`` wordt niet gelezen: er is EEN goedkeuring per project."""
        return state.status(project_data)

    def _is_approved(project_data: dict[str, Any]) -> bool:
        return state.status(project_data) is ApprovalStatus.APPROVED

    def _items(project_data: dict[str, Any]) -> list[ApprovalItem]:
        """De LIJST. Een item per project dat het gevraagd heeft."""
        approval = state.read(project_data)
        if approval is None:
            return []
        return [
            {
                "type": naam,
                "subject": SERVICE_USE_SUBJECT,
                "domain": "",
                "name": project_data.get("name", ""),
                "current_status": approval.get("status", ""),
                "status": "skip",
                "history": approval.get("history", []),
            }
        ]

    def _record(project_data: dict[str, Any], item: ApprovalItem, history_entry: dict[str, Any]) -> None:
        """De RECORD. Schrijft het oordeel in het configblok van de dienst zelf."""
        state.write(project_data, {"status": item.get("status", "skip")}, history_entry)

    def _notices(project_data: dict[str, Any], deployment: dict[str, Any]) -> list[ApprovalNotice]:
        """De NOTICE. Wat de eigenaar te horen krijgt zolang er geen goedkeuring is."""
        if not state.selected(project_data):
            return []
        status = state.status(project_data)
        if status is ApprovalStatus.APPROVED:
            return []
        return [
            _notice(
                state,
                project_data,
                subject=project_data.get("name", ""),
                status=status,
                text=_notice_text(activity, consequence, status),
            )
        ]

    def _ensure_requested(project_data: dict[str, Any]) -> None:
        """De dienst aanzetten IS de aanvraag.

        Toestandsvormig en niet gebeurtenisvormig: lees het project zoals het staat en
        vul aan wat ontbreekt. Zo landt een aanvraag via de API op dezelfde plek als een
        vinkje in de wizard, en verandert een tweede keer draaien niets.
        """
        if not state.selected(project_data) or state.read(project_data) is not None:
            return
        state.write(project_data, {"status": ApprovalStatus.REQUESTED.value})

    return ServiceUseApproval(
        spec=ApprovalSpec(
            key=naam,
            label=label,
            approver=approver,
            status_of=_status_of,
            list_items=_items,
            record=_record,
            notices_for=_notices,
        ),
        is_approved=_is_approved,
        ensure_requested=_ensure_requested,
    )


#: Achtervoegsel van het goedkeuringsblok bij een waarde: ``from-address`` ->
#: ``from-address-approval``. Een vaste vorm, zodat een lezer van een projectbestand bij
#: elk aanvraagbaar veld weet waar het oordeel erover staat.
VALUE_APPROVAL_SUFFIX = "-approval"

#: Sleutel waaronder het blok de LAATST goedgekeurde waarde onthoudt. Het oordeel hangt aan
#: de waarde, dus een gewijzigde waarde is een nieuwe aanvraag; zonder dit geheugen zou een
#: openstaande aanvraag ook weghalen wat er al gold.
LAST_APPROVED_KEY = "approved-value"


def _status_of_value(state: _ApprovalState, data: dict[str, Any], value: Any) -> ApprovalStatus:
    """DE CHECK op een waarde: een oordeel geldt alleen voor de waarde waarover het ging."""
    block = state.read(data)
    if block is None:
        return ApprovalStatus.NONE
    if "value" in block and block.get("value") == value:
        return state.status(data)
    if LAST_APPROVED_KEY in block and block.get(LAST_APPROVED_KEY) == value:
        return ApprovalStatus.APPROVED
    return ApprovalStatus.NONE


def _record_value(
    state: _ApprovalState,
    data: dict[str, Any],
    value: Any,
    item: ApprovalItem,
    history_entry: dict[str, Any],
) -> None:
    """DE RECORD op een waarde: het oordeel landt bij de waarde waarover het ging.

    Een oordeel over een andere waarde dan er nu gevraagd wordt, schrijft niets: het
    project kan het veld wijzigen tussen het renderen van de lijst en het insturen.
    """
    if value is None or item.get("domain", "") != str(value):
        return
    status = item.get("status", "skip")
    fields: dict[str, Any] = {"status": status, "value": value}
    if status == ApprovalStatus.APPROVED.value:
        fields[LAST_APPROVED_KEY] = value
    state.write(data, fields, history_entry)
    if status == ApprovalStatus.DENIED.value and (state.read(data) or {}).get(LAST_APPROVED_KEY) == value:
        # Afwijzen van precies de staande goedkeuring neemt die terug. Zonder dit is een
        # goedgekeurde waarde niet in te trekken. Een afwijzing van een ANDERE gevraagde
        # waarde raakt de staande goedkeuring niet.
        (state.read(data) or {}).pop(LAST_APPROVED_KEY, None)


def _ensure_value_requested(state: _ApprovalState, data: dict[str, Any], value: Any) -> None:
    """DE AANVRAAG op een waarde: de waarde zetten IS de aanvraag."""
    if value is None:
        # Een besluit geldt alleen voor de stand waarvoor het is gegeven. Blijft het blok
        # staan als het veld terug naar de onschuldige stand gaat, dan geldt die
        # goedkeuring later opnieuw zonder besluit.
        state.clear(data)
        return
    block = state.read(data)
    last = (block or {}).get(LAST_APPROVED_KEY)
    if block is not None:
        if "value" in block and block.get("value") == value:
            return
        if "value" not in block and last == value:
            return
    # Een nieuwe waarde begint met een lege geschiedenis: de vorige oordelen gingen over een
    # andere waarde. De laatst goedgekeurde waarde gaat WEL mee: die geldt zolang deze
    # aanvraag openstaat.
    state.clear(data)
    if last is not None and last == value:
        state.write(data, {LAST_APPROVED_KEY: last})
        return
    fields: dict[str, Any] = {"status": ApprovalStatus.REQUESTED.value, "value": value}
    if last is not None:
        fields[LAST_APPROVED_KEY] = last
    state.write(data, fields)


def _value_item(
    *,
    field: str,
    block: dict[str, Any],
    value: Any,
    subject: str,
    name: str,
    extra: dict[str, Any] | None = None,
) -> ApprovalItem:
    """Het item voor de beheerpagina, met de GEVRAAGDE WAARDE als deel van zijn identiteit.

    De bewaking op goedkeuringsbesluiten (``validate_approval_decisions``) paart items op
    (dienst, type, domain, name), dus zo is een andere waarde een ander item en kan een
    goedkeuring niet meeliften op een waarde die intussen veranderde. ``name`` draagt het
    project, en op de deploymentlaag ook de deployment.
    """
    return {
        "type": field,
        "subject": subject,
        "domain": str(value),
        "name": name,
        "current_status": block.get("status", ""),
        "status": "skip",
        "history": block.get("history", []),
        **(extra or {}),
    }


@dataclass(frozen=True)
class ConfigValueApproval:
    """De complete vorm van "deze WAARDE mag pas gelden als iemand hem goedkeurt".

    Wat :func:`config_value_approval` teruggeeft. Het verschil met
    :class:`ServiceUseApproval` zit in waar het besluit over gaat: niet over de dienst,
    maar over een waarde in zijn config, en dus geldt een goedkeuring alleen voor de
    waarde die is goedgekeurd.

    Attributes:
        spec: de :class:`ApprovalSpec` voor ``config_approvals()``.
        is_approved: DE POORT: vraagt de config een besluit, en is dat er?
        approved_value: de gevraagde waarde als tekst als die is goedgekeurd, anders leeg.
        last_approved_value: de laatste waarde waarover een beheerder "ja" zei, ook als het
            project daarna iets anders vroeg. Leeg als er nooit iets is goedgekeurd.
        ensure_requested: de waarde zetten IS de aanvraag; dit legt hem vast.
            Toestandsvormig en dus idempotent.
    """

    spec: ApprovalSpec
    is_approved: Callable[[dict[str, Any]], bool]
    approved_value: Callable[[dict[str, Any]], str]
    last_approved_value: Callable[[dict[str, Any]], str]
    ensure_requested: Callable[[dict[str, Any]], None]


def config_value_approval(
    service_type: ServiceType,
    *,
    field: str,
    label: str,
    activity: str | Callable[[Any], str],
    consequence: str | Callable[[dict[str, Any], dict[str, Any]], str],
    required_when: Callable[[dict[str, Any]], bool] | None = None,
    requested_value: Callable[[dict[str, Any]], str] | None = None,
    subject: Callable[[dict[str, Any], str], str] | None = None,
    approver: ApproverScope = ApproverScope.PLATFORM_ADMIN,
) -> ConfigValueApproval:
    """De goedkeuring op een configwaarde: deze stand van dit veld vraagt een besluit.

    De tegenhanger van :func:`service_use_approval`. Daar is de dienst zelf het onderwerp
    en is er een besluit per project; hier is een WAARDE het onderwerp, vraagt alleen een
    gevraagde waarde een besluit, en geldt een goedkeuring voor die waarde. Verandert de
    waarde, dan ontstaat een nieuwe aanvraag.

    De toestand staat onder de dienst, op ``services/[<dienst>]/config/<veld>-approval``,
    met ``status``, de gevraagde ``value``, de laatst goedgekeurde ``approved-value`` en
    ``history``. Markeer dat veld als ``PLATFORM_MANAGED`` in het configmodel: een project
    dat zijn eigen status mag zetten heeft geen goedkeuring.

    Twee manieren om te zeggen WAT er gevraagd is, precies een van beide:

    - ``required_when``: een predicaat op de config. De waarde van ``field`` is de vraag
      zolang het predicaat waar is. Redis (``acl-key-prefix`` uit) werkt zo.
    - ``requested_value``: de gevraagde waarde als tekst, leeg als er niets gevraagd is. De
      dienst bepaalt wat "niets gevraagd" is: een leeg veld, of een getal binnen wat het
      project al mag. Send-email (afzenderadres, dagbudget) werkt zo.

    Terug naar de laatst goedgekeurde waarde is geen aanvraag: het blok houdt dan alleen
    die waarde vast, zonder open aanvraag. De status wordt daarbij NIET op ``approved``
    gezet, want een besluit komt alleen van de goedkeuringsroute
    (``validate_approval_decisions``).

    Args:
        service_type: de dienst waar het veld bij hoort.
        field: de sleutel in de projectconfig van de dienst waar het besluit over gaat.
        label: het opschrift van de aanvraag in de beheerpagina.
        activity: waar het besluit over gaat, als onderwerp van een zin. Een functie van
            de gevraagde waarde als die in de zin hoort ("Het afzenderadres ...").
        consequence: wat er zolang gebeurt, in de woorden van de dienst. Een functie van
            project en deployment als dat ervan afhangt; leeg mag.
        required_when: of de config zoals hij er staat een besluit vraagt.
        requested_value: de gevraagde waarde als tekst, leeg voor geen aanvraag.
        subject: hoe de aanvraag in de beheerpagina heet; standaard ``activity``.
        approver: wie mag beslissen.
    """
    if (required_when is None) == (requested_value is None):
        raise TypeError("config_value_approval vraagt precies een van required_when en requested_value")

    state = _ApprovalState(service_type.value, f"{field}{VALUE_APPROVAL_SUFFIX}")

    def _requested(project_data: dict[str, Any]) -> Any:
        """De waarde waarover een besluit nodig is, of None als er niets gevraagd is."""
        if requested_value is not None:
            return requested_value(project_data) or None
        config = state.service_config(project_data)
        return config.get(field) if required_when is not None and required_when(config) else None

    def _activity(value: Any) -> str:
        return activity(value) if callable(activity) else activity

    def _consequence(project_data: dict[str, Any], deployment: dict[str, Any]) -> str:
        return consequence(project_data, deployment) if callable(consequence) else consequence

    def _last_approved(project_data: dict[str, Any]) -> Any:
        return (state.read(project_data) or {}).get(LAST_APPROVED_KEY)

    def _status_of(project_data: dict[str, Any], value: Any) -> ApprovalStatus:
        return _status_of_value(state, project_data, value)

    def _is_approved(project_data: dict[str, Any]) -> bool:
        """DE POORT: staat het veld in een stand die een besluit vraagt, en is dat er?"""
        value = _requested(project_data)
        return value is not None and _status_of(project_data, value) is ApprovalStatus.APPROVED

    def _approved_value(project_data: dict[str, Any]) -> str:
        return str(_requested(project_data)) if _is_approved(project_data) else ""

    def _last_approved_value(project_data: dict[str, Any]) -> str:
        last = _last_approved(project_data)
        return "" if last is None else str(last)

    def _items(project_data: dict[str, Any]) -> list[ApprovalItem]:
        """De LIJST. Een item zolang er een aanvraag open staat of is beoordeeld."""
        block = state.read(project_data)
        value = _requested(project_data)
        if block is None or "value" not in block or value is None:
            return []
        return [
            _value_item(
                field=field,
                block=block,
                value=value,
                subject=subject(project_data, str(value)) if subject else _activity(value),
                name=project_data.get("name", ""),
            )
        ]

    def _record(project_data: dict[str, Any], item: ApprovalItem, history_entry: dict[str, Any]) -> None:
        """De RECORD. Het oordeel landt bij de waarde waarover het ging."""
        _record_value(state, project_data, _requested(project_data), item, history_entry)

    def _notices(project_data: dict[str, Any], deployment: dict[str, Any]) -> list[ApprovalNotice]:
        """De NOTICE. Wat de eigenaar te horen krijgt zolang de waarde niet geldt."""
        value = _requested(project_data)
        if not state.selected(project_data) or value is None:
            return []
        status = _status_of(project_data, value)
        if status is ApprovalStatus.APPROVED:
            return []
        return [
            _notice(
                state,
                project_data,
                subject=_activity(value),
                status=status,
                text=_notice_text(_activity(value), _consequence(project_data, deployment), status),
            )
        ]

    def _ensure_requested(project_data: dict[str, Any]) -> None:
        """De waarde zetten IS de aanvraag. Een andere waarde is een nieuwe aanvraag."""
        if not state.selected(project_data):
            return
        _ensure_value_requested(state, project_data, _requested(project_data))

    return ConfigValueApproval(
        spec=ApprovalSpec(
            key=field,
            label=label,
            approver=approver,
            status_of=_status_of,
            list_items=_items,
            record=_record,
            notices_for=_notices,
            requires_approval_for=(
                (lambda config: required_when(config if isinstance(config, dict) else {}))
                if required_when is not None
                else None
            ),
        ),
        is_approved=_is_approved,
        approved_value=_approved_value,
        last_approved_value=_last_approved_value,
        ensure_requested=_ensure_requested,
    )


#: Voorvoegsel van de spec-sleutel van een goedkeuring op de DEPLOYMENTlaag.
#:
#: Een eigen sleutel en niet die van het veld, want ``Service.approval_specs`` ontdubbelt
#: op sleutel: met dezelfde sleutel als de projectaanvraag zou de deploymentaanvraag
#: nergens meer in de beheerpagina opduiken.
DEPLOYMENT_APPROVAL_KEY_PREFIX = "deployment-"

#: Scheidingsteken tussen project en deployment in de identiteit van zo'n item.
#:
#: Een ``/`` komt in geen projectnaam en in geen deploymentnaam voor, dus een item van een
#: deployment valt nooit samen met het projectitem of met dat van een andere deployment.
#: Die identiteit is wat ``validate_approval_decisions`` paart, en wat de reseed in de
#: beheerpagina overhoudt.
DEPLOYMENT_ITEM_SEPARATOR = "/"


def deployment_item_name(project_name: str, deployment_name: str) -> str:
    """De identiteit van een deploymentitem in de beheerpagina: ``<project>/<deployment>``."""
    return f"{project_name}{DEPLOYMENT_ITEM_SEPARATOR}{deployment_name}"


@dataclass(frozen=True)
class DeploymentValueApproval:
    """Hetzelfde als :class:`ConfigValueApproval`, maar per DEPLOYMENT.

    Een eigen vorm en geen ``layer``-vlag op dezelfde dataclass, omdat de aanroepers een
    andere vraag stellen: niet "mag dit project dit", maar "mag DEZE deployment dit". Dat
    is een extra argument op elke poort, en een dataclass die beide signaturen moet dragen
    typeert geen van beide. De REGELS zijn gedeeld: beide fabrieken gebruiken
    ``_status_of_value``, ``_record_value`` en ``_ensure_value_requested``, dus een oordeel
    over een waarde werkt op beide lagen hetzelfde.

    Attributes:
        spec: de :class:`ApprovalSpec` voor ``config_approvals(ConfigLayer.DEPLOYMENT)``.
        is_approved: DE POORT: vraagt deze deployment een besluit, en is dat er?
        approved_value: de gevraagde waarde van deze deployment als die is goedgekeurd.
        last_approved_value: de laatste waarde waarover een beheerder voor deze deployment
            "ja" zei, ook als zij daarna iets anders vroeg.
        ensure_requested: legt de aanvraag van elke deployment vast. Toestandsvormig.
    """

    spec: ApprovalSpec
    is_approved: Callable[[dict[str, Any], str], bool]
    approved_value: Callable[[dict[str, Any], str], str]
    last_approved_value: Callable[[dict[str, Any], str], str]
    ensure_requested: Callable[[dict[str, Any]], None]


def deployment_value_approval(
    service_type: ServiceType,
    *,
    field: str,
    label: str,
    activity: str | Callable[[Any], str],
    consequence: str | Callable[[dict[str, Any], dict[str, Any]], str],
    requested_value: Callable[[dict[str, Any], str], str],
    subject: Callable[[dict[str, Any], str, str], str] | None = None,
    approver: ApproverScope = ApproverScope.PLATFORM_ADMIN,
) -> DeploymentValueApproval:
    """De goedkeuring op een configwaarde die EEN DEPLOYMENT overschrijft (RC-401).

    De tegenhanger van :func:`config_value_approval` op de deploymentlaag. Een project zet
    een waarde voor al zijn deployments; een deployment die daarvan afwijkt, vraagt een
    eigen besluit, en dat besluit is van die deployment alleen. Daardoor blijft een
    goedgekeurde afwijking staan als de projectwaarde later omlaag gaat, terwijl een
    deployment die ERFT wel meegaat.

    De toestand staat op ``deployments[*].services[<dienst>].config/<veld>-approval``, in
    dezelfde blokvorm als op het project: ``status``, de gevraagde ``value``, de laatst
    goedgekeurde ``approved-value`` en ``history``.

    Args:
        service_type: de dienst waar het veld bij hoort.
        field: de sleutel in de deploymentconfig van de dienst waar het besluit over gaat.
        label: het opschrift van de aanvraag in de beheerpagina.
        activity: waar het besluit over gaat, als onderwerp van een zin.
        consequence: wat er zolang gebeurt, in de woorden van de dienst.
        requested_value: de gevraagde waarde van EEN deployment als tekst, leeg voor geen
            aanvraag. De dienst bepaalt wat "niets gevraagd" is: een leeg veld, of een
            waarde die het project al mocht.
        subject: hoe de aanvraag heet in de beheerpagina, gegeven project, deployment en
            waarde; standaard ``activity`` met de deployment erbij.
        approver: wie mag beslissen.
    """
    state_key = f"{field}{VALUE_APPROVAL_SUFFIX}"
    service = service_type.value
    selection = _ApprovalState(service, state_key)

    def _deployment(project_data: dict[str, Any], deployment_name: str) -> dict[str, Any]:
        """De deployment met deze naam, of een leeg blok als het project hem niet kent."""
        index = deployment_index(project_data, deployment_name)
        if index is None:
            return {}
        deployment = project_data["deployments"][index]
        return deployment if isinstance(deployment, dict) else {}

    def _state(deployment_name: str) -> _ApprovalState:
        return _ApprovalState(service, state_key, holder=lambda pd: _deployment(pd, deployment_name))

    def _deployment_names(project_data: dict[str, Any]) -> list[str]:
        return [
            deployment["name"]
            for deployment in project_data.get("deployments") or []
            if isinstance(deployment, dict) and deployment.get("name")
        ]

    def _requested(project_data: dict[str, Any], deployment_name: str) -> Any:
        """De waarde waarover een besluit nodig is, of None als er niets gevraagd is."""
        return requested_value(project_data, deployment_name) or None

    def _activity(value: Any) -> str:
        return activity(value) if callable(activity) else activity

    def _subject(project_data: dict[str, Any], deployment_name: str, value: Any) -> str:
        if subject is not None:
            return subject(project_data, deployment_name, str(value))
        return f"{_activity(value)} voor deployment {deployment_name}"

    def _status_of(project_data: dict[str, Any], value: Any) -> ApprovalStatus:
        """De CHECK, met de deployment in ``value``: generieke code kent de laag niet.

        ``value`` is hier het paar (deployment, waarde). De generieke laag behandelt het
        als een ondoorzichtige waarde, precies zoals het domein/subdomein-paar van
        publish-on-web.
        """
        deployment_name, waarde = value
        return _status_of_value(_state(deployment_name), project_data, waarde)

    def _is_approved(project_data: dict[str, Any], deployment_name: str) -> bool:
        """DE POORT: vraagt deze deployment een besluit, en is dat er?"""
        value = _requested(project_data, deployment_name)
        return value is not None and _status_of(project_data, (deployment_name, value)) is ApprovalStatus.APPROVED

    def _approved_value(project_data: dict[str, Any], deployment_name: str) -> str:
        if not _is_approved(project_data, deployment_name):
            return ""
        return str(_requested(project_data, deployment_name))

    def _last_approved_value(project_data: dict[str, Any], deployment_name: str) -> str:
        last = (_state(deployment_name).read(project_data) or {}).get(LAST_APPROVED_KEY)
        return "" if last is None else str(last)

    def _items(project_data: dict[str, Any]) -> list[ApprovalItem]:
        """De LIJST. Een item per deployment met een open of beoordeelde aanvraag."""
        items: list[ApprovalItem] = []
        project_name = project_data.get("name", "")
        for deployment_name in _deployment_names(project_data):
            block = _state(deployment_name).read(project_data)
            value = _requested(project_data, deployment_name)
            if block is None or "value" not in block or value is None:
                continue
            items.append(
                _value_item(
                    field=f"{DEPLOYMENT_APPROVAL_KEY_PREFIX}{field}",
                    block=block,
                    value=value,
                    subject=_subject(project_data, deployment_name, value),
                    name=deployment_item_name(project_name, deployment_name),
                    extra={"deployment": deployment_name},
                )
            )
        return items

    def _deployment_of(item: ApprovalItem) -> str:
        """Welke deployment dit item is, uit de identiteit die de reseed overleeft."""
        return str(item.get("name", "")).partition(DEPLOYMENT_ITEM_SEPARATOR)[2]

    def _record(project_data: dict[str, Any], item: ApprovalItem, history_entry: dict[str, Any]) -> None:
        """De RECORD. Het oordeel landt bij de deployment en de waarde waarover het ging."""
        deployment_name = _deployment_of(item)
        if not deployment_name:
            return
        _record_value(
            _state(deployment_name),
            project_data,
            _requested(project_data, deployment_name),
            item,
            history_entry,
        )

    def _notices(project_data: dict[str, Any], deployment: dict[str, Any]) -> list[ApprovalNotice]:
        """De NOTICE. Wat de eigenaar van DEZE deployment te horen krijgt."""
        deployment_name = deployment.get("name") or ""
        value = _requested(project_data, deployment_name) if deployment_name else None
        if not selection.selected(project_data) or value is None:
            return []
        status = _status_of(project_data, (deployment_name, value))
        if status is ApprovalStatus.APPROVED:
            return []
        consequence_text = consequence(project_data, deployment) if callable(consequence) else consequence
        return [
            _notice(
                _state(deployment_name),
                project_data,
                subject=_subject(project_data, deployment_name, value),
                status=status,
                text=_notice_text(_activity(value), consequence_text, status),
            )
        ]

    def _ensure_requested(project_data: dict[str, Any]) -> None:
        """De waarde zetten IS de aanvraag, per deployment."""
        if not selection.selected(project_data):
            return
        for deployment_name in _deployment_names(project_data):
            _ensure_value_requested(
                _state(deployment_name),
                project_data,
                _requested(project_data, deployment_name),
            )

    return DeploymentValueApproval(
        spec=ApprovalSpec(
            key=f"{DEPLOYMENT_APPROVAL_KEY_PREFIX}{field}",
            label=label,
            approver=approver,
            status_of=_status_of,
            list_items=_items,
            record=_record,
            notices_for=_notices,
        ),
        is_approved=_is_approved,
        approved_value=_approved_value,
        last_approved_value=_last_approved_value,
        ensure_requested=_ensure_requested,
    )
