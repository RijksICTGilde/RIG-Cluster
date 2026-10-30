"""Typed config models for the ``send-email`` service.

Two layers since RC-401. The PROJECT layer decides for every deployment: whether an
administrator allowed the project to send at all, the display name, the sender address and
the daily budget. A DEPLOYMENT may override the last three, because the account is per
deployment and so is its budget. What a deployment leaves out, it inherits.

Rules that make mail arrive (envelope, pinned domain, DKIM) are enforced on the relay and
are not configurable (``plans/mailrelay.md``). The account's own credentials are not here
either: the password lives in that deployment's Kubernetes secret, like its database
password.

The approval blocks are platform-managed (``PLATFORM_MANAGED``), so the API can neither
clear nor rewrite them. The three fields a project or deployment chooses go straight into
mail headers and therefore carry validation.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

from opi.services.config_managed import PLATFORM_MANAGED
from opi.utils.naming import MAIL_LOCAL_PART_MAX_LENGTH, MAIL_SENDER_DOMAIN_ALLOWLIST, mail_sender_address_error

#: Schemaplafond tegen tikfouten, niet tegen te grote aanvragen. De relay dwingt het getal
#: per account af (``queue.limiter.inbound.zad-<account>``), dus dit getal is niet meer het
#: echte plafond.
MAX_MESSAGES_PER_DAY = 1_000_000

#: Longest display name a project may choose. Not a technical ceiling (RFC 5322 allows a
#: much longer one) but a human one: a name is what the recipient reads next to the
#: address, and anything past this is not a name any more.
MAX_FROM_NAME_LENGTH = 64

#: What a display name may NOT contain, and every character in it is here for a reason.
#: The name is pasted straight into the ``From:`` header by the relay, so:
#:
#: * control characters (``\r``, ``\n``, and the rest of the C0/C1 range) would end the
#:   header and start another one -- header injection, the classic one;
#: * ``@`` and the angle brackets make a name READ like an address. "beveiliging@bank.nl"
#:   as a display name shows up as the sender in many mail clients, next to an address the
#:   reader never sees;
#: * ``"`` and ``\`` would break out of the quoting the relay puts around the name, and
#:   that quoting is what makes a comma or a colon in a name safe instead of turning the
#:   ``From:`` into a list of two mailboxes;
#: * ``$`` is what a sieve string interpolates (``${...}``), and the relay writes the name
#:   into a GENERATED sieve script. This character is here for the layer BELOW this one:
#:   ``opi.connectors.mail`` refuses it when it renders that script, and a name that only
#:   this rule let through would blow up halfway through processing a project instead of
#:   being refused at the form. Keep the two lists identical.
#:
#: The rule lives HERE and not in the form: this model is what the API writes against and
#: what a stored project file is validated with, and the form reuses it through
#: ``ModelFieldValidator`` so there is one definition and not two that drift.
FROM_NAME_PATTERN = r'^[^@<>"$\\\x00-\x1F\x7F]*$'


def _valid_sender_address(address: str) -> str:
    """De regel uit ``opi.utils.naming``, zodat het model en de connector er een delen."""
    error = mail_sender_address_error(address)
    if error:
        raise ValueError(error)
    return address


#: De uitleg die het formulier en de API geven als een adres niet mag. Een zin voor alle
#: weigeringen samen: de pydantic-melding is Engels en praat over patronen.
_ALLOWED_DOMAINS = ", ".join(sorted(MAIL_SENDER_DOMAIN_ALLOWLIST))
FROM_ADDRESS_MESSAGE = (
    f"Het afzenderadres moet een adres in {_ALLOWED_DOMAINS} zijn, met voor de @ hoogstens "
    f"{MAIL_LOCAL_PART_MAX_LENGTH} tekens uit kleine letters, cijfers en . _ + - "
    "(bijvoorbeeld noreply@rijksoverheid.nl). Een ander domein lijnt niet uit met de "
    "envelope en komt niet door DMARC."
)


class SendEmailApproval(BaseModel):
    """Whether an administrator has allowed this project to send mail (aanvulling 6).

    Shaped like the domain/subdomain entries publish-on-web stores: a status plus the
    verdict history, so the file is the audit trail. Written only through the generic
    approval flow (``opi/services/approvals.py``).
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    status: str = Field(description="requested, approved or denied. Written by the platform's approval flow.")
    history: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Verdict history: one {date, status, by, message} entry per decision.",
    )


class SendEmailValueApproval(BaseModel):
    """Whether an administrator approved a requested VALUE (address or budget).

    Hangs on the VALUE, not the project: if approved for one value and the project changes
    it, that is a new request. Both address and budget use this shape; both are text so a
    single shape fits both.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    value: str = Field(description="The value this verdict is about. Written by the platform's approval flow.")
    status: str = Field(description="requested, approved or denied. Written by the platform's approval flow.")
    approved_value: str | None = Field(
        default=None,
        alias="approved-value",
        description="The last value an administrator approved, which keeps applying while a new request is open.",
    )
    history: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Verdict history: one {date, status, by, message} entry per decision.",
    )


#: De drie velden die een project kiest en een deployment mag overschrijven. Als fabrieken,
#: zodat de twee lagen EEN definitie delen: de regels gaan over een mailheader, en twee
#: beschrijvingen die uit elkaar lopen zijn twee regels.
_FromName = Annotated[str, StringConstraints(max_length=MAX_FROM_NAME_LENGTH, pattern=FROM_NAME_PATTERN)]
_FromAddress = Annotated[str, AfterValidator(_valid_sender_address)]
_MessagesPerDay = Annotated[int, Field(ge=1, le=MAX_MESSAGES_PER_DAY)]


def _from_name_field(whose: str) -> Any:
    return Field(
        default=None,
        alias="from-name",
        description=(
            f"Display name shown to the recipient, e.g. 'Algoritmeregister'. The relay puts this "
            f"in the From: header next to the sender address, so it may not contain control "
            f"characters, an @, angle brackets, quotes, a backslash or a dollar sign, and it is "
            f"at most {MAX_FROM_NAME_LENGTH} characters. Leave it out {whose}."
        ),
    )


def _from_address_field(whose: str) -> Any:
    return Field(
        default=None,
        alias="from-address",
        description=(
            f"Sender address asked to send from, e.g. 'noreply@rijksoverheid.nl'. It only takes "
            f"effect once a platform administrator has approved that exact value; until then the "
            f"platform composes the address itself. Leave it out {whose}. {FROM_ADDRESS_MESSAGE}"
        ),
    )


def _messages_per_day_field(whose: str) -> Any:
    return Field(
        default=None,
        alias="messages-per-day",
        description=(
            f"Messages per day, enforced by the relay per account. Up to and including the platform "
            f"default this is a free choice; above that it is a REQUEST requiring approval. Leave it "
            f"out {whose}. The {MAX_MESSAGES_PER_DAY} ceiling only catches typos."
        ),
    )


def _value_approval_field(alias: str, description: str) -> Any:
    return Field(default=None, alias=alias, json_schema_extra={PLATFORM_MANAGED: True}, description=description)


class SendEmailConfig(BaseModel):
    """What a project may decide about the outgoing mail of all its deployments."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_name: _FromName | None = _from_name_field("to send without a display name")
    from_address: _FromAddress | None = _from_address_field("for the composed address")
    messages_per_day: _MessagesPerDay | None = _messages_per_day_field("for the platform default")
    approval: SendEmailApproval | None = Field(
        default=None,
        json_schema_extra={PLATFORM_MANAGED: True},
        description=(
            "Whether an administrator has allowed this project to send mail. Written by the platform: "
            "until it says approved, no account is created, no network policy is written and no SMTP "
            "credentials reach the deployment."
        ),
    )
    from_address_approval: SendEmailValueApproval | None = _value_approval_field(
        "from-address-approval",
        "Whether an administrator has allowed the sender address this project requested. Written by the "
        "platform: until it says approved for exactly the address in from-address, the relay keeps "
        "deriving the address from the account name.",
    )
    messages_per_day_approval: SendEmailValueApproval | None = _value_approval_field(
        "messages-per-day-approval",
        "Whether an administrator approved the budget this project requested. Until approved for exactly "
        "the requested number, the relay enforces the last approved number or the default.",
    )


class SendEmailDeploymentConfig(BaseModel):
    """What ONE deployment may decide about its own outgoing mail (RC-401).

    The account is per deployment, so the budget and the sender are too. A field left out
    is inherited from the project; a field that is filled in is this deployment's own, and
    an address or a budget above the platform default is its own REQUEST. That separation
    is deliberate: a deployment with an approved override keeps it when the project lowers
    its own value, while a deployment that inherits follows the project down.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_name: _FromName | None = _from_name_field("to inherit the project's display name")
    from_address: _FromAddress | None = _from_address_field("to inherit the project's address")
    messages_per_day: _MessagesPerDay | None = _messages_per_day_field("to inherit the project's budget")
    from_address_approval: SendEmailValueApproval | None = _value_approval_field(
        "from-address-approval",
        "Whether an administrator has allowed the sender address this deployment requested. Written by "
        "the platform: until it says approved for exactly the address in from-address, this deployment "
        "sends from the inherited address.",
    )
    messages_per_day_approval: SendEmailValueApproval | None = _value_approval_field(
        "messages-per-day-approval",
        "Whether an administrator approved the budget this deployment requested. Until approved for "
        "exactly the requested number, the relay enforces the last approved override or the inherited "
        "budget.",
    )
