"""Typed config-modellen voor de dienst ``image-registries``.

Projectniveau draagt de registries zelf, componentniveau alleen een verwijzing bij naam.
Wat wij eruit afleiden (de RCR-URL, de secretnaam) wordt niet opgeslagen maar berekend,
zie ``naming.py``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

#: Een hostnaam met eventueel een pad, in kleine letters, zonder protocol en zonder tag.
#: Ook een veiligheidsgrendel: zonder patroon komt een waarde met een aanhalingsteken en
#: een regeleinde in ``upstreamRegistry`` van het Organization-manifest terecht.
#:
#: ``$`` en niet ``\Z``, want de rust-engine (waar pydantic op draait) kent ``\Z`` niet.
#: Het gecommitte fragment ``image-registries.v1.0.json`` draagt hetzelfde patroon naar de
#: Python-engine, waar ``$`` een afsluitende newline wel doorlaat; dat fragment is vandaag
#: drift-lock en geen poort, maar wie het wel als poort gebruikt hoort er ``\Z`` bij te
#: zetten.
_HOST_LABEL = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
_PORT = r"[0-9]{1,5}"
UPSTREAM_PATTERN = (
    rf"^(?:{_HOST_LABEL}(?:\.{_HOST_LABEL})+(?::{_PORT})?|{_HOST_LABEL}:{_PORT}|localhost)"
    r"(?:/[a-z0-9][a-z0-9._-]*)*$"
)

#: De uitleg bij dat patroon; de pydantic-melding zelf is Engels en praat over patronen.
UPSTREAM_MESSAGE = (
    "Vul de registry in als hostnaam met eventueel een pad, in kleine letters, zonder "
    "protocol en zonder tag, bijvoorbeeld code.overheid.nl/jouw-naam"
)

#: AGE-versleuteld, of expliciet als platte tekst gemarkeerd met ``plain:``. Niet alleen
#: een vormregel: ``find_plaintext_service_config_violations`` herkent een AGE-veld AAN
#: dit patroon en weigert platte tekst ook zonder ``enforce_validation``.
AGE_ENCRYPTED_OR_PLAIN_PATTERN = r"(-----BEGIN AGE ENCRYPTED FILE-----|^base64\+age:|^plain:)"


#: Een DNS-1123-achtige naam die met een kleine LETTER begint, zodat hij nooit als
#: YAML-getal gelezen wordt. In het model en niet in het formulier, zodat de API dezelfde
#: regel draagt.
REGISTRY_NAME_PATTERN = r"^[a-z]([-a-z0-9]*[a-z0-9])?$"

#: De uitleg bij dat patroon.
REGISTRY_NAME_MESSAGE = (
    "Registrynaam moet met een kleine letter beginnen en mag alleen kleine letters, "
    "cijfers en streepjes bevatten, geen spaties of hoofdletters"
)


class RegistryEntry(BaseModel):
    """Eén private registry van het project."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        pattern=REGISTRY_NAME_PATTERN,
        max_length=63,
        description="Naam waarmee een component naar deze registry verwijst, uniek binnen het project.",
    )
    upstream: str = Field(
        pattern=UPSTREAM_PATTERN,
        description=(
            "De registry inclusief pad waar de images staan, zonder protocol, "
            "bijvoorbeeld 'code.overheid.nl/robbert.uittenbroek'."
        ),
    )
    username: str | None = Field(
        default=None,
        description="Gebruikersnaam waarmee ZAD bij de registry inlogt; leeg voor een registry zonder inlog.",
    )
    password: str | None = Field(
        default=None,
        pattern=AGE_ENCRYPTED_OR_PLAIN_PATTERN,
        description="Het token dat leesrecht op de packages geeft, AGE-versleuteld opgeslagen.",
    )
    secret_name: str | None = Field(
        default=None,
        alias="secretName",
        description=(
            "Naam van een al bestaand kubernetes.io/dockerconfigjson-secret in de namespace, "
            "in plaats van gebruikersnaam en token. Voor een secret dat het platform zelf neerzet."
        ),
    )


class ImageRegistriesConfig(BaseModel):
    """Projectniveau: de lijst private registries."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    registries: list[RegistryEntry] = Field(
        default_factory=list,
        description="De private registries van dit project, met hun upstream en inloggegevens.",
    )


class ComponentRegistryConfig(BaseModel):
    """Component- en deployment-componentniveau: welke registry bij deze image hoort."""

    model_config = ConfigDict(extra="forbid")

    registry: str = Field(
        description=(
            "Naam van de registry uit de projectconfig van deze dienst waar de image van dit component vandaan komt."
        )
    )
