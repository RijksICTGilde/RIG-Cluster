"""The API's enforcement point: who is this caller, and may they perform this action here.

Every credential comes in here and is translated into ONE concept, so that afterwards there
is a single decision instead of one per kind:

- a token of a project member. It sits as a hash with that member in ``users`` of the
  project file, so it is gone as soon as the member is gone: a token is never more than the
  person or the service account it belongs to;
- the old project key from ``config.api-key``, only for a file whose migration to schema
  version 2.12 could not run because the key would not open. After that migration the key
  IS a token of the service account ``project-api``.

What a token may do follows from the member's ROLES and is not in the token itself. A role
change or a departure therefore takes effect the same second, without anything having to be
revoked. The decision itself falls in :func:`opi.core.authz.engine.decide`, the same
function the portal uses.
"""

from __future__ import annotations

import logging
import secrets
from collections import defaultdict
from dataclasses import replace
from typing import TYPE_CHECKING

from opi.core.actor import project_api_key_actor, service_account_actor, user_actor
from opi.core.authz.engine import (
    Cause,
    Decision,
    Principal,
    PrincipalKind,
    Resource,
    decide,
    record_decision,
)
from opi.services.members import MemberKind
from opi.services.project_members import hash_of, is_expired, member_named
from opi.services.project_roles import PLATFORM_ADMIN_ROLES, build_resource, member_roles
from opi.services.user_service import get_user_service

if TYPE_CHECKING:
    from starlette.requests import Request

    from opi.core.authz.actions import Action
    from opi.services.project_service import ProjectSummary, ProjectUser

logger = logging.getLogger(__name__)

#: The ``cause`` a caller bounces on who has the right role but is the wrong kind. A reason
#: of its own and not "insufficient rights": the role is right, the KIND is not, and that is
#: something else to solve.
PRINCIPAL_KIND_NOT_ALLOWED = "principal-kind-not-allowed"


class NotRecognisedError(Exception):
    """The credential is unknown or expired (401)."""


class AccessDeniedError(Exception):
    """The credential is valid but may not perform this action here (403)."""

    def __init__(self, message: str, *, cause: str = "insufficient-role") -> None:
        super().__init__(message)
        self.cause = cause


# Without this count, "the project key disappears once nobody uses it any more" is a wish.
# The table per (project, kind) is what ``opi/core/metrics.py`` hands out as a counter.
_usage: dict[tuple[str, str], int] = defaultdict(int)


def count_usage(project: str, kind: PrincipalKind) -> None:
    """Count a successful identification, per project and per kind of credential."""
    _usage[(project, kind.value)] += 1


def usage_per_project() -> dict[tuple[str, str], int]:
    """The count so far. Read by the Prometheus collector."""
    return dict(_usage)


def reset_usage() -> None:
    """Reset the count. For tests only."""
    _usage.clear()


def _token_owner(project: ProjectSummary, presented: str) -> ProjectUser | None:
    """The member whose token is presented here, or None.

    Compared on bytes and walked over ALL tokens, so that the duration of the loop says
    nothing about the hit.
    """
    presented_hash = hash_of(presented).encode()
    found: ProjectUser | None = None
    for member in project.users or []:
        for token in member.tokens:
            if secrets.compare_digest(token.hash.encode(), presented_hash):
                if is_expired(token.expires):
                    logger.warning("Token '%s' van '%s' is verlopen", token.name, member.principal)
                    continue
                found = member
    return found


async def recognise_principal(presented: str, project: ProjectSummary) -> Principal:
    """Translate an ``X-API-Key`` into a principal of THIS project.

    The project comes in as an object and is not fetched again here: the entry point that
    identifies the key against a project has already read it, and a second read would base
    the same decision on another moment.

    A token of ANOTHER project falls here as not recognised and not as denied: whether a
    project exists is itself something an unauthorised caller should not be able to read off,
    and a difference between those two answers makes project names probeable.

    Raises:
        NotRecognisedError: When the credential is unknown or expired.
    """
    member = _token_owner(project, presented)
    if member is not None:
        if member.type is MemberKind.SERVICE_ACCOUNT:
            name = member.name or ""
            return Principal(
                kind=PrincipalKind.SERVICE_ACCOUNT,
                name=name,
                project=project.name,
                roles=member_roles(member),
                actor=service_account_actor(project.name, name),
            )
        # A person's token is no more than that person: whoever is no longer a platform user
        # is no longer recognised here either. Without this read the token survives an
        # offboarding in the user administration, which touches no project file.
        email = member.email or ""
        if not get_user_service().is_email_allowed(email):
            raise NotRecognisedError(f"'{email}' heeft geen toegang tot het platform")
        return Principal(
            kind=PrincipalKind.HUMAN,
            name=email,
            project=project.name,
            roles=member_roles(member),
            actor=user_actor(email),
        )

    # The old project key. It only sits in a file whose migration to 2.12 could not open it;
    # after that migration it is a token of ``project-api``. It carries an administrator's
    # roles; what a person asks for falls down for it on the KIND.
    if project.api_key and secrets.compare_digest(project.api_key.encode(), presented.encode()):
        return Principal(
            kind=PrincipalKind.PROJECT_KEY,
            name=project.name,
            project=project.name,
            roles=PLATFORM_ADMIN_ROLES,
            actor=project_api_key_actor(project.name),
        )

    raise NotRecognisedError("bewijsmiddel is onbekend of verlopen")


def decide_action(
    principal: Principal,
    *,
    project: ProjectSummary,
    action: Action,
    resource: Resource | None = None,
    step_up_fresh: bool = True,
    request: Request | None = None,
) -> Decision:
    """Allow this action on this project, or refuse it.

    That the credential belongs to THIS project has already been established, in
    :func:`recognise_principal`; what still falls here is a valid credential that may do too
    little, or the wrong KIND of caller for this action.

    The roles are read AGAIN here and not copied from the principal: between recognising
    and deciding the project file may have changed, and then what is there now counts.

    A missing step-up is no refusal here: that is imposed per route by ``@requires_step_up``,
    which gives its own answer with the second factor.

    Raises:
        AccessDeniedError: When the credential does not open this action here.
    """
    if principal.kind is PrincipalKind.PROJECT_KEY:
        current = principal
    else:
        member = member_named(project, principal.name)
        if member is None:
            raise AccessDeniedError(f"'{principal.name}' is geen lid van project '{project.name}'")
        current = replace(principal, roles=member_roles(member))

    target = (
        resource
        if resource is not None
        else build_resource(project, project_name=project.name, path=f"project/{project.name}")
    )
    decision = decide(current, action, target, step_up_fresh=step_up_fresh)
    if request is not None:
        record_decision(request, action=action, resource=target, decision=decision, step_up_fresh=step_up_fresh)
    if decision.allowed or decision.cause is Cause.STEP_UP_REQUIRED or decision.step_up_would_help:
        return decision
    if decision.cause is Cause.PRINCIPAL_KIND_NOT_ALLOWED:
        raise AccessDeniedError(
            f"'{action.value}' vraagt een geverifieerde persoon; een service account heeft geen 2-factor",
            cause=PRINCIPAL_KIND_NOT_ALLOWED,
        )
    raise AccessDeniedError(f"de rollen van '{principal.name}' openen '{action.value}' niet in dit project")
