"""De ArgoCD-stand van ALLE deployments van een project, in EEN bevraging.

Waarom dit bestaat: de deploymenttabel op het tabblad Overzicht toont per rij of die
deployment gezond en gesynchroniseerd is. Dat is precies waarvoor je naar dat blok kijkt -
een statusoverzicht zonder status is geen overzicht.

De weg die er al lag schaalt daar niet mee. De statuskaart in het deploymentpaneel haalt
zijn stand per deployment op (``/argocd-status/<naam>``, met ``hx-trigger="intersect
once"``), en dat is prima voor EEN kaart die je opent. Twintig rijen zouden twintig
ArgoCD-bevragingen worden bij het openen van de pagina.

Hier wordt daarom de LIJST opgehaald: een enkele ``GET /api/v1/applications``, waar de
applicaties van dit project uit gepikt worden. Een rij erbij kost dus niets. De
applicaties van andere projecten komen wel over de lijn en worden hier weggegooid; dat is
de prijs van een bevraging in plaats van N, en die is het waard.

Deze module levert alleen de SAMENVATTING per deployment (gezondheid, sync, laatste sync,
revisie). De volledige diagnose - de foutenlijst, de gebeurtenissen uit de namespace -
blijft waar hij stond, in de statuskaart van het geopende paneel: die vraagt meer van
ArgoCD en van kubectl, en dat hoort niet bij het opbouwen van een tabel.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from opi.connectors.argo import create_argo_connector
from opi.utils.naming import generate_argocd_application_name

logger = logging.getLogger(__name__)

#: Hoe lang een opgehaalde stand hergebruikt wordt, in seconden.
#:
#: VIJFTIEN SECONDEN, en de reden staat erbij omdat de keuze een afweging is: een
#: verouderde "Healthy" is erger dan geen status. Wie op deze pagina kijkt, kijkt of het
#: goed gaat; een groen vinkje dat een minuut oud is beantwoordt die vraag verkeerd.
#:
#: Wat deze vervaltijd dan wel opvangt is de STOOT: de pagina openen, F5, het tabblad
#: wisselen en weer terug - dat zijn meerdere renders binnen een paar seconden, en die
#: hoeven ArgoCD niet elk apart te bevragen. Alles daarbuiten haalt gewoon opnieuw op.
CACHE_TTL_SECONDS = 15

#: Hoe lang de pagina op ArgoCD wacht voor hij zonder statuskolom verder gaat.
#:
#: Deze bevraging staat op het renderpad: hij houdt de projectpagina op. Vijf seconden is
#: ruim voor een enkele lijst-aanroep en kort genoeg om geen pagina op te offeren aan een
#: ArgoCD die niet antwoordt. De connector zelf wacht tot dertig seconden, en dat is voor
#: een pagina te lang.
BEVRAGING_TIMEOUT_SECONDS = 5

#: De opgehaalde stand per project: naam -> (tijdstip, stand per deployment).
#:
#: In het geheugen van dit proces, en dus per OPI-instantie. Dat is genoeg: het gaat om
#: het opvangen van een stoot verzoeken van dezelfde gebruiker, niet om een gedeelde
#: waarheid tussen instanties.
_cache: dict[str, tuple[float, dict[str, dict[str, Any]]]] = {}


def clear_cache() -> None:
    """Gooi de bewaarde standen weg. Voor tests, en voor wie de stoot niet wil."""
    _cache.clear()


def _summarize(application: dict[str, Any]) -> dict[str, Any]:
    """De velden die een tabelrij nodig heeft, uit een ArgoCD-applicatie.

    Dezelfde lezing als ``_fetch_argocd_deployment_status`` in de webrouter, zodat de rij
    en de kaart in het paneel niet twee verschillende woorden voor dezelfde stand
    gebruiken.
    """
    status = application.get("status", {}) or {}
    sync = status.get("sync", {}) or {}
    operation_state = status.get("operationState", {}) or {}

    last_sync = operation_state.get("finishedAt")
    if not last_sync and sync.get("status") == "Synced":
        last_sync = status.get("reconciledAt")

    revision = sync.get("revision") or ""
    return {
        "available": True,
        "health": (status.get("health", {}) or {}).get("status", "Unknown"),
        "sync": sync.get("status", "Unknown"),
        "revision": revision[:7] if revision else None,
        "last_sync": last_sync,
    }


async def get_project_argocd_statuses(project_name: str, deployment_names: list[str]) -> dict[str, dict[str, Any]]:
    """De ArgoCD-stand van elke genoemde deployment, uit EEN bevraging.

    Args:
        project_name: het project waar de deployments bij horen.
        deployment_names: de deployments waarvan de stand gevraagd wordt.

    Returns:
        Per deploymentnaam een dict met ``available``, ``health``, ``sync``, ``revision``
        en ``last_sync``. Een deployment die ArgoCD niet kent staat er WEL in, met
        ``available: False`` - dan kan de tabel het verschil tonen tussen "nog niets
        uitgerold" en "we weten het niet".

        Is ArgoCD niet verbonden, dan komt er een lege dict terug. De pagina weet dat al
        (``argocd_available``) en zegt het daar in gewone taal; hier een verzonnen
        "Unknown" per rij van maken zou dat overschreeuwen.
    """
    if not deployment_names:
        return {}

    now = time.monotonic()
    bewaard = _cache.get(project_name)
    if bewaard is not None and now - bewaard[0] < CACHE_TTL_SECONDS:
        gevraagd = set(deployment_names)
        # Alleen hergebruiken als de bewaarde stand ELKE gevraagde deployment kent: is er
        # er intussen een bijgekomen, dan is de bewaarde stand niet het antwoord op deze
        # vraag en wordt er gewoon opnieuw opgehaald.
        if gevraagd <= set(bewaard[1]):
            return {naam: bewaard[1][naam] for naam in deployment_names}

    argo = create_argo_connector()
    # Dezelfde toets als de pagina zelf doet voor ``argocd_available``, zodat die twee
    # niet uiteen kunnen lopen: de pagina die zegt dat ArgoCD verbonden is hoort ook
    # standen te krijgen, en andersom. Inloggen gebeurt hier niet met de hand - dat doet
    # de connector zelf bij het verzoek hieronder.
    if argo.auth_token is None:
        logger.debug("ArgoCD niet verbonden; geen gebundelde status voor %s", project_name)
        return {}

    # Deze bevraging staat op het RENDERPAD van de projectpagina: hij houdt de pagina op
    # tot hij antwoord heeft. Dat is de prijs van de status meteen in de tabel hebben, en
    # daarom staat er een grens omheen. Een trage of hangende ArgoCD kost een halve
    # seconde extra en levert een tabel zonder statuskolom op - hij mag de pagina niet
    # meeslepen. De connector zelf wacht tot 30 seconden, en dat is voor een pagina te
    # lang.
    try:
        async with asyncio.timeout(BEVRAGING_TIMEOUT_SECONDS):
            applications = await argo.list_applications()
    except TimeoutError:
        logger.warning(
            "ArgoCD antwoordde niet binnen %ss; de deploymenttabel van %s krijgt geen statuskolom",
            BEVRAGING_TIMEOUT_SECONDS,
            project_name,
        )
        return {}

    per_naam = {
        naam: application
        for application in applications
        if (naam := (application.get("metadata", {}) or {}).get("name"))
    }

    standen: dict[str, dict[str, Any]] = {}
    for deployment_name in deployment_names:
        app_name = generate_argocd_application_name(project_name, deployment_name)
        application = per_naam.get(app_name)
        standen[deployment_name] = (
            _summarize(application)
            if application is not None
            else {"available": False, "health": "Unknown", "sync": "Unknown", "revision": None, "last_sync": None}
        )

    _cache[project_name] = (now, standen)
    # De vervallen standen van andere projecten meteen weg, zodat deze dict niet
    # meegroeit met het aantal projecten dat ooit bekeken is.
    for naam in [naam for naam, (tijdstip, _) in _cache.items() if now - tijdstip >= CACHE_TTL_SECONDS]:
        del _cache[naam]

    return standen
