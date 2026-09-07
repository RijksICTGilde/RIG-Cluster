"""Typed config-modellen voor de dienst ``image-registries``.

Twee lagen, twee modellen:

* **Projectniveau** (``ImageRegistriesConfig``): de private registries die het project
  zelf opgeeft -- naam, upstream inclusief pad, gebruikersnaam en token. Meer staat er
  niet in, en er wordt ook niets bij teruggeschreven: de RCR-URL en de naam van het
  pull-secret zijn een functie van de projectnaam en de upstream en worden berekend op
  het moment dat een manifest wordt gegenereerd (zie ``naming.py``).
* **Componentniveau** (``ComponentRegistryConfig``): welke registry bij de image van dit
  component hoort. Alleen een verwijzing bij naam, en alleen als er iets te verwijzen
  valt. "Publieke registry" is een non-waarde: dan staat de dienst niet bij het component
  en is er dus ook geen configblok.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class RegistryEntry(BaseModel):
    """Eén private registry van het project."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Naam waarmee een component naar deze registry verwijst, uniek binnen het project.")
    upstream: str = Field(
        description=(
            "De registry inclusief pad waar de images staan, zonder protocol, "
            "bijvoorbeeld 'code.overheid.nl/robbert.uittenbroek'."
        )
    )
    username: str | None = Field(
        default=None,
        description="Gebruikersnaam waarmee ZAD bij de registry inlogt; leeg voor een registry zonder inlog.",
    )
    password: str | None = Field(
        default=None,
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
