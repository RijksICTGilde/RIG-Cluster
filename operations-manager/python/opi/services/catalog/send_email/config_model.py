"""Typed config model for the ``send-email`` service.

Everything sits on the PROJECT layer: an account belongs to a project, not to one
container, and every component of the project that ticks the service uses that same
account. What a user decides is small on purpose -- the identity rules that make mail
arrive (envelope rewriting, the pinned ``From:`` domain, DKIM) are enforced on the relay
and are deliberately not negotiable per project (``plans/mailrelay.md``).

``accounts`` and ``approval`` are the platform's side of the block: OPI creates the account
on the relay and writes down what it created, and an administrator decides whether the
project may send at all. Both are marked ``PLATFORM_MANAGED`` so the API can neither clear
nor rewrite them -- the same protection the Keycloak realm block got, declared here from the
start instead of repaired afterwards (aanvulling 5 in the plan). ``approval`` in particular:
a project that could set its own status to ``approved`` would be no approval at all.

There is no field for the sender ADDRESS, and there deliberately is not going to be one:
the platform composes it from the project name and the relay writes it into the ``From:``
header itself. ``from-name`` (the display name) is all a project chooses -- and since
RC-145 it is actually READ, which is why it now carries validation: it goes straight into
a mail header.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from opi.services.config_managed import PLATFORM_MANAGED

#: Upper bound on a project's daily budget, and the number the relay really enforces: it
#: is the per-account rate in ``queue.limiter.inbound.account`` in the relay's configmap.
#: Change one and the other moves with it. It is also the agreement with the mail team --
#: the sum of the accounts must stay under the volume that was agreed, and a single project
#: asking for a number with an extra zero is exactly how that is broken.
MAX_MESSAGES_PER_DAY = 5000

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
#:   ``From:`` into a list of two mailboxes.
#:
#: The rule lives HERE and not in the form: this model is what the API writes against and
#: what a stored project file is validated with, and the form reuses it through
#: ``ModelFieldValidator`` so there is one definition and not two that drift.
FROM_NAME_PATTERN = r'^[^@<>"\\\x00-\x1F\x7F]*$'


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
                "characters, an @, angle brackets, quotes or a backslash, and it is at most "
                f"{MAX_FROM_NAME_LENGTH} characters. Leave it out to send without a display name."
            ),
        )
    )
    messages_per_day: Annotated[int, Field(ge=1, le=MAX_MESSAGES_PER_DAY)] | None = Field(
        default=None,
        alias="messages-per-day",
        description=(
            f"Messages this project may send per day, at most {MAX_MESSAGES_PER_DAY}. "
            "Leave it out for the platform default. This is the agreed budget, recorded on the account: "
            f"the relay itself enforces one ceiling of {MAX_MESSAGES_PER_DAY} per account per day, because "
            "an account on the relay carries no limit of its own."
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
    accounts: list[SendEmailAccount] = Field(
        default_factory=list,
        json_schema_extra={PLATFORM_MANAGED: True},
        description=(
            "Per-cluster SMTP accounts on the relay. Written and managed by the platform: carried over on a "
            "write, so a caller neither has to send it nor can lose it by leaving it out."
        ),
    )
