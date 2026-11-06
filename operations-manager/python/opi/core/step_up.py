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
zodat het venster na de code ook echt zo lang duurt als ingesteld.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from opi.core.config import settings
from opi.core.leden import ledenlijst_wijzigt
from opi.core.project_environment import (
    Omgeving,
    effective_environment,
    environment_markings,
)

if TYPE_CHECKING:
    from starlette.requests import Request

    from opi.services.user_service import UserService

logger = logging.getLogger(__name__)

#: Hoe lang een step-up een volgende handeling dekt.
STEP_UP_WINDOW = timedelta(minutes=settings.STEP_UP_WINDOW_MINUTES)

#: De sessiesleutel met het moment van de laatste geslaagde step-up.
STEP_UP_SESSION_KEY = "step_up_at"

#: Whether the caller of this request has a fresh step-up. Service-layer checks have no
#: request to read it from. False by default, so a worker or a forgotten entry point never
#: holds platform admin rights.
step_up_fresh: ContextVar[bool] = ContextVar("step_up_fresh", default=False)


class StepUpKind(StrEnum):
    """Waar de handeling over gaat, en daarmee hoe "in productie" gelezen wordt."""

    DEPLOYMENT = "deployment"
    COMPONENT = "component"
    ENVIRONMENT = "environment"


class StepUpAction(StrEnum):
    """Welke handeling een step-up vraagt, zoals een client hem leest.

    De waarde gaat als ``action`` mee in het problem-detail van de API, en is daarmee het
    enige deel van de weigering waarop een client mag sturen. Daarnaast hangen hier de
    twee teksten die bij de handeling horen, zodat een route er maar een ding over zegt.
    """

    PROJECT_DELETE = "project-delete"
    DEPLOYMENT_DELETE = "deployment-delete"
    COMPONENT_DELETE = "component-delete"
    MARKING_SET = "marking-set"
    CLEANUP_START = "cleanup-start"
    RECONCILIATION_START = "reconciliation-start"
    MARKING_REVOKE = "marking-revoke"
    ORPHANS_CONFIRM = "orphans-confirm"
    PROJECTS_RELOAD = "projects-reload"
    PROJECT_KEY_REPLACE = "project-key-replace"
    TOKEN_ISSUE = "token-issue"  # noqa: S105 -- naam van een handeling, geen secret
    TOKEN_ROTATE = "token-rotate"  # noqa: S105 -- naam van een handeling, geen secret
    TOKEN_REVOKE = "token-revoke"  # noqa: S105 -- naam van een handeling, geen secret
    ROLE_CHANGE = "role-change"
    SERVICE_ACCOUNT_ADD = "service-account-add"
    SERVICE_ACCOUNT_REMOVE = "service-account-remove"
    ENV_VARS_MANAGE = "env-vars-manage"
    USER_CREATE = "user-create"
    USER_EDIT = "user-edit"
    USER_SESSIONS_REVOKE = "user-sessions-revoke"
    USER_DELETE = "user-delete"
    USER_OTP_RESET = "user-otp-reset"
    ELEVATE = "elevate"

    @property
    def phrase(self) -> str:
        """De handeling als te-infinitief, voor de dialoog en de log."""
        return _PHRASE[self]

    def reason(self, doelwit: str, *, project: str = "") -> str:
        """Waarom DEZE aanroep een step-up vraagt, voor het problem-detail."""
        return _REASON[self].format(doelwit=doelwit, project=project)


_PHRASE = {
    StepUpAction.PROJECT_DELETE: "dit project te verwijderen",
    StepUpAction.DEPLOYMENT_DELETE: "deze deployment te verwijderen",
    StepUpAction.COMPONENT_DELETE: "dit component te verwijderen",
    StepUpAction.MARKING_SET: "de OTAP-markering te zetten of te verlagen",
    StepUpAction.CLEANUP_START: "de opruiming te starten",
    StepUpAction.RECONCILIATION_START: "een reconciliatie te starten",
    StepUpAction.MARKING_REVOKE: "deze verwijdermarkering in te trekken",
    StepUpAction.ORPHANS_CONFIRM: "deze wezen te bevestigen",
    StepUpAction.PROJECTS_RELOAD: "de projects-repo nu in te lezen",
    StepUpAction.PROJECT_KEY_REPLACE: "de projectsleutel te vervangen",
    StepUpAction.TOKEN_ISSUE: "een token uit te geven",
    StepUpAction.TOKEN_ROTATE: "dit token te roteren",
    StepUpAction.TOKEN_REVOKE: "dit token in te trekken",
    StepUpAction.ROLE_CHANGE: "de leden of hun rollen te wijzigen",
    StepUpAction.SERVICE_ACCOUNT_ADD: "een service account toe te voegen",
    StepUpAction.SERVICE_ACCOUNT_REMOVE: "dit service account te verwijderen",
    StepUpAction.ENV_VARS_MANAGE: "de omgevingsvariabelen van dit component te zien en te wijzigen",
    StepUpAction.USER_CREATE: "een gebruiker aan het portaal toe te voegen",
    StepUpAction.USER_EDIT: "deze gebruiker te wijzigen",
    StepUpAction.USER_SESSIONS_REVOKE: "de sessies van deze gebruiker te beëindigen",
    StepUpAction.USER_DELETE: "deze gebruiker te verwijderen",
    StepUpAction.USER_OTP_RESET: "de 2-factor van deze gebruiker leeg te maken",
    StepUpAction.ELEVATE: "verhoogde rechten te activeren",
}

_REASON = {
    StepUpAction.PROJECT_DELETE: "het project '{doelwit}' heeft een deployment op productie",
    StepUpAction.DEPLOYMENT_DELETE: "de deployment '{doelwit}' staat op productie",
    StepUpAction.COMPONENT_DELETE: "het component '{doelwit}' wordt gebruikt door een deployment op productie",
    StepUpAction.MARKING_SET: (
        "de markering van een deployment in '{doelwit}' verandert, en wie hem kan verlagen omzeilt elke productieregel"
    ),
    StepUpAction.CLEANUP_START: "de opruiming verwijdert resources van '{doelwit}' waarvan de genadeperiode voorbij is",
    StepUpAction.RECONCILIATION_START: "een reconciliatie over het hele platform kan resources verwijderen",
    StepUpAction.MARKING_REVOKE: "de markering '{doelwit}' vervalt, en de geplande verwijdering ermee",
    StepUpAction.ORPHANS_CONFIRM: "bevestigde wezen worden na de genadeperiode van het platform verwijderd",
    StepUpAction.PROJECTS_RELOAD: "het inlezen vervangt de projectstore van dit cluster",
    StepUpAction.PROJECT_KEY_REPLACE: (
        "de sleutel van '{doelwit}' wordt vervangen en elke versleutelde waarde opnieuw versleuteld"
    ),
    StepUpAction.TOKEN_ISSUE: "'{doelwit}' krijgt een bewijsmiddel voor project '{project}'",
    StepUpAction.TOKEN_ROTATE: "het token van '{doelwit}' in project '{project}' krijgt een nieuwe waarde",
    StepUpAction.TOKEN_REVOKE: "een token van '{doelwit}' in project '{project}' werkt daarna niet meer",
    StepUpAction.ROLE_CHANGE: "de rol van '{doelwit}' in project '{project}' bepaalt wat zijn tokens mogen",
    StepUpAction.SERVICE_ACCOUNT_ADD: ("er komt een lid bij in project '{project}', en dat lid kan een token dragen"),
    StepUpAction.SERVICE_ACCOUNT_REMOVE: "'{doelwit}' en al zijn tokens gaan uit project '{project}'",
    StepUpAction.ENV_VARS_MANAGE: (
        "de omgevingsvariabelen van '{doelwit}' in project '{project}' kunnen geheimen bevatten"
    ),
    StepUpAction.USER_CREATE: "een nieuwe gebruiker kan op het portaal inloggen",
    StepUpAction.USER_EDIT: "het adres van een gebruiker bepaalt wie er met zijn account inlogt",
    StepUpAction.USER_SESSIONS_REVOKE: "de gebruiker moet daarna opnieuw inloggen",
    StepUpAction.USER_DELETE: "de gebruiker kan daarna niet meer op het portaal inloggen",
    StepUpAction.USER_OTP_RESET: "wie de 2-factor van een ander leegmaakt zet zijn tweede slot open",
    StepUpAction.ELEVATE: "verhoogde rechten openen platformbeheer en alle projecten voor '{doelwit}'",
}


def _deployments(project_data: dict[str, Any]) -> list[dict[str, Any]]:
    return [d for d in project_data.get("deployments") or [] if isinstance(d, dict)]


def deployment_by_name(project_data: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next((d for d in _deployments(project_data) if d.get("name") == name), None)


def _uses_component(deployment: dict[str, Any], component_name: str) -> bool:
    return any(isinstance(c, dict) and c.get("reference") == component_name for c in deployment.get("components") or [])


def production_marking_change(before: dict[str, Omgeving], after: dict[str, Omgeving]) -> bool:
    """Of deze opslag een deployment NAAR of VAN ``productie`` zet.

    Alleen die overgang is "de markering zetten" (``environment.set``): wie een deployment
    van productie haalt omzeilt elke productieregel met een klik, en wie hem erop zet
    bepaalt waar die regels gelden. Een NIEUWE deployment die meteen op productie komt telt
    mee.

    Een overgang tussen de andere stages (ontwikkeling, test, acceptatie) valt er bewust
    niet onder: dat is een deployment schrijven, en de regel daarvan weegt hem al. Zo kan
    een developer of een pijplijn met ``developer-ci`` een deployment van test naar
    acceptatie zetten. Een deployment die VERDWIJNT valt er ook niet onder: het
    verwijderen heeft zijn eigen regel, met zijn eigen tekst.
    """
    for name, after_value in after.items():
        before_value = before.get(name)
        if before_value is not after_value and Omgeving.PRODUCTIE in (before_value, after_value):
            return True
    return False


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
            return change is not None and production_marking_change(*change)


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
    return f"Om {handeling} is een verse code uit je 2-factor nodig."


def record_step_up(request: Request) -> None:
    """Leg vast dat deze gebruiker zojuist een geldige code gaf."""
    request.session[STEP_UP_SESSION_KEY] = datetime.now(UTC).isoformat()


def step_up_until(request: Request) -> datetime | None:
    """Het moment waarop de verse step-up in de sessie verloopt, of None als er geen is.

    Geen sessie is geen step-up: op een ``/api/``-pad draait de sessiemiddleware niet, en
    Starlette werpt daar een AssertionError op ``request.session``. Sinds RC-392 leest ook
    het rechtensysteem deze functie, en dat doet het op beide paden.
    """
    try:
        sessie = request.session
    except AssertionError, AttributeError:
        return None
    return session_step_up_until(sessie)


def session_step_up_until(sessie: Any) -> datetime | None:
    """:func:`step_up_until` for a caller holding only the session, such as a WebSocket."""
    stamp = sessie.get(STEP_UP_SESSION_KEY)
    if not isinstance(stamp, str):
        return None
    try:
        until = datetime.fromisoformat(stamp) + STEP_UP_WINDOW
    except ValueError:
        logger.warning("Onleesbare step-up-stempel in de sessie, genegeerd")
        return None
    return until if datetime.now(UTC) < until else None


def has_fresh_step_up(request: Request) -> bool:
    """Of er een step-up van minder dan :data:`STEP_UP_WINDOW` geleden in de sessie staat."""
    return step_up_until(request) is not None


def clear_step_up(request: Request) -> None:
    """End the step-up in this session, before its window runs out."""
    request.session.pop(STEP_UP_SESSION_KEY, None)


def is_elevated_admin(users: UserService, email: str) -> bool:
    """Whether this caller may use platform admin rights right now.

    A platform admin without a fresh step-up is an ordinary user who sees their own
    projects. ``UserService.is_platform_admin`` only says who MAY elevate.
    """
    return step_up_fresh.get() and users.is_platform_admin(email)


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
        return StepUpAction.MARKING_SET.phrase
    if ledenlijst_wijzigt(before, after):
        return StepUpAction.ROLE_CHANGE.phrase
    return None
