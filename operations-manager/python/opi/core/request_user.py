"""Who the current request is from, as the entrances have recorded it."""

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from starlette.requests import Request

logger = logging.getLogger(__name__)


def get_current_user(request: Request) -> dict[str, Any] | None:
    """The authenticated user on this request, or None.

    Three routes fill ``request.state.user``: the authorization middleware out of a
    session, the bearer decorators out of a verified SSO token, and ``enforce`` out of a
    personal API token (RC-322). A filled value therefore says WHO the caller is and NOT
    that they went through a browser flow: hang no check on it that assumes a session or a
    second factor. A step-up belongs on ``@requires_step_up``, which reads the window of
    this person.
    """
    user = getattr(request.state, "user", None)
    if user:
        logger.debug(f"Retrieved current user: {user.get('email', 'unknown')}")
    else:
        logger.debug("No current user found in request state")
    return user
