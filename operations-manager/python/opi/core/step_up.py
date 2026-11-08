"""An action in production asks for a fresh step-up (RC-332).

So does every mutating administration route, whatever the environment: an action of
platform administration touches the whole cluster. So does a token or a membership:
whoever gets or keeps a credential has lasting access to a project, outside production
too. And so do the environment variables of a component, which can hold secrets.

This is WHEN an action asks for one, and whether there is a fresh one. How the portal asks
for it is in ``opi/web/router_step_up.py``; how an action imposes it is in
:func:`opi.core.auth_decorators.requires_step_up`.

The window does not slide along: an action READS the stamp and never sets it again, so the
window after the code really lasts as long as configured.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from opi.core.config import settings
from opi.core.members import member_list_changed
from opi.core.project_environment import (
    Omgeving,
    effective_environment,
    environment_markings,
)

if TYPE_CHECKING:
    from starlette.requests import Request

    from opi.services.user_service import UserService

logger = logging.getLogger(__name__)

#: How long a step-up covers a following action.
STEP_UP_WINDOW = timedelta(minutes=settings.STEP_UP_WINDOW_MINUTES)

#: The session key holding the moment of the last successful step-up.
STEP_UP_SESSION_KEY = "step_up_at"

#: Whether caller has a fresh step-up. Service-layer has no request. Default False
#: prevents accidental admin rights for workers or forgotten entry points.
step_up_fresh: ContextVar[bool] = ContextVar("step_up_fresh", default=False)


class StepUpKind(StrEnum):
    """What the action is about, and with that how "in production" is read."""

    DEPLOYMENT = "deployment"
    COMPONENT = "component"
    ENVIRONMENT = "environment"


class StepUpAction(StrEnum):
    """Which action asks for a step-up, as a client reads it.

    The value travels as ``action`` in the problem detail of the API, and is with that the
    only part of the refusal a client may steer on. Next to it hang the two texts that
    belong to the action, so a route says only one thing about it.
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
    TOKEN_ISSUE = "token-issue"  # noqa: S105  # the name of an action, not a secret
    TOKEN_ROTATE = "token-rotate"  # noqa: S105  # the name of an action, not a secret
    TOKEN_REVOKE = "token-revoke"  # noqa: S105  # the name of an action, not a secret
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
        """The action as a Dutch "te"-infinitive, for the dialog and the log."""
        return _PHRASE[self]

    def reason(self, target: str, *, project: str = "") -> str:
        """Why THIS call asks for a step-up, for the problem detail."""
        return _REASON[self].format(doelwit=target, project=project)


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
    """Whether this save puts a deployment ON or OFF ``productie``.

    Only that transition is "setting the marking" (``environment.set``): whoever takes a
    deployment off production sidesteps every production rule with one click, and whoever
    puts it on decides where those rules hold. A NEW deployment that lands on production
    straight away counts.

    A transition between the other stages (ontwikkeling, test, acceptatie) deliberately
    does not: that is writing a deployment, and the rule for that already weighs it. So a
    developer or a pipeline with ``developer-ci`` can move a deployment from test to
    acceptatie. A deployment that DISAPPEARS does not fall under it either: deleting has a
    rule of its own, with a text of its own.
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
    """Whether this action asks for a fresh step-up.

    ``target`` is the name of the deployment or the component; ``change`` is the markings
    before and after, and is filled only for :attr:`StepUpKind.ENVIRONMENT`.
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
    action_phrase: str,
    *,
    project_name: str,
    actor: str,
    target: str | None = None,
) -> str:
    """Log a refused action and return the text the user gets to see.

    Three places refuse an action (the decorator, the modal save and the wizard) with
    different answer shapes, but with the same rule and the same text.
    """
    logger.warning(
        "Geweigerd, geen verse step-up om %s: doel '%s', project '%s', door %s",
        action_phrase,
        target or project_name,
        project_name,
        actor,
    )
    return f"Om {action_phrase} is een verse code uit je 2-factor nodig."


def record_step_up(request: Request) -> None:
    """Record that this user just gave a valid code."""
    request.session[STEP_UP_SESSION_KEY] = datetime.now(UTC).isoformat()


def step_up_until(request: Request) -> datetime | None:
    """The moment the fresh step-up in the session expires, or None when there is none.

    No session is no step-up: on an ``/api/`` path the session middleware does not run, and
    Starlette raises an AssertionError on ``request.session`` there. The authorization
    system reads this function as well, and it does so on both paths.
    """
    try:
        session = request.session
    except AssertionError, AttributeError:
        return None
    return session_step_up_until(session)


def session_step_up_until(session: Any) -> datetime | None:
    """:func:`step_up_until` for a caller holding only the session, such as a WebSocket."""
    stamp = session.get(STEP_UP_SESSION_KEY)
    if not isinstance(stamp, str):
        return None
    try:
        until = datetime.fromisoformat(stamp) + STEP_UP_WINDOW
    except ValueError:
        logger.warning("Onleesbare step-up-stempel in de sessie, genegeerd")
        return None
    return until if datetime.now(UTC) < until else None


def has_fresh_step_up(request: Request) -> bool:
    """Whether the session holds a step-up from less than :data:`STEP_UP_WINDOW` ago."""
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
    """How this save touches production, as a "te"-infinitive for the dialog, or None.

    The portal routes that do NOT run through a delete route come together here: the modal
    wizard takes a deployment or a component out of the list, and the same save can lower
    the marking.
    """
    for name in sorted(_names(before.get("deployments")) - _names(after.get("deployments"))):
        if requires_production_step_up(StepUpKind.DEPLOYMENT, before, name):
            return f"de deployment '{name}' te verwijderen"
    for name in sorted(_names(before.get("components")) - _names(after.get("components"))):
        if requires_production_step_up(StepUpKind.COMPONENT, before, name):
            return f"het component '{name}' te verwijderen"
    if requires_production_step_up(
        StepUpKind.ENVIRONMENT, before, change=(environment_markings(before), environment_markings(after))
    ):
        return StepUpAction.MARKING_SET.phrase
    if member_list_changed(before, after):
        return StepUpAction.ROLE_CHANGE.phrase
    return None
