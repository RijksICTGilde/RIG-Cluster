"""Typed config model for the ``send-email`` service.

Everything sits on the PROJECT layer: one account per project, used by all components.
Rules that make mail arrive (envelope, pinned domain, DKIM) are enforced on the relay,
not configurable per project (``plans/mailrelay.md``).

``accounts``, ``approval``, ``from-address-approval`` and ``messages-per-day-approval``
are platform-managed:
``PLATFORM_MANAGED`` marked so the API can neither clear nor rewrite them. A project
chooses two things that go straight to mail headers and therefore carry validation:
``from-name`` and ``from-address`` (latter only takes effect after admin approval).
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


class SendEmailAccount(BaseModel):
    """One SMTP account on the relay, as OPI created it (one per cluster).

    Shaped like ``KeycloakRealm``: matched on ``cluster``, written by the platform, and
    the only place this account's password lives. ``extra="allow"`` so a field added later
    does not make existing project files unreadable.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    cluster: str = Field(description="Cluster this account was created for. Written by the platform.")
    username: str = Field(description="SASL username the application authenticates to the relay with.")
    password: str = Field(description="That account's password, AGE-encrypted or 'plain:'-prefixed.")
    from_address: str = Field(
        alias="from-address",
        description="Address the relay pins the envelope sender and the From: header to for this account.",
    )
    bounce_address: str = Field(
        alias="bounce-address",
        description="Address bounces for this account return to; carries the project name so a bounce is traceable.",
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


class SendEmailConfig(BaseModel):
    """What a project may decide about its outgoing mail."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_name: Annotated[str, StringConstraints(max_length=MAX_FROM_NAME_LENGTH, pattern=FROM_NAME_PATTERN)] | None = (
        Field(
            default=None,
            alias="from-name",
            description=(
                "Display name shown to the recipient, e.g. 'Algoritmeregister'. The relay puts this "
                "in the From: header next to the project's address, so it may not contain control "
                "characters, an @, angle brackets, quotes, a backslash or a dollar sign, and it is "
                f"at most {MAX_FROM_NAME_LENGTH} characters. Leave it out to send without a display name."
            ),
        )
    )
    from_address: Annotated[str, AfterValidator(_valid_sender_address)] | None = Field(
        default=None,
        alias="from-address",
        description=(
            "Sender address this project asks to send from, e.g. 'noreply@rijksoverheid.nl'. It only "
            "takes effect once a platform administrator has approved that exact value; until then the "
            "platform composes the address from the project name. Leave it out for the composed address. "
            f"{FROM_ADDRESS_MESSAGE}"
        ),
    )
    messages_per_day: Annotated[int, Field(ge=1, le=MAX_MESSAGES_PER_DAY)] | None = Field(
        default=None,
        alias="messages-per-day",
        description=(
            "Messages per day, enforced by the relay per account. Up to and including the platform "
            "default, the project chooses; above that is a REQUEST requiring approval. The "
            f"{MAX_MESSAGES_PER_DAY} ceiling only catches typos."
        ),
    )
    approval: SendEmailApproval | None = Field(
        default=None,
        json_schema_extra={PLATFORM_MANAGED: True},
        description=(
            "Whether an administrator has allowed this project to send mail. Written by the platform: "
            "until it says approved, no account is created, no network policy is written and no SMTP "
            "credentials reach the deployment."
        ),
    )
    from_address_approval: SendEmailValueApproval | None = Field(
        default=None,
        alias="from-address-approval",
        json_schema_extra={PLATFORM_MANAGED: True},
        description=(
            "Whether an administrator has allowed the requested sender address. Written by the platform: "
            "until it says approved for exactly the address in from-address, the relay keeps deriving the "
            "address from the project name."
        ),
    )
    messages_per_day_approval: SendEmailValueApproval | None = Field(
        default=None,
        alias="messages-per-day-approval",
        json_schema_extra={PLATFORM_MANAGED: True},
        description=(
            "Whether an administrator approved the requested budget. Until approved for exactly the "
            "requested number, the relay enforces the last approved number or the default."
        ),
    )
    accounts: list[SendEmailAccount] = Field(
        default_factory=list,
        json_schema_extra={PLATFORM_MANAGED: True},
        description=(
            "Per-cluster SMTP accounts on the relay. Written and managed by the platform: carried over on a "
            "write, so a caller neither has to send it nor can lose it by leaving it out."
        ),
    )
