"""Een handeling in productie vraagt een verse step-up (RC-332).

Sinds RC-377 vraagt ook elke wijzigende beheerroute er een, ongeacht de omgeving: een
handeling van platformbeheer raakt het hele cluster. Sinds RC-387 geldt dat ook voor een
token of een lidmaatschap: wie een bewijsmiddel krijgt of houdt kan blijvend bij een
project, ook buiten productie. Sinds RC-388 geldt het voor de omgevingsvariabelen van een
component: die kunnen geheimen bevatten en staan daarom niet meer op een pagina.

Hier staat WANNEER een handeling er een vraagt en of er een verse is. Hoe het portaal
erom vraagt staat in ``opi/web/router_step_up.py``; hoe een handeling hem oplegt in
:func:`opi.core.auth_decorators.requires_step_up`.

Het venster schuift niet mee: een handeling LEEST de stempel en zet hem nooit opnieuw,
zodat vijf minuten na de code ook echt vijf minuten zijn (besluit 8.5).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from opi.core.project_environment import (
    Omgeving,
    effective_environment,
    environment_markings,
    highest_environment,
    is_production_downgrade,
)

if TYPE_CHECKING:
    from starlette.requests import Request

logger = logging.getLogger(__name__)

#: Hoe lang een step-up een volgende handeling dekt.
STEP_UP_WINDOW = timedelta(minutes=5)

#: De sessiesleutel met het moment van de laatste geslaagde step-up.
STEP_UP_SESSION_KEY = "step_up_at"


class StepUpKind(StrEnum):
    """Waar de handeling over gaat, en daarmee hoe "in productie" gelezen wordt."""

    PROJECT = "project"
    DEPLOYMENT = "deployment"
    COMPONENT = "component"
    ENVIRONMENT = "environment"


class StepUpActie(StrEnum):
    """Welke handeling een step-up vraagt, zoals een client hem leest.

    De waarde gaat als ``action`` mee in het problem-detail van de API, en is daarmee het
    enige deel van de weigering waarop een client mag sturen. Daarnaast hangen hier de
    twee teksten die bij de handeling horen, zodat een route er maar een ding over zegt.
    """

    PROJECT_VERWIJDEREN = "project-verwijderen"
    DEPLOYMENT_VERWIJDEREN = "deployment-verwijderen"
    COMPONENT_VERWIJDEREN = "component-verwijderen"
    MARKERING_VERLAGEN = "markering-verlagen"
    OPRUIMING_STARTEN = "opruiming-starten"
    RECONCILIATIE_STARTEN = "reconciliatie-starten"
    MARKERING_INTREKKEN = "markering-intrekken"
    WEZEN_BEVESTIGEN = "wezen-bevestigen"
    PROJECTEN_HERINLEZEN = "projecten-herinlezen"
    PROJECTSLEUTEL_VERVANGEN = "projectsleutel-vervangen"
    TOKEN_UITGEVEN = "token-uitgeven"  # noqa: S105 -- naam van een handeling, geen secret
    TOKEN_ROTEREN = "token-roteren"  # noqa: S105 -- naam van een handeling, geen secret
    TOKEN_INTREKKEN = "token-intrekken"  # noqa: S105 -- naam van een handeling, geen secret
    ROL_WIJZIGEN = "rol-wijzigen"
    SERVICE_ACCOUNT_TOEVOEGEN = "service-account-toevoegen"
    SERVICE_ACCOUNT_VERWIJDEREN = "service-account-verwijderen"
    ENV_VARS_BEHEREN = "env-vars-beheren"

    @property
    def handeling(self) -> str:
        """De handeling als te-infinitief, voor de dialoog en de log."""
        return _HANDELING[self]

    def reden(self, doelwit: str, *, project: str = "") -> str:
        """Waarom DEZE aanroep een step-up vraagt, voor het problem-detail."""
        return _REDEN[self].format(doelwit=doelwit, project=project)


_HANDELING = {
    StepUpActie.PROJECT_VERWIJDEREN: "dit project te verwijderen",
    StepUpActie.DEPLOYMENT_VERWIJDEREN: "deze deployment te verwijderen",
    StepUpActie.COMPONENT_VERWIJDEREN: "dit component te verwijderen",
    StepUpActie.MARKERING_VERLAGEN: "de markering productie te verlagen",
    StepUpActie.OPRUIMING_STARTEN: "de opruiming te starten",
    StepUpActie.RECONCILIATIE_STARTEN: "een reconciliatie te starten",
    StepUpActie.MARKERING_INTREKKEN: "deze verwijdermarkering in te trekken",
    StepUpActie.WEZEN_BEVESTIGEN: "deze wezen te bevestigen",
    StepUpActie.PROJECTEN_HERINLEZEN: "de projects-repo nu in te lezen",
    StepUpActie.PROJECTSLEUTEL_VERVANGEN: "de projectsleutel te vervangen",
    StepUpActie.TOKEN_UITGEVEN: "een token uit te geven",
    StepUpActie.TOKEN_ROTEREN: "dit token te roteren",
    StepUpActie.TOKEN_INTREKKEN: "dit token in te trekken",
    StepUpActie.ROL_WIJZIGEN: "de rol van dit lid te wijzigen",
    StepUpActie.SERVICE_ACCOUNT_TOEVOEGEN: "een service account toe te voegen",
    StepUpActie.SERVICE_ACCOUNT_VERWIJDEREN: "dit service account te verwijderen",
    StepUpActie.ENV_VARS_BEHEREN: "de omgevingsvariabelen van dit component te zien en te wijzigen",
}

_REDEN = {
    StepUpActie.PROJECT_VERWIJDEREN: "het project '{doelwit}' heeft een deployment op productie",
    StepUpActie.DEPLOYMENT_VERWIJDEREN: "de deployment '{doelwit}' staat op productie",
    StepUpActie.COMPONENT_VERWIJDEREN: "het component '{doelwit}' wordt gebruikt door een deployment op productie",
    StepUpActie.MARKERING_VERLAGEN: "de wijziging haalt een deployment van '{doelwit}' van productie af",
    StepUpActie.OPRUIMING_STARTEN: "de opruiming verwijdert resources van '{doelwit}' waarvan de genadeperiode voorbij is",
    StepUpActie.RECONCILIATIE_STARTEN: "een reconciliatie over het hele platform kan resources verwijderen",
    StepUpActie.MARKERING_INTREKKEN: "de markering '{doelwit}' vervalt, en de geplande verwijdering ermee",
    StepUpActie.WEZEN_BEVESTIGEN: "bevestigde wezen worden na de genadeperiode van het platform verwijderd",
    StepUpActie.PROJECTEN_HERINLEZEN: "het inlezen vervangt de projectstore van dit cluster",
    StepUpActie.PROJECTSLEUTEL_VERVANGEN: (
        "de sleutel van '{doelwit}' wordt vervangen en elke versleutelde waarde opnieuw versleuteld"
    ),
    StepUpActie.TOKEN_UITGEVEN: "'{doelwit}' krijgt een bewijsmiddel voor project '{project}'",
    StepUpActie.TOKEN_ROTEREN: "het token van '{doelwit}' in project '{project}' krijgt een nieuwe waarde",
    StepUpActie.TOKEN_INTREKKEN: "een token van '{doelwit}' in project '{project}' werkt daarna niet meer",
    StepUpActie.ROL_WIJZIGEN: "de rol van '{doelwit}' in project '{project}' bepaalt wat zijn tokens mogen",
    StepUpActie.SERVICE_ACCOUNT_TOEVOEGEN: (
        "er komt een lid bij in project '{project}', en dat lid kan een token dragen"
    ),
    StepUpActie.SERVICE_ACCOUNT_VERWIJDEREN: "'{doelwit}' en al zijn tokens gaan uit project '{project}'",
    StepUpActie.ENV_VARS_BEHEREN: (
        "de omgevingsvariabelen van '{doelwit}' in project '{project}' kunnen geheimen bevatten"
    ),
}


def _deployments(project_data: dict[str, Any]) -> list[dict[str, Any]]:
    return [d for d in project_data.get("deployments") or [] if isinstance(d, dict)]


def deployment_by_name(project_data: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next((d for d in _deployments(project_data) if d.get("name") == name), None)


def _uses_component(deployment: dict[str, Any], component_name: str) -> bool:
    return any(isinstance(c, dict) and c.get("reference") == component_name for c in deployment.get("components") or [])


def _any_downgrade(before: dict[str, Omgeving], after: dict[str, Omgeving]) -> bool:
    """Of een deployment productie verlaat: door een lagere markering of door te verdwijnen.

    Een project heeft sinds RC-330 zelf geen markering, dus er is niets anders te
    vergelijken dan de deployments.
    """
    return any(
        is_production_downgrade(before_value, after.get(name, Omgeving.ONTWIKKELING))
        for name, before_value in before.items()
    )


def requires_production_step_up(
    kind: StepUpKind,
    project_data: dict[str, Any],
    target: str | None = None,
    change: tuple[dict[str, Omgeving], dict[str, Omgeving]] | None = None,
) -> bool:
    """Of deze handeling een verse step-up vraagt.

    ``target`` is de naam van de deployment of het component; ``change`` zijn de
    markeringen van voor en na, en alleen bij :attr:`StepUpKind.ENVIRONMENT` gevuld.
    """
    match kind:
        case StepUpKind.PROJECT:
            return highest_environment(project_data) is Omgeving.PRODUCTIE
        case StepUpKind.DEPLOYMENT:
            deployment = deployment_by_name(project_data, target or "")
            return deployment is not None and effective_environment(deployment) is Omgeving.PRODUCTIE
        case StepUpKind.COMPONENT:
            return any(
                effective_environment(d) is Omgeving.PRODUCTIE
                for d in _deployments(project_data)
                if _uses_component(d, target or "")
            )
        case StepUpKind.ENVIRONMENT:
            return change is not None and _any_downgrade(*change)


def log_step_up_refusal(
    handeling: str,
    *,
    project_name: str,
    actor: str,
    doelwit: str | None = None,
) -> str:
    """Log een geweigerde handeling en geef de tekst die de gebruiker te zien krijgt.

    Drie plekken weigeren een handeling (de decorator, de modal-opslag en de wizard) met
    verschillende antwoordvormen, maar met dezelfde regel en dezelfde tekst.
    """
    logger.warning(
        "Geweigerd, geen verse step-up om %s: doel '%s', project '%s', door %s",
        handeling,
        doelwit or project_name,
        project_name,
        actor,
    )
    return f"Om {handeling} is een verse code uit je tweede factor nodig."


def record_step_up(request: Request) -> None:
    """Leg vast dat deze gebruiker zojuist een geldige code gaf."""
    request.session[STEP_UP_SESSION_KEY] = datetime.now(UTC).isoformat()


def has_fresh_step_up(request: Request) -> bool:
    """Of er een step-up van minder dan :data:`STEP_UP_WINDOW` geleden in de sessie staat."""
    stamp = request.session.get(STEP_UP_SESSION_KEY)
    if not isinstance(stamp, str):
        return False
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        logger.warning("Onleesbare step-up-stempel in de sessie, genegeerd")
        return False
    return datetime.now(UTC) - moment < STEP_UP_WINDOW


def _names(items: Any) -> set[str]:
    return {str(i["name"]) for i in items or [] if isinstance(i, dict) and i.get("name")}


def save_requires_step_up(before: dict[str, Any], after: dict[str, Any]) -> str | None:
    """Hoe deze opslag productie raakt, als te-infinitief voor de dialoog, of None.

    De portaalwegen die NIET door een verwijderroute lopen komen hier samen: de
    modal-wizard haalt een deployment of een component uit de lijst, en dezelfde opslag
    kan de markering verlagen.
    """
    for naam in sorted(_names(before.get("deployments")) - _names(after.get("deployments"))):
        if requires_production_step_up(StepUpKind.DEPLOYMENT, before, naam):
            return f"de deployment '{naam}' te verwijderen"
    for naam in sorted(_names(before.get("components")) - _names(after.get("components"))):
        if requires_production_step_up(StepUpKind.COMPONENT, before, naam):
            return f"het component '{naam}' te verwijderen"
    if requires_production_step_up(
        StepUpKind.ENVIRONMENT, before, change=(environment_markings(before), environment_markings(after))
    ):
        return "de markering productie te verlagen"
    return None
