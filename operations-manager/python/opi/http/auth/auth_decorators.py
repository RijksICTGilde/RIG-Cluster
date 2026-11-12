"""Decorators a route uses to declare what it needs: SSO, platform admin, a step-up."""

import inspect
import logging
from functools import wraps
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode, urlsplit

from fastapi import HTTPException
from starlette.requests import Request  # noqa: TC002 -- FastAPI needs Request at runtime

from opi.core.audit_trail import fields_from_form
from opi.services.step_up import StepUpAction, has_fresh_step_up, is_elevated_admin, log_step_up_refusal

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

#: The route behind the popup where a platform admin activates elevated permissions.
ELEVATE_PATH = "/step-up/elevate"

ELEVATION_NEEDED = "Activeer eerst verhoogde rechten via het menu rechtsboven."


def requires_sso(func: Callable) -> Callable:
    """Mark a route as requiring Keycloak SSO, as metadata on the function."""

    @wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        return await func(*args, **kwargs)

    wrapper._requires_sso = True
    logger.debug(f"Marked function {func.__name__} as requiring SSO")
    return wrapper


def get_current_user(request) -> dict[str, Any] | None:
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


def require_platform_admin(request) -> dict[str, Any]:
    """Return the current user, or refuse the request if they are not a platform admin.

    Hiding a link in the menu is presentation, not access control: a page meant for
    admins has to say so itself, or the URL is the way in.
    """
    from opi.services.user_service import get_user_service

    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Niet ingelogd")
    email = str(user.get("email", "")).lower()
    users = get_user_service()
    if not users.is_platform_admin(email):
        raise HTTPException(status_code=403, detail="Alleen beheerders hebben toegang")
    if not is_elevated_admin(users, email):
        raise _elevation_required(request)
    return user


def _elevation_required(request) -> HTTPException:
    """Send a platform admin without a fresh step-up to the popup that activates it."""
    if request.headers.get("HX-Request"):
        back = urlsplit(request.headers.get("HX-Current-URL", "")).path or "/"
        return HTTPException(status_code=403, detail=ELEVATION_NEEDED, headers={"HX-Redirect": elevate_url(back)})
    if request.method == "GET":
        back = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return HTTPException(status_code=303, detail=ELEVATION_NEEDED, headers={"Location": elevate_url(back)})
    return HTTPException(status_code=403, detail=ELEVATION_NEEDED)


def elevate_url(back: str) -> str:
    """The dashboard with the popup open, which returns to ``back`` after a valid code.

    The dashboard because an admin page cannot show itself without the rights it asks for.
    """
    return f"/dashboard?{urlencode({'verhoog': back})}"


#: Marking on a route that signed up for a step-up, with the
#: :class:`~opi.services.step_up.StepUpAction` as its value. ``tests/test_step_up_routes.py``
#: reads it, and ``custom_openapi`` documents the 403 out of it: without a marking a route
#: that loses the decorator would quietly stand open again.
REQUIRES_STEP_UP = "zad_requires_step_up"


def requires_step_up(
    condition: Callable[..., Any] | None = None, *, action: StepUpAction, fields: tuple[str, ...] = ()
) -> Callable:
    """Let an action declare for itself that it asks for a fresh step-up (RC-332).

    Without ``condition`` the answer comes out of the authorization system: the route
    carries an action, and the grant that opens it says whether a fresh second factor
    belongs with it (``Cause.STEP_UP_REQUIRED``). A ``condition`` of its own is for what a
    scope cannot see, such as a CHANGE that lowers a marking. ``action`` carries the texts
    for the dialog, the log and the client.

    Two routes, because there are two kinds of caller: the portal has a session and gets a
    dialog, the API reads the window the caller opened with ``POST /api/v2/step-up``.

    ``fields`` names the form fields the repeat in the portal has to send along: the dialog
    takes the place of the form, so without those fields an action with a body (a role, a
    token name, a number of days) would lose them. Listed per route and not the whole form,
    so a field the route did not name can never come back in the page.
    """

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            # The request comes as a keyword from FastAPI and positionally from a test that
            # calls the route directly.
            request: Request = kwargs.get("request") or args[0]
            arguments = {key: value for key, value in kwargs.items() if key != "request"}

            if condition is None:
                needed = _register_requires_step_up(request)
            else:
                needed = condition(request, **arguments)
                if inspect.isawaitable(needed):
                    needed = await needed
            if not needed:
                return await func(*args, **kwargs)

            project_name = str(arguments.get("project_name", ""))
            target = str(
                arguments.get("deployment_name")
                or arguments.get("component_name")
                or arguments.get("mark_id")
                or arguments.get("naam")
                or arguments.get("user_id")
                or project_name
            )

            if request.url.path.startswith("/api/"):
                from opi.http.auth.otp_step_up import step_up_problem_response, verify_api_step_up

                problem = await verify_api_step_up(request, project_name=project_name, action=action, target=target)
                if problem is not None:
                    return step_up_problem_response(problem)
                return await func(*args, **kwargs)

            email = str((get_current_user(request) or {}).get("email", ""))
            if not has_fresh_step_up(request):
                text = log_step_up_refusal(action.phrase, project_name=project_name, actor=email, target=target)
                return await _step_up_refusal(
                    request,
                    action.phrase,
                    text,
                    fields=await fields_from_form(request, fields),
                    method=request.method,
                )

            logger.info(
                "Verse step-up gebruikt om %s: doel '%s', project '%s', door %s",
                action.phrase,
                target,
                project_name,
                email,
            )
            return await func(*args, **kwargs)

        setattr(wrapper, REQUIRES_STEP_UP, action)
        return wrapper

    return decorator


async def _step_up_refusal(
    request: Request, action_phrase: str, text: str, *, fields: dict[str, str] | None = None, method: str = "POST"
) -> Any:
    """The dialog for an htmx request, the same text as a 403 for everything else.

    The dialog comes out of the web layer; importing that at module level would make a
    cycle, because the web layer leans on this file itself. ``method`` is the one of this
    request, so the repeat after a valid code uses the same method.
    """
    from opi.http.auth.step_up_dialog import render_step_up_dialog, step_up_dialog_response

    if request.headers.get("HX-Request"):
        html = await render_step_up_dialog(
            request, follow_url=request.url.path, action_text=action_phrase, follow_fields=fields, method=method
        )
        return step_up_dialog_response(html)
    raise HTTPException(status_code=403, detail=text)


def _register_requires_step_up(request: Request) -> bool:
    """Whether the decision of THIS request asks for a fresh second factor.

    The gate has already called ``decide()`` and put the outcome on the request
    (``record_decision``): ``enforce_route_action`` in the portal, ``enforce`` on the API.
    Deciding again would be a second read of the project file, and therefore a second
    answer to the same question.

    When the decision is missing, the gate did not run, and asking for a second factor is
    the safe outcome.
    """
    decision = getattr(request.state, "authz_decision", None)
    return decision.requires_step_up if decision is not None else True
