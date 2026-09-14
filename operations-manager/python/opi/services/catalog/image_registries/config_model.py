"""Typed config-modellen voor de dienst ``image-registries``.

Projectniveau draagt de registries zelf, componentniveau alleen een verwijzing bij naam.
Wat wij eruit afleiden (de RCR-URL, de secretnaam) wordt niet opgeslagen maar berekend,
zie ``naming.py``.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationInfo, model_validator

from opi.services.catalog.image_registries.upstream import normalize_upstream
from opi.services.catalog.shared.storage import STORED_CONTEXT_KEY

#: Een hostnaam met eventueel een pad, in kleine letters, zonder protocol en zonder tag.
#: Ook een veiligheidsgrendel, zie features/image-registries.md.
#:
#: ``$`` en niet ``\Z``, want de rust-engine (waar pydantic op draait) kent ``\Z`` niet.
#: In de Python-engine laat ``$`` een afsluitende newline door: wie het fragment
#: ``image-registries.v1.0.json`` als poort gaat gebruiken, zet er ``\Z`` bij.
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
#: YAML-getal gelezen wordt.
REGISTRY_NAME_PATTERN = r"^[a-z]([-a-z0-9]*[a-z0-9])?$"

#: De uitleg bij dat patroon.
REGISTRY_NAME_MESSAGE = (
    "Registrynaam moet met een kleine letter beginnen en mag alleen kleine letters, "
    "cijfers en streepjes bevatten, geen spaties of hoofdletters"
)

#: Een RFC-1123-subdomeinnaam: precies wat kubernetes een secret laat heten. Net als
#: ``UPSTREAM_PATTERN`` een veiligheidsgrendel, en met ``$`` om dezelfde reden.
SECRET_NAME_PATTERN = r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$"


def _normalize_upstream(value: object, info: ValidationInfo) -> object:
    """De invoerhulp, VOOR de vormregel: wat een afnemer plakt is zelden de upstream.

    Hier en niet in een converter van de editable, zodat het formulier en de API dezelfde
    omzetting krijgen -- de converter roept deze functie aan, hij herhaalt hem niet.

    Wat hij NIET doet is een projectbestand dat er al staat repareren. De hele-bestandspoort
    draait dit model over elk bestand bij elke save en elke herverwerking, met
    ``STORED_CONTEXT_KEY`` erbij; zou de omzetting daar ook draaien, dan kwam een opgeslagen
    ``https://ghcr.io`` door de poort en bleef hij ONgewijzigd in het bestand staan -- want
    valideren schrijft niet terug. ``normalize_prefix`` matcht die vorm nooit, dus de
    registry zou stil niet meer gelden in plaats van luid geweigerd te worden. Een
    opgeslagen waarde hoort dus al canoniek te zijn, en blijft dat.
    """
    if info.context and info.context.get(STORED_CONTEXT_KEY):
        return value
    return normalize_upstream(value) if isinstance(value, str) else value


#: De upstream: eerst omgezet vanuit wat er geplakt is, dan pas aan de vormregel gehouden.
#: Als geannoteerd type en niet als losse ``field_validator``, want ``ModelFieldValidator``
#: bouwt zijn toets uit de ANNOTATIE van het veld -- een validator naast het model zou het
#: formulier een geplakte URL laten afwijzen die de API wel accepteert.
#: De volgorde in ``Annotated`` is niet vrijblijvend: met het patroon VOOR de
#: before-validator staat het patroon ook in het gerenderde JSON-schema, en dat fragment is
#: waar een client de regel leest. Andersom valt het uit het schema weg (pydantic beschrijft
#: dan de INVOER, en die mag van een before-validator alles zijn) terwijl de toets zelf
#: gewoon blijft draaien -- een stille regel is precies wat we hier niet willen.
Upstream = Annotated[str, Field(pattern=UPSTREAM_PATTERN), BeforeValidator(_normalize_upstream)]


class RegistryEntry(BaseModel):
    """Eén private registry van het project."""

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
            "Gebruikersnaam waarmee ZAD bij de registry inlogt, naast 'password'. Optioneel, want "
            "wat hij betekent verschilt per registry: bij GitHub (ghcr.io) doet de waarde er niet toe "
            "zolang het token klopt, bij Docker Hub is het de accountnaam en bij Quay de naam van het "
            "robotaccount. Laat je hem leeg, dan blijft hij leeg in het projectbestand en vult het "
            "platform een neutrale plaatshouder in het pull-secret. Eist de registry een echte naam, "
            "dan mislukt het ophalen van de images. De tokentoets vangt dat alleen in een formulier "
            "met het registryblok (de wizards, de dienstenmodal en de modal van het blok), en alleen "
            "tegen images uit deze registry die in dat formulier al staan; in het componentformulier "
            "en via de API wordt er niet getoetst."
        ),
    )
    password: str | None = Field(
        default=None,
        pattern=AGE_ENCRYPTED_OR_PLAIN_PATTERN,
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

    @model_validator(mode="after")
    def _has_something_to_be_called(self) -> RegistryEntry:
        """Een entry zonder naam EN zonder label is nergens naar te verwijzen.

        ``name`` mag ontbreken omdat het platform hem uit het label afleidt
        (``ImageRegistriesService.generate_missing_values``), maar dan moet dat label er
        wel zijn -- anders valt er niets af te leiden en is de entry onzichtbaar voor
        ``project_registries``, dus voor de hele dienst.
        """
        if not self.name and not self.display_name:
            msg = "Geef een 'name' of een 'display-name'"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _has_exactly_one_way_to_pull(self) -> RegistryEntry:
        """Een entry draagt OF een ``secretName`` OF een token.

        Zonder een van beide komt hij overal doorheen en schrijft de backend stil geen
        pull-secret; de afnemer merkt het pas als de pod niet kan pullen, met een melding die
        niet over een ontbrekend token gaat. De twee vormen mengen kan ook niet: met een
        ``secretName`` slaat de backend gebruikersnaam en token over, dus die zouden er voor
        niets staan. In het model, zodat het formulier en de API dezelfde regel krijgen.

        De gebruikersnaam hoort hier NIET bij, en dat is een herziening (RC-187). Wat hij
        betekent verschilt per registry -- bij ghcr.io doet de waarde er niet toe, bij Docker
        Hub is het de accountnaam, bij Quay de robotnaam -- en dat verschil kan een formulier
        niet weten. Laat de afnemer hem leeg, dan vult ``PULL_USERNAME_PLACEHOLDER`` het gat in
        de dockerconfigjson. Eist de registry wel een echte naam, dan vangt de tokentoets
        (``enforcers.py``) dat alleen onder twee voorwaarden: bij het opslaan van het
        registryblok in het formulier, en als het project al een image onder die upstream
        heeft. Via ``POST .../registries/by-credentials`` of zonder image komt zo'n entry
        ongetoetst door en loopt hij pas bij de pull vast.
        """
        if self.secret_name and (self.username or self.password):
            msg = "Geef een 'secretName' OF een 'username' met 'password', niet allebei"
            raise ValueError(msg)
        if not self.secret_name and not self.password:
            # Noem wat er MIST: bij een entry met een gebruikersnaam vraagt de melding alleen
            # het token, niet ook de naam die er al staat.
            if self.username:
                msg = "Vul een token in bij de gebruikersnaam, anders kunnen we de images niet ophalen"
            else:
                msg = "Vul een token in, anders kunnen we de images niet ophalen"
            raise ValueError(msg)
        return self


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
        min_length=1,
        description=(
            "Naam van de registry uit de projectconfig van deze dienst waar de image van dit component vandaan komt. "
            "Er is geen lege waarde: een component zonder deze dienstvermelding haalt zijn image publiek op. "
            "Gebruik DELETE om die keuze terug te draaien; dat haalt de vermelding weg."
        ),
    )
