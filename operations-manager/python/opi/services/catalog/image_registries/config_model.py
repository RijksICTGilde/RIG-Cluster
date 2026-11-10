"""Typed config models for the ``image-registries`` service.

The project layer carries the registries themselves, the component layer only a reference by
name. What we derive from them (the RCR URL, the secret name) is not stored but computed, see
``naming.py``.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationInfo, model_validator

from opi.services.catalog.image_registries.upstream import normalize_upstream
from opi.services.catalog.shared.storage import STORED_CONTEXT_KEY
from opi.services.config_managed import PLATFORM_MANAGED
from opi.utils.age import AGE_ENCRYPTED_PATTERN

#: A host name with an optional path, lowercase, without protocol and without tag. Also a
#: safety guard, see features/image-registries.md.
#:
#: ``$`` and not ``\Z``, because the rust engine pydantic runs on does not know ``\Z``. In the
#: Python engine ``$`` lets a trailing newline through: whoever starts using the
#: ``image-registries.v1.0.json`` fragment as a guard adds ``\Z``.
_HOST_LABEL = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
_PORT = r"[0-9]{1,5}"
UPSTREAM_PATTERN = (
    rf"^(?:{_HOST_LABEL}(?:\.{_HOST_LABEL})+(?::{_PORT})?|{_HOST_LABEL}:{_PORT}|localhost)"
    r"(?:/[a-z0-9][a-z0-9._-]*)*$"
)

#: The explanation for that pattern; pydantic's own message is English and talks about patterns.
UPSTREAM_MESSAGE = (
    "Vul de registry in als hostnaam met eventueel een pad, in kleine letters, zonder "
    "protocol en zonder tag, bijvoorbeeld code.overheid.nl/jouw-naam"
)

#: The key of the quota on a registry, shared by the model and its approval.
QUOTA_FIELD = "quota-gib"

#: Up to and including this value a project picks the quota itself; above it is a request.
QUOTA_FREE_MAX_GIB = 10

#: The ceiling. An ``le`` on the model rather than an ``IntegerSetting``: ``config_settings``
#: cannot look inside a list, and the quota is per registry.
QUOTA_MAX_GIB = 20

#: A DNS-1123-like name starting with a lowercase LETTER, so it is never read as a YAML
#: number.
REGISTRY_NAME_PATTERN = r"^[a-z]([-a-z0-9]*[a-z0-9])?$"

#: The explanation for that pattern.
REGISTRY_NAME_MESSAGE = (
    "Registrynaam moet met een kleine letter beginnen en mag alleen kleine letters, "
    "cijfers en streepjes bevatten, geen spaties of hoofdletters"
)

#: An RFC-1123 subdomain name: exactly what kubernetes lets a secret be called. Like
#: ``UPSTREAM_PATTERN`` a safety guard, and with ``$`` for the same reason.
SECRET_NAME_PATTERN = r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$"  # noqa: S105  # a shape rule for secret names, not a secret


def _normalize_upstream(value: object, info: ValidationInfo) -> object:
    """The input aid, BEFORE the shape rule: what a consumer pastes is rarely the upstream.

    Not under ``STORED_CONTEXT_KEY``: validating does not write back, so a stored
    ``https://ghcr.io`` would pass the whole-file guard and stay unchanged, after which
    ``normalize_prefix`` never matches it and the registry silently stops applying.
    """
    if info.context and info.context.get(STORED_CONTEXT_KEY):
        return value
    return normalize_upstream(value) if isinstance(value, str) else value


#: As an annotated type and not as a separate ``field_validator``, because
#: ``ModelFieldValidator`` builds its check from the ANNOTATION: otherwise the form rejects a
#: pasted URL the API accepts. The pattern comes BEFORE the before-validator, otherwise it
#: falls out of the rendered JSON schema (pydantic then describes the input) while the check
#: keeps running.
Upstream = Annotated[str, Field(pattern=UPSTREAM_PATTERN), BeforeValidator(_normalize_upstream)]


class RegistryEntry(BaseModel):
    """One private registry of the project."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str | None = Field(
        default=None,
        pattern=REGISTRY_NAME_PATTERN,
        max_length=63,
        description=(
            "De verwijzing waarmee een component deze registry aanwijst, uniek binnen het project. "
            "Laat hem weg en geef een 'display-name': het platform leidt hem daar dan uit af. "
            "Hij ligt vast zodra hij bestaat, want componenten wijzen ernaar en hij zit in de naam "
            "van het pull-secret; een gewijzigd label verandert hem niet mee."
        ),
    )
    display_name: str | None = Field(
        default=None,
        max_length=255,
        alias="display-name",
        description="Het label dat je op het scherm ziet; vrije tekst, dus geen DNS-label nodig.",
    )
    upstream: Upstream = Field(
        description=(
            "De registry inclusief pad waar de images staan, zonder protocol, "
            "bijvoorbeeld 'code.overheid.nl/robbert.uittenbroek'. Een geplakte browser-URL of "
            "een volledige image-referentie wordt omgezet naar deze vorm."
        ),
    )
    username: str | None = Field(
        default=None,
        description=(
            "Gebruikersnaam waarmee ZAD bij de registry inlogt, naast 'password'. Optioneel: bij "
            "GitHub (ghcr.io) doet de waarde er niet toe, bij Docker Hub is het de accountnaam en bij "
            "Quay de naam van het robotaccount. Leeg blijft leeg in het projectbestand; het pull-secret "
            "krijgt dan een plaatshouder. Eist de registry een echte naam, dan mislukt het ophalen van "
            "de images. Via de API wordt het token niet getoetst."
        ),
    )
    password: str | None = Field(
        default=None,
        pattern=AGE_ENCRYPTED_PATTERN,
        description="Het token dat leesrecht op de packages geeft, AGE-versleuteld opgeslagen.",
    )
    secret_name: str | None = Field(
        default=None,
        pattern=SECRET_NAME_PATTERN,
        max_length=253,
        alias="secretName",
        description=(
            "Naam van een al bestaand kubernetes.io/dockerconfigjson-secret in de namespace, "
            "in plaats van gebruikersnaam en token. Voor een secret dat het platform zelf neerzet."
        ),
    )

    quota_gib: int | None = Field(
        default=None,
        alias=QUOTA_FIELD,
        ge=1,
        le=QUOTA_MAX_GIB,
        description=(
            f"Opslagquota in GiB van de proxy-cache van deze registry in RCR. Laat hem weg voor de "
            f"standaard van het cluster. Tot en met {QUOTA_FREE_MAX_GIB} kies je zelf; daarboven, tot "
            f"{QUOTA_MAX_GIB}, is het een aanvraag die een platformbeheerder goedkeurt. Tot dan geldt "
            f"{QUOTA_FREE_MAX_GIB}, of de laatst goedgekeurde waarde."
        ),
    )

    @model_validator(mode="after")
    def _has_something_to_be_called(self) -> RegistryEntry:
        """An entry without a name AND without a label cannot be referenced at all.

        Without either there is no name to derive and the entry is invisible to
        ``project_registries``, so to the whole service.
        """
        if not self.name and not self.display_name:
            msg = "Geef een 'name' of een 'display-name'"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _has_exactly_one_way_to_pull(self) -> RegistryEntry:
        """An entry carries EITHER a ``secretName`` OR a token.

        With neither it passes everything and the backend silently writes no pull secret; the
        consumer only notices when the pod cannot pull, with a message that is not about a
        missing token. Mixing is impossible too: with a ``secretName`` the backend skips
        username and token.

        The username is optional, see ``PULL_USERNAME_PLACEHOLDER``.
        """
        if self.secret_name and (self.username or self.password):
            msg = "Geef een 'secretName' OF een 'username' met 'password', niet allebei"
            raise ValueError(msg)
        if not self.secret_name and not self.password:
            if self.username:
                msg = "Vul een token in bij de gebruikersnaam, anders kunnen we de images niet ophalen"
            else:
                msg = "Vul een token in, anders kunnen we de images niet ophalen"
            raise ValueError(msg)
        return self


class QuotaApproval(BaseModel):
    """Whether a platform administrator approved the quota of one registry.

    Same shape as the value approvals of send-email, but ``value`` and ``status`` stay
    optional: lowering back to an already approved quota leaves a block with only
    ``approved-value``, which send-email's required fields would reject. Written by the
    approval flow (``opi/services/approvals.py``).
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    value: str | None = Field(default=None, description="The quota this verdict is about.")
    status: str | None = Field(default=None, description="requested, approved or denied.")
    approved_value: str | None = Field(
        default=None,
        alias="approved-value",
        description="The last approved quota, which keeps applying while a new request is open.",
    )
    history: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Verdict history: one {date, status, by, message} entry per decision.",
    )


class ImageRegistriesConfig(BaseModel):
    """Project layer: the list of private registries."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    registries: list[RegistryEntry] = Field(
        default_factory=list,
        description="De private registries van dit project, met hun upstream en inloggegevens.",
    )
    quota_gib_approval: dict[str, QuotaApproval] | None = Field(
        default=None,
        alias=f"{QUOTA_FIELD}-approval",
        json_schema_extra={PLATFORM_MANAGED: True},
        description=(
            "Per registry name: whether the requested quota is approved. Written by the platform. Here and "
            "not on the registry itself, because only a field at this level is protected against writes."
        ),
    )


class ComponentRegistryConfig(BaseModel):
    """Component and deployment-component layer: which registry this image belongs to."""

    model_config = ConfigDict(extra="forbid")

    registry: str = Field(
        min_length=1,
        description=(
            "Naam van de registry uit de projectconfig van deze dienst waar de image van dit component vandaan komt. "
            "Er is geen lege waarde: een component zonder deze dienstvermelding gebruikt de registry van dit project "
            "waar zijn image onder valt, en anders de publieke weg. "
            "Gebruik DELETE om die keuze terug te draaien; dat haalt de vermelding weg."
        ),
    )
