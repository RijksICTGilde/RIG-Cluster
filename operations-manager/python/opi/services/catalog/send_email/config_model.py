"""Typed config model for the ``send-email`` service.

Everything sits on the PROJECT layer: an account belongs to a project, not to one
container, and every component of the project that ticks the service uses that same
account. What a user decides is small on purpose -- the identity rules that make mail
arrive (envelope rewriting, the pinned ``From:`` domain, DKIM) are enforced on the relay
and are deliberately not negotiable per project (``plans/mailrelay.md``).

``accounts`` is the platform's side of the block: OPI creates the account on the relay and
writes down what it created. It is marked ``PLATFORM_MANAGED`` so the API can neither clear
nor rewrite it -- the same protection the Keycloak realm block got, declared here from the
start instead of repaired afterwards (aanvulling 5 in the plan).
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from opi.services.config_managed import PLATFORM_MANAGED

#: Upper bound on a project's daily budget. Not a technical limit but the agreement with
#: the mail team: the sum of the accounts must stay under the volume that was agreed, and
#: a single project asking for a number with an extra zero is exactly how that is broken.
MAX_MESSAGES_PER_DAY = 5000

#: The local part of an address, as the relay will enforce it. Expressed in the ANNOTATION
#: rather than in a ``field_validator`` so the form can reuse the very same rule through
#: ``ModelFieldValidator`` -- a hand-written copy next to a model is a second definition of
#: one rule, and the two drift (see that validator's docstring).
MailLocalPart = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z0-9]([a-z0-9._-]{0,62}[a-z0-9])?$"),
]


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


class SendEmailConfig(BaseModel):
    """What a project may decide about its outgoing mail."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_name: str | None = Field(
        default=None,
        alias="from-name",
        description="Display name shown to the recipient, e.g. 'Algoritmeregister'. The address itself is fixed.",
    )
    from_local_part: MailLocalPart | None = Field(
        default=None,
        alias="from-local-part",
        description="Local part of the sender address (the bit before the @), e.g. 'noreply'. Defaults to 'noreply'.",
    )
    from_domain: str | None = Field(
        default=None,
        alias="from-domain",
        description=(
            "Sender domain, when the project has one of its own. Leave it out for the platform mail domain. "
            "A domain of your own needs a DKIM record in its zone first, so it is not yet self-service."
        ),
    )
    messages_per_day: Annotated[int, Field(ge=1, le=MAX_MESSAGES_PER_DAY)] | None = Field(
        default=None,
        alias="messages-per-day",
        description=(
            f"Messages this project may send per day, at most {MAX_MESSAGES_PER_DAY}. "
            "Leave it out for the platform default."
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
