"""De actiecatalogus: de vaste lijst dingen die een aanroeper kan doen.

Fase 1 uit ``features/futures/rechten-plan-van-aanpak.md``, ingevoerd voor het oppervlak
dat een token kan bereiken. Een scope noemt handelingen en geen paden: een nieuwe route
moet een handeling kiezen, dus de lijst veroudert niet zodra er een route bij komt.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import Any, TypeVar


class Action(StrEnum):
    """Wat een aanroeper op een project kan doen, als gesloten lijst.

    De waarde staat nergens opgeslagen: een route wijst een handeling aan en het
    regelregister (:mod:`opi.core.authz.rules`) zegt welke rollen die opent. Hernoemen
    raakt dus geen uitgegeven token, maar wel elke route die de naam noemt.
    """

    PROJECT_READ = "project.read"
    PROJECT_LIST = "project.list"
    PROJECT_CREATE = "project.create"
    PROJECT_DELETE = "project.delete"
    PROJECT_REFRESH = "project.refresh"

    DEPLOYMENT_READ = "deployment.read"
    DEPLOYMENT_WRITE = "deployment.write"
    DEPLOYMENT_DELETE = "deployment.delete"
    DEPLOYMENT_REFRESH = "deployment.refresh"
    DEPLOYMENT_IMAGE_REPLACE = "deployment.image-replace"
    DEPLOYMENT_DOMAIN_SET = "deployment.domain-set"
    DEPLOYMENT_CLONE_FROM = "deployment.clone-from"

    COMPONENT_READ = "component.read"
    COMPONENT_ADD = "component.add"
    COMPONENT_UPDATE = "component.update"
    COMPONENT_DELETE = "component.delete"
    COMPONENT_LINK = "component.link"

    SERVICE_READ = "service.read"
    SERVICE_ADD = "service.add"
    SERVICE_CONFIGURE = "service.configure"
    SERVICE_WRITE_VALUES = "service.write-values"
    SERVICE_ACTION = "service.action"

    DATABASE_CLONE = "database.clone"
    DATABASE_SCHEMA_READ = "database.schema-read"
    DATABASE_SCHEMA_MANAGE = "database.schema-manage"
    BUCKET_CLONE = "bucket.clone"
    CLONE_VALIDATE = "clone.validate"

    REGISTRY_ADD = "registry.add"
    IMAGE_PUSH = "image.push"

    BACKUP_READ = "backup.read"
    BACKUP_CREATE = "backup.create"
    BACKUP_DELETE = "backup.delete"
    RESTORE_READ = "restore.read"
    RESTORE_RUN = "restore.run"

    LOGS_READ = "logs.read"
    RESOURCES_ADJUST = "resources.adjust"
    RESOURCES_CLEANUP = "resources.cleanup"
    SUBDOMAIN_READ = "subdomain.read"
    CLUSTER_READ = "cluster.read"

    TASK_READ = "task.read"
    TASK_CANCEL = "task.cancel"
    SLEEP_READ = "sleep.read"
    SLEEP_WAKE = "sleep.wake"

    PROJECT_KEY_READ = "project-key.read"

    TOKEN_READ = "token.read"  # noqa: S105 -- handelingsnaam, geen secret
    TOKEN_MANAGE = "token.manage"  # noqa: S105 -- handelingsnaam, geen secret
    MEMBER_MANAGE = "member.manage"

    # Het projectBESTAND bewerken via een formulier: wat ``require_project_edit_access``
    # in het portaal afsloot, voor een modal of wizardstap die meer dan een sectie raakt.
    PROJECT_EDIT = "project.edit"

    ENV_VARS_MANAGE = "env-vars.manage"
    ENVIRONMENT_SET = "environment.set"
    JOB_RUN = "job.run"
    DATABASE_CONSOLE = "database.console"
    ATTACHMENT_MANAGE = "attachment.manage"
    AUDIT_READ = "audit.read"

    # Wat elke ingelogde gebruiker kan, los van een project: de portaalpagina's, de
    # aanmaakwizard, de eigen tweede factor en de step-up-dialoog.
    PORTAL_READ = "portal.read"
    WIZARD_USE = "wizard.use"
    ACCOUNT_OTP = "account.otp"
    STEP_UP_VERIFY = "step-up.verify"
    TOOLS_USE = "tools.use"

    # Platformbeheer. Deze routes blijven achter ``is_platform_admin``; de action bestaat
    # zodat de route er een draagt en de audit hem kan noemen.
    PLATFORM_CLEANUP = "platform.cleanup"
    PLATFORM_RECONCILE = "platform.reconcile"
    PLATFORM_USERS = "platform.users"
    PLATFORM_APPROVALS = "platform.approvals"
    PLATFORM_USAGE = "platform.usage"
    PLATFORM_SERVICES = "platform.services"
    PLATFORM_ROLES = "platform.roles"
    PLATFORM_METRICS = "platform.metrics"
    PLATFORM_FEDERATION = "platform.federation"
    PLATFORM_BACKUP = "platform.backup"
    PLATFORM_PROJECT_KEY = "platform.project-key"


#: Markering op een route die zegt welke handeling hij vraagt. Een attribuut en geen
#: register, zodat een test (en de opstartcontrole) het uit de routetabel leest in plaats
#: van uit een lijst die naast de code staat; dezelfde vorm als ``REQUIRES_PROJECT_NAME``.
ACTION_ATTR = "zad_action"

F = TypeVar("F", bound=Callable[..., Any])


#: Markering met de velden die de audit van DEZE route in de rij mag zetten. Alleen door
#: ``audit_fields=`` hieronder: ``velden=`` van ``@requires_step_up`` staat er los van,
#: want dat is wat de dialoog herhaalt en mag ook een vrij tekstveld noemen.
AUDIT_FIELDS_ATTR = "zad_audit_fields"


def action(which: Action, *, audit_fields: tuple[str, ...] = ()) -> Callable[[F], F]:
    """Zet de handeling die deze route vraagt op de route zelf.

    Hoort BOVEN de authenticatiedecorator te staan, zodat de markering op het object komt
    dat FastAPI als endpoint registreert. De functie zelf wordt niet gewikkeld.

    ``audit_fields`` noemt de velden die in de auditrij komen, voor een route waarvan de
    waarde niet in het projectbestand landt (welke snapshot, welk image) en die geen
    step-up heeft om ze met ``velden=`` te noemen. Nooit een veld dat een persoon
    aanwijst: wat hier in komt blijft een jaar staan (``features/audit.md``).
    """

    def markeer(func: F) -> F:
        setattr(func, ACTION_ATTR, which)
        if audit_fields:
            setattr(func, AUDIT_FIELDS_ATTR, audit_fields)
        return func

    return markeer


def action_of(endpoint: Any) -> Action | None:
    """De handeling van een route-endpoint, of None als hij er geen draagt."""
    return getattr(endpoint, ACTION_ATTR, None)


def audit_fields_of(endpoint: Any) -> tuple[str, ...]:
    """De velden die deze route in de auditrij noemt, of een leeg paar haken."""
    return tuple(getattr(endpoint, AUDIT_FIELDS_ATTR, ()))


#: Markering op een route die zijn deployment niet in het PAD heeft, met de functie die
#: hem uit de argumenten van de route haalt.
DEPLOYMENT_ATTR = "zad_deployment_from"


def deployment_from(resolve: Callable[..., str | None]) -> Callable[[F], F]:
    """Zeg waar de deployment van deze route staat als hij niet in het pad zit.

    Zonder zo'n verklaring weegt de aanroep tegen de HOOGSTE markering van het project
    (:func:`opi.services.project_roles.build_resource`), en dan kan een rol die alleen
    niet-productie mag op deze route niets. *resolve* krijgt de argumenten van de route en
    geeft de naam terug. Hoort op dezelfde plek als ``@action``: boven de
    authenticatiedecorator.
    """

    def markeer(func: F) -> F:
        setattr(func, DEPLOYMENT_ATTR, resolve)
        return func

    return markeer


def deployment_of(endpoint: Any, route_kwargs: dict[str, Any]) -> str | None:
    """De deployment die deze route uit zijn eigen argumenten aanwijst, of None."""
    resolve = getattr(endpoint, DEPLOYMENT_ATTR, None)
    return resolve(**route_kwargs) if resolve is not None else None
