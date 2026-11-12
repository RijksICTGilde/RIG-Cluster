"""Who may restore: the weighing the portal and the API share.

A restore has two deployments: the one whose data it READS and the one it lands in. The
portal weighed both, the API only the one in its path, so a project key could restore the
data of production into a deployment of its own.
"""

from __future__ import annotations

from starlette.requests import Request  # noqa: TC002 -- FastAPI reads the annotation at runtime

from opi.core.authz.actions import Action
from opi.http.auth.authz import require_action
from opi.http.auth.endpoint_util import require_api_action


def require_restore_source_access(request: Request, project_name: str, source_deployment: str) -> None:
    """Weigh ``restore.run`` on the deployment whose data this restore reads.

    Separately from the target, and not against the highest marking of the project: inside a
    project with production a developer may restore between two non-production deployments,
    but never out of production into a deployment of their own. A source that is not known
    weighs against the highest marking of the project, which is what an empty name does.

    Raises:
        HTTPException: 403 when the roles do not open the source.
    """
    if getattr(request.state, "authz_principal", None) is not None:
        # An API credential: the entrance recognised it, so the second weighing uses that
        # same principal instead of recognising it again.
        require_api_action(request, project_name, Action.RESTORE_RUN, deployment=source_deployment)
    else:
        require_action(request, Action.RESTORE_RUN, project_name, deployment=source_deployment)
