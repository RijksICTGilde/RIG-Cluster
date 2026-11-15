"""The action catalogue: the fixed list of things a caller can do.

A scope names actions and not paths: a new route has to pick an action, so the list does
not age the moment a route is added. The design is in
``features/futures/rechten-plan-van-aanpak.md``.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import Any, TypeVar


class Action(StrEnum):
    """What a caller can do on a project, as a closed list.

    The value is stored nowhere: a route points at an action and the rule register
    (:mod:`opi.core.authz.rules`) says which roles open it. Renaming therefore touches no
    issued token, but it does touch every route that names it.
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

    TOKEN_READ = "token.read"  # noqa: S105  # the name of an action, not a secret
    TOKEN_MANAGE = "token.manage"  # noqa: S105  # the name of an action, not a secret
    MEMBER_MANAGE = "member.manage"

    # Editing the project FILE through a form: what ``require_project_edit_access`` closed
    # off in the portal, for a modal or wizard step that touches more than one section.
    PROJECT_EDIT = "project.edit"

    ENV_VARS_MANAGE = "env-vars.manage"
    ENVIRONMENT_SET = "environment.set"
    JOB_RUN = "job.run"
    DATABASE_CONSOLE = "database.console"
    ATTACHMENT_MANAGE = "attachment.manage"
    AUDIT_READ = "audit.read"

    # What every signed-in user can do, apart from a project: the portal pages, the create
    # wizard, their own second factor and the step-up dialog.
    PORTAL_READ = "portal.read"
    WIZARD_USE = "wizard.use"
    ACCOUNT_OTP = "account.otp"
    STEP_UP_VERIFY = "step-up.verify"
    TOKEN_STEP_UP = "token.step-up"  # noqa: S105  # the name of an action, not a secret
    TOOLS_USE = "tools.use"

    # Platform administration. These routes stay behind ``is_platform_admin``; the action
    # exists so the route carries one and the audit can name it.
    PLATFORM_CLEANUP = "platform.cleanup"
    PLATFORM_RECONCILE = "platform.reconcile"
    PLATFORM_USERS = "platform.users"
    PLATFORM_APPROVALS = "platform.approvals"
    PLATFORM_USAGE = "platform.usage"
    PLATFORM_SERVICES = "platform.services"
    PLATFORM_METRICS = "platform.metrics"
    PLATFORM_FEDERATION = "platform.federation"
    PLATFORM_BACKUP = "platform.backup"
    PLATFORM_PROJECT_KEY = "platform.project-key"


#: Marking on a route that says which action it asks for. An attribute and not a register,
#: so a test (and the startup check) reads it out of the route table instead of out of a list
#: that sits next to the code; the same shape as ``REQUIRES_PROJECT_NAME``.
ACTION_ATTR = "zad_action"

F = TypeVar("F", bound=Callable[..., Any])


#: Marking with the fields the audit of THIS route may put in the row. Only through
#: ``audit_fields=`` below: ``fields=`` of ``@requires_step_up`` is independent of it,
#: because that is what the dialog repeats and may also name a free text field.
AUDIT_FIELDS_ATTR = "zad_audit_fields"


def action(which: Action, *, audit_fields: tuple[str, ...] = ()) -> Callable[[F], F]:
    """Put the action this route asks for on the route itself.

    Belongs ABOVE the authentication decorator, so the marking lands on the object FastAPI
    registers as the endpoint. The function itself is not wrapped.

    ``audit_fields`` names the fields that end up in the audit row, for a route whose value
    does not land in the project file (which snapshot, which image) and that has no step-up
    to name them with ``fields=``. Never a field that points at a person: what goes in here
    stays for a year (``features/audit.md``).
    """

    def mark(func: F) -> F:
        setattr(func, ACTION_ATTR, which)
        if audit_fields:
            setattr(func, AUDIT_FIELDS_ATTR, audit_fields)
        return func

    return mark


def action_of(endpoint: Callable[..., object]) -> Action | None:
    """The action of a route endpoint, or None when it carries none."""
    return getattr(endpoint, ACTION_ATTR, None)


def audit_fields_of(endpoint: Callable[..., object]) -> tuple[str, ...]:
    """The fields this route names in the audit row, or an empty tuple."""
    return tuple(getattr(endpoint, AUDIT_FIELDS_ATTR, ()))


#: Marking on a route that does not have its deployment in the PATH, with the function that
#: pulls it out of the route's arguments.
DEPLOYMENT_ATTR = "zad_deployment_from"


def deployment_from(resolve: Callable[..., str | None]) -> Callable[[F], F]:
    """Say where the deployment of this route sits when it is not in the path.

    Without such a declaration the call weighs against the HIGHEST marking of the project
    (:func:`opi.services.project_roles.build_resource`), and then a role that may only touch
    non-production can do nothing on this route. *resolve* gets the arguments of the route
    and returns the name. Belongs in the same place as ``@action``: above the authentication
    decorator.
    """

    def mark(func: F) -> F:
        setattr(func, DEPLOYMENT_ATTR, resolve)
        return func

    return mark


def deployment_of(endpoint: Callable[..., object], route_kwargs: dict[str, Any]) -> str | None:
    """The deployment this route points at out of its own arguments, or None."""
    resolve = getattr(endpoint, DEPLOYMENT_ATTR, None)
    return resolve(**route_kwargs) if resolve is not None else None


#: Marker on a route that may name a deployment that does not exist (yet), with the
#: function that gives the marking such a deployment is weighed against.
ABSENT_MARKING_ATTR = "zad_absent_marking"


def marking_if_absent(resolve: Callable[..., str]) -> Callable[[F], F]:
    """Say which marking a deployment this route names, but that does not exist, weighs as.

    Without it such a deployment weighs as the HIGHEST marking of the project, which is
    right for a route that only reads or changes what exists. A route that creates a
    deployment, or deletes one that is already gone, would otherwise need ops in every
    project with production.
    """

    def mark(func: F) -> F:
        setattr(func, ABSENT_MARKING_ATTR, resolve)
        return func

    return mark


def marking_if_absent_of(endpoint: Callable[..., object], route_kwargs: dict[str, Any]) -> str | None:
    """The marking this route gives a deployment that does not exist, or None."""
    resolve = getattr(endpoint, ABSENT_MARKING_ATTR, None)
    return resolve(**route_kwargs) if resolve is not None else None


#: Marker on a route whose request can amount to a narrower action than the one it
#: declares, with the function that recognises that request.
WEIGHED_ACTION_ATTR = "zad_weighed_action"


def weighed_action_from(resolve: Callable[..., Action | None]) -> Callable[[F], F]:
    """Say which action this request really performs, when that differs from the route's.

    *resolve* returns None to keep the declared action. The declared action stays what the
    startup check and the matrix see; only the decision on this one request changes.
    """

    def mark(func: F) -> F:
        setattr(func, WEIGHED_ACTION_ATTR, resolve)
        return func

    return mark


def weighed_action_of(endpoint: Callable[..., object], route_kwargs: dict[str, Any]) -> Action | None:
    """The action this request is weighed as instead of the declared one, or None."""
    resolve = getattr(endpoint, WEIGHED_ACTION_ATTR, None)
    return resolve(**route_kwargs) if resolve is not None else None


#: Marking on a route that carries a target in the QUERY, with the names that may be read
#: there.
QUERY_TARGETS_ATTR = "zad_query_targets"


def targets_in_query(*names: str) -> Callable[[F], F]:
    """Say which targets of this route sit in the query and not in the path.

    The counterpart of :func:`deployment_from` for the portal, where the gate holds the
    request and not the arguments of the route. Without this declaration
    ``enforce_route_action`` reads the path only: a query pointing the gate at a DIFFERENT
    deployment than the handler uses could widen the decision. Belongs in the same place as
    ``@action``.
    """

    def mark(func: F) -> F:
        setattr(func, QUERY_TARGETS_ATTR, frozenset(names))
        return func

    return mark


def query_targets_of(endpoint: Callable[..., object]) -> frozenset[str]:
    """The target names this route lets the query be read for, or an empty set."""
    return frozenset(getattr(endpoint, QUERY_TARGETS_ATTR, frozenset()))
