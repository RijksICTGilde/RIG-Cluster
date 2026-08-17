"""Een lijst in een serviceconfig die de API als ÉÉN object toont.

TIJDELIJKE CONSTRUCTIE -- lees dit voordat je erop voortbouwt.
=============================================================

De aanleiding is ``invite.active``. Een project heeft er vandaag altijd precies één, en
het formulier pint dat ook af (``INVITE_ACTIVE_EDITABLE``: min == max == 1, geen
toevoeg-/verwijderknop). Een lijst van één maakt elke client lelijker dan nodig: wie via
de CLI of een agent een uitnodiging zet, schrijft ``{"active": [{...}]}`` met een
haakjespaar dat nooit een tweede element krijgt, en moet bij het lezen op index 0 duiken
voor het enige dat er is.

Dus liegt de API: hij toont het enkelvoud. De OPSLAG blijft onaangeroerd een lijst -- in
het projectbestand, in het configmodel, in ``project_v2.json`` en in het vastgelegde
schemafragment. Er is geen migratie, geen tweede vorm en geen schemawijziging, precies
omdat we NIET weten of er nog invite-soorten bij komen. Komt dat er, dan gaat de naam uit
``Service.api_singular_lists`` en is het lijstoppervlak terug; er verandert niets aan wat
er op schijf staat.

De grens die deze gevel eerlijk houdt
-------------------------------------

Een gevel mag alleen bestaan zolang hij waar is. Staan er twee of meer entries in het
bestand -- met de hand toegevoegd, of via de PATCH-route -- dan mag deze laag NIET de
eerste tonen en de rest overschrijven. Dat is het verschil tussen een nette gevel en stil
dataverlies, en bij een invite is dat verlies onherstelbaar: het projectbestand is de enige
plek waar die uitnodiging staat, en een overschreven uitnodigingslink is per direct
ongeldig voor iedereen die hem al had.

``overflowing_list`` is die detectie. De aanroepers (de leesroute en de PUT in
``opi/api/v2/router.py``) weigeren erop met een 409 die vertelt wat er aan de hand is en
welke weg wel werkt: de PATCH op de lijst, die entries één voor één aanpakt.

De PATCH mag er daarom zelf geen tweede bij zetten
--------------------------------------------------

Dat kon aanvankelijk wel, en dan was de gevel binnen twee geldige aanroepen onwaar: twee
keer een invite toevoegen is allebei een prima verzoek, en pas de VOLGENDE lezer kreeg de
409 -- met een uitweg die vroeg wat die read hem zou vertellen, namelijk welke sleutel hij
moet weghalen (gemeld door de zad-cli, punt 13). De grens ligt nu bij de handeling die de
tweede entry maakt: ``ServiceAdapter.patch_service_config_list`` weigert een TOEVOEGING die
de lijst boven één brengt.

Verwijderen blijft altijd toegestaan, en dat is de reden dat een bestand dat er al meer
heeft niet vastloopt: haal er een weg en de gevel klopt weer. Vervangen mag ook, want dat
verandert het aantal niet.

De DELETE valt hier bewust buiten. Die zegt "wis dit configblok" en doet dat ook, met of
zonder gevel; hij houdt zich niet voor dat er één entry is. Hem óók weigeren zou een
project met meerdere invites zonder enige weg naar een schone lei zetten.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, create_model

from opi.services.config_lists import list_item_type

#: Markering in ``/openapi.json`` op een veld dat hier enkelvoudig staat en in het
#: projectbestand een lijst is. Naast ``x-platform-managed`` en met dezelfde bedoeling: een
#: client ziet de afwijking in het document in plaats van hem te ontdekken.
SINGULAR_MARKER = "x-api-singular"

#: Gebouwde gevelmodellen, zodat twee routes van dezelfde dienst één schema in het
#: OpenAPI-document delen in plaats van twee gelijknamige.
_MODELS: dict[tuple[type[BaseModel], frozenset[str]], type[BaseModel]] = {}


def _fields_by_key(config_model: type[BaseModel]) -> dict[str, tuple[str, Any]]:
    """De velden van *config_model* op hun sleutel op schijf (de alias, anders de naam)."""
    return {(field.alias or name): (name, field) for name, field in config_model.model_fields.items()}


def singular_config_model(config_model: type[BaseModel], names: frozenset[str]) -> type[BaseModel]:
    """*config_model*, maar met elk veld uit *names* als één entry in plaats van een lijst.

    Een subklasse van het echte model, dus alle andere velden, hun aliassen en de
    ``model_config`` (``extra="forbid"``, ``populate_by_name``) zijn per definitie
    dezelfde. Een naam die geen lijstveld van het model is, is een fout en geen stille
    no-op: hetzelfde argument als bij ``ITEM_KEYS`` -- een typefout zou anders ongemerkt
    het lijstoppervlak laten staan.
    """
    key = (config_model, names)
    cached = _MODELS.get(key)
    if cached is not None:
        return cached

    by_key = _fields_by_key(config_model)
    overrides: dict[str, Any] = {}
    for on_disk in sorted(names):
        found = by_key.get(on_disk)
        if found is None:
            raise ValueError(
                f"{config_model.__name__} has no field '{on_disk}', so it cannot be presented as a single entry"
            )
        name, field = found
        item = list_item_type(field.annotation)
        if item is None:
            raise ValueError(f"{config_model.__name__}.{on_disk} is not a list, so there is no list character to hide")
        description = " ".join(
            part
            for part in (
                field.description,
                f"Exactly one is accepted here. The project file stores '{on_disk}' as a list; this API presents "
                "that list as a single entry for as long as there is only ever one of them. A file that already "
                "holds more than one is refused rather than overwritten -- use the PATCH route on this list to "
                "manage entries one at a time.",
            )
            if part
        )
        overrides[name] = (
            item | None,
            Field(default=None, alias=field.alias, description=description, json_schema_extra={SINGULAR_MARKER: True}),
        )

    built = create_model(f"{config_model.__name__}Singular", __base__=config_model, **overrides)
    _MODELS[key] = built
    return built


def overflowing_list(config: Any, names: frozenset[str]) -> tuple[str, int] | None:
    """De eerste lijst uit *names* in *config* met meer dan één entry, en hoeveel het er zijn.

    ``None`` als de gevel klopt: nul of één entry per lijst, of niets van deze dienst.
    """
    if not isinstance(config, dict):
        return None
    for on_disk in sorted(names):
        stored = config.get(on_disk)
        if isinstance(stored, list) and len(stored) > 1:
            return on_disk, len(stored)
    return None


def to_singular(config: Any, names: frozenset[str]) -> Any:
    """Opslagvorm -> API-vorm: de lijst wordt zijn enige entry, of ``None`` als hij leeg is.

    Roep dit pas aan als ``overflowing_list`` niets vond; met meer dan één entry zou dit
    de rest verzwijgen, en dat is precies wat hier niet mag gebeuren.
    """
    if not isinstance(config, dict):
        return config
    result = dict(config)
    for on_disk in names:
        stored = result.get(on_disk)
        if isinstance(stored, list):
            result[on_disk] = stored[0] if stored else None
    return result


def to_stored(config: Any, names: frozenset[str]) -> Any:
    """API-vorm -> opslagvorm: de ene entry wordt een lijst van één.

    Een veld dat de afzender niet noemde staat er (``exclude_unset``) niet in en blijft
    er niet in: wat hij niet stuurde, schrijft hij niet. Een expliciete ``null`` betekent
    "geen entry" en wordt de lege lijst, want de opslag kent geen enkelvoud.
    """
    if not isinstance(config, dict):
        return config
    result = dict(config)
    for on_disk in names:
        if on_disk not in result:
            continue
        entry = result[on_disk]
        result[on_disk] = [] if entry is None else [entry]
    return result
