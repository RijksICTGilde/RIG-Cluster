"""De handelingencatalogus: de vaste lijst dingen die een aanroeper kan doen.

Fase 1 uit ``features/futures/rechten-plan-van-aanpak.md``, ingevoerd voor het oppervlak
dat een token kan bereiken. Een scope noemt handelingen en geen paden: een nieuwe route
moet een handeling kiezen, dus de lijst veroudert niet zodra er een route bij komt.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import Any, TypeVar


class Handeling(StrEnum):
    """Wat een aanroeper op een project kan doen, als gesloten lijst.

    De waarde staat nergens opgeslagen: een route wijst een handeling aan en de ROL van
    een lid zegt welke handelingen die opent (``ROL_HANDELINGEN`` in
    :mod:`opi.api.principaal`). Hernoemen raakt dus geen uitgegeven token, maar wel elke
    route die de naam noemt.
    """

    PROJECT_LEZEN = "project.lezen"
    PROJECT_LIJSTEN = "project.lijsten"
    PROJECT_AANMAKEN = "project.aanmaken"
    PROJECT_VERWIJDEREN = "project.verwijderen"
    PROJECT_VERVERSEN = "project.verversen"

    DEPLOYMENT_LEZEN = "deployment.lezen"
    DEPLOYMENT_SCHRIJVEN = "deployment.schrijven"
    DEPLOYMENT_VERWIJDEREN = "deployment.verwijderen"
    DEPLOYMENT_VERVERSEN = "deployment.verversen"
    DEPLOYMENT_IMAGE_VERVANGEN = "deployment.image-vervangen"

    COMPONENT_LEZEN = "component.lezen"
    COMPONENT_TOEVOEGEN = "component.toevoegen"
    COMPONENT_WIJZIGEN = "component.wijzigen"
    COMPONENT_VERWIJDEREN = "component.verwijderen"
    COMPONENT_KOPPELEN = "component.koppelen"

    DIENST_LEZEN = "dienst.lezen"
    DIENST_TOEVOEGEN = "dienst.toevoegen"
    DIENST_CONFIGUREREN = "dienst.configureren"
    DIENST_WAARDEN_SCHRIJVEN = "dienst.waarden-schrijven"
    DIENST_ACTIE = "dienst.actie"

    DATABASE_KLONEN = "database.klonen"
    DATABASE_SCHEMA_LEZEN = "database.schema-lezen"
    DATABASE_SCHEMA_BEHEREN = "database.schema-beheren"
    BUCKET_KLONEN = "bucket.klonen"
    KLOON_VALIDEREN = "kloon.valideren"

    REGISTRY_TOEVOEGEN = "registry.toevoegen"
    IMAGE_PUSHEN = "image.pushen"

    BACKUP_LEZEN = "backup.lezen"
    BACKUP_MAKEN = "backup.maken"
    BACKUP_VERWIJDEREN = "backup.verwijderen"
    HERSTEL_LEZEN = "herstel.lezen"
    HERSTEL_UITVOEREN = "herstel.uitvoeren"

    LOGS_LEZEN = "logs.lezen"
    RESOURCES_BIJSTELLEN = "resources.bijstellen"
    RESOURCES_OPSCHONEN = "resources.opschonen"
    SUBDOMEIN_LEZEN = "subdomein.lezen"
    CLUSTER_LEZEN = "cluster.lezen"

    TAAK_LEZEN = "taak.lezen"
    TAAK_ANNULEREN = "taak.annuleren"
    SLAAPSTAND_LEZEN = "slaapstand.lezen"
    SLAAPSTAND_WEKKEN = "slaapstand.wekken"

    # Staat bewust niet in LEESHANDELINGEN: wie de projectsleutel kan uitlezen heeft
    # daarmee alles wat die sleutel opent, dus dit is geen gewone leeshandeling.
    PROJECTSLEUTEL_LEZEN = "projectsleutel.lezen"

    TOKEN_LEZEN = "token.lezen"  # noqa: S105 -- handelingsnaam, geen secret
    TOKEN_BEHEREN = "token.beheren"  # noqa: S105 -- handelingsnaam, geen secret


#: Handelingen die niets wijzigen. Een persoonlijk token van een ``developer`` komt niet
#: verder dan deze lijst, want dat is exact wat ``require_project_edit_access``
#: (``opi/web/project_edit_security.py``) een developer in de interface toestaat.
LEESHANDELINGEN: frozenset[Handeling] = frozenset(
    {
        Handeling.PROJECT_LEZEN,
        Handeling.PROJECT_LIJSTEN,
        Handeling.DEPLOYMENT_LEZEN,
        Handeling.COMPONENT_LEZEN,
        Handeling.DIENST_LEZEN,
        Handeling.DATABASE_SCHEMA_LEZEN,
        Handeling.BACKUP_LEZEN,
        Handeling.HERSTEL_LEZEN,
        Handeling.LOGS_LEZEN,
        Handeling.SUBDOMEIN_LEZEN,
        Handeling.CLUSTER_LEZEN,
        Handeling.TAAK_LEZEN,
        Handeling.SLAAPSTAND_LEZEN,
        Handeling.TOKEN_LEZEN,
    }
)

#: Markering op een route die zegt welke handeling hij vraagt. Een attribuut en geen
#: register, zodat een test (en de opstartcontrole) het uit de routetabel leest in plaats
#: van uit een lijst die naast de code staat; dezelfde vorm als ``REQUIRES_PROJECT_NAME``.
HANDELING_ATTR = "zad_handeling"

F = TypeVar("F", bound=Callable[..., Any])


def handeling(welke: Handeling) -> Callable[[F], F]:
    """Zet de handeling die deze route vraagt op de route zelf.

    Hoort BOVEN de authenticatiedecorator te staan, zodat de markering op het object komt
    dat FastAPI als endpoint registreert. De functie zelf wordt niet gewikkeld.
    """

    def markeer(func: F) -> F:
        setattr(func, HANDELING_ATTR, welke)
        return func

    return markeer


def handeling_van(endpoint: Any) -> Handeling | None:
    """De handeling van een route-endpoint, of None als hij er geen draagt."""
    return getattr(endpoint, HANDELING_ATTR, None)
