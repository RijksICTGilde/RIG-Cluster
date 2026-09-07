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

#: Wat een upstream mag zijn: een hostnaam (met een punt, of een poort, of ``localhost``),
#: eventueel gevolgd door een pad, in kleine letters, zonder protocol en zonder tag of
#: digest. De afnemer schrijft de UPSTREAM, nooit een RCR-URL, want dat houdt het bestand
#: overdraagbaar naar een ander platform.
#:
#: De regel staat HIER en niet in het formulier. Dit model is waar de API tegenaan
#: schrijft en waar een opgeslagen projectbestand mee wordt gevalideerd; het formulier
#: hergebruikt hem via ``ModelFieldValidator``. Er is dus een definitie en geen tweeling
#: die uit elkaar loopt -- de vorige vorm had de regel alleen in de formulierlaag, en
#: ``POST /projects/{p}/registries/by-credentials`` kwam er niet langs.
#:
#: Het is ook een veiligheidsgrendel. ``$defs/registry.url`` in project_v2.json droeg
#: ``^(?:(?:https?|ssh|git)://)?[^\s\u0000"]+\Z`` zolang dit veld daar stond, en een
#: sleutel die verhuist neemt zijn constraints mee: zonder patroon komt een waarde met een
#: aanhalingsteken en een regeleinde in ``upstreamRegistry`` van het Organization-manifest
#: terecht en staan er extra DOCUMENTEN in dat bestand. Dit patroon is strikter dan het
#: oude en laat allebei de waarden door die de vloot werkelijk heeft (``ghcr.io`` en
#: ``rcr.rijksapps.nl/rig``).
#:
#: ``$`` en niet ``\Z``: de standaard rust-regex-engine kent ``\Z`` niet, en zijn ``$``
#: bindt aan het EINDE van de tekst (anders dan die van Python, die een afsluitende
#: newline doorlaat). Zo draaien het model en de ``ModelFieldValidator`` van het formulier
#: op dezelfde engine met dezelfde uitkomst.
#:
#: Let op wie dit patroon nog meer draagt: het gecommitte fragment
#: ``image-registries.v1.0.json`` krijgt dezelfde tekst mee, en wie dat fragment met de
#: PYTHON-engine valideert krijgt een andere uitkomst -- daar laat ``$`` een afsluitende
#: newline wel door (``ghcr.io\n`` matcht). Vandaag is dat inert, want het fragment is een
#: drift-lock en documentatie (``test_service_config_schema.py``) en geen validatiepoort;
#: de poort die draait is dit model. Gaat iemand het fragment wel als poort gebruiken, dan
#: hoort daar ``\Z``/``fullmatch`` bij.
_HOST_LABEL = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
_PORT = r"[0-9]{1,5}"
UPSTREAM_PATTERN = (
    rf"^(?:{_HOST_LABEL}(?:\.{_HOST_LABEL})+(?::{_PORT})?|{_HOST_LABEL}:{_PORT}|localhost)"
    r"(?:/[a-z0-9][a-z0-9._-]*)*$"
)

#: De uitleg die bij dat patroon hoort. Staat naast de regel zelf, zodat het formulier hem
#: kan tonen zonder de regel opnieuw op te schrijven (de pydantic-melding is Engels en
#: praat over patronen).
UPSTREAM_MESSAGE = (
    "Vul de registry in als hostnaam met eventueel een pad, in kleine letters, zonder "
    "protocol en zonder tag, bijvoorbeeld code.overheid.nl/jouw-naam"
)

#: Wat een opgeslagen geheim mag zijn: AGE-versleuteld, of expliciet als platte tekst
#: gemarkeerd met ``plain:``. Gelijk aan ``$defs/age-encrypted-or-plain`` in
#: ``project_v2.json``, dat dit veld droeg zolang het daar stond. Het is niet alleen een
#: vormregel: ``find_plaintext_service_config_violations`` herkent een AGE-veld AAN dit
#: patroon en weigert daarop ook op de schrijfroutes met ``enforce_validation=False``.
AGE_ENCRYPTED_OR_PLAIN_PATTERN = r"(-----BEGIN AGE ENCRYPTED FILE-----|^base64\+age:|^plain:)"


class RegistryEntry(BaseModel):
    """Eén private registry van het project."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Naam waarmee een component naar deze registry verwijst, uniek binnen het project.")
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
