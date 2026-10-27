"""
Authentication decorators for marking routes with SSO requirements.

These decorators provide a clean way to annotate routes with their
authentication requirements instead of using URL-based checks.
"""

import inspect
import logging
from functools import wraps
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from starlette.requests import Request  # noqa: TC002 -- FastAPI needs Request at runtime

from opi.core.step_up import StepUpActie, has_fresh_step_up, log_step_up_refusal

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


def requires_sso(func: Callable) -> Callable:
    """
    Decorator to mark a route as requiring SSO authentication.

    This decorator adds metadata to the function indicating that
    the route requires user authentication via Keycloak SSO.

    Args:
        func: The route handler function

    Returns:
        The decorated function with SSO requirement metadata
    """

    @wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        return await func(*args, **kwargs)

    # Add metadata to indicate SSO is required
    wrapper._requires_sso = True
    logger.debug(f"Marked function {func.__name__} as requiring SSO")
    return wrapper


def get_current_user(request) -> dict[str, Any] | None:
    """
    Utility function to get the current authenticated user from request.

    This is a helper function that route handlers can use to access
    the current user information.

    Drie wegen vullen ``request.state.user``: de autorisatiemiddleware uit een sessie,
    de bearer-decorators uit een geverifieerd SSO-token, en ``handhaaf`` uit een
    persoonlijk API-token (RC-322). Een gevulde waarde zegt dus WIE de aanroeper is en
    NIET dat hij een browserflow doorliep: hang er geen controle aan die een sessie of
    een tweede factor veronderstelt. Een step-up hoort op ``@requires_step_up``, dat de
    code per verzoek tegen deze persoon toetst.

    Args:
        request: The FastAPI/Starlette request object

    Returns:
        User information dictionary if authenticated, None otherwise
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
    if not get_user_service().is_platform_admin(email):
        raise HTTPException(status_code=403, detail="Alleen beheerders hebben toegang")
    return user


#: Markering op een route die zich voor een step-up heeft aangemeld, met de
#: :class:`~opi.core.step_up.StepUpActie` als waarde. ``tests/test_step_up_routes.py``
#: leest hem, en ``custom_openapi`` documenteert de header en de 403 eruit: zonder
#: markering zou een route die de decorator kwijtraakt stilzwijgend weer open staan.
REQUIRES_STEP_UP = "zad_requires_step_up"


def requires_step_up(condition: Callable[..., Any], *, actie: StepUpActie, velden: tuple[str, ...] = ()) -> Callable:
    """Laat een handeling zelf verklaren dat ze een verse step-up vraagt (RC-332).

    ``condition`` krijgt het verzoek en de argumenten van de route en zegt of DEZE
    aanroep er een vraagt; is hij onwaar, dan gebeurt er niets extra. ``actie`` zegt welke
    handeling het is, en draagt zelf de teksten voor de dialoog, de log en de client.

    Twee wegen, omdat er twee soorten aanroepers zijn: het portaal heeft een sessie en
    krijgt een dialoog, de API heeft die niet en draagt de code van de eigenaar van het
    token per verzoek mee.

    ``velden`` noemt de formuliervelden die de herhaling in het portaal moet meesturen:
    de dialoog neemt de plaats in van het formulier, dus zonder die velden zou een
    handeling met een body (een rol, een tokennaam, een aantal dagen) ze kwijt zijn. Per
    route opgesomd en niet het hele formulier, zodat er nooit een veld in de pagina
    terugkomt dat de route niet noemde.
    """

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            # Het verzoek komt als keyword van FastAPI en positioneel van een test die de
            # route rechtstreeks aanroept.
            request: Request = kwargs.get("request") or args[0]
            argumenten = {sleutel: waarde for sleutel, waarde in kwargs.items() if sleutel != "request"}

            nodig = condition(request, **argumenten)
            if inspect.isawaitable(nodig):
                nodig = await nodig
            if not nodig:
                return await func(*args, **kwargs)

            project_name = str(argumenten.get("project_name", ""))
            doelwit = str(
                argumenten.get("deployment_name")
                or argumenten.get("component_name")
                or argumenten.get("mark_id")
                or argumenten.get("naam")
                or project_name
            )

            if request.url.path.startswith("/api/"):
                from opi.api.otp_step_up import step_up_problem_response, verify_api_step_up

                probleem = await verify_api_step_up(request, project_name=project_name, actie=actie, doelwit=doelwit)
                if probleem is not None:
                    return step_up_problem_response(probleem)
                return await func(*args, **kwargs)

            email = str((get_current_user(request) or {}).get("email", ""))
            if not has_fresh_step_up(request):
                tekst = log_step_up_refusal(actie.handeling, project_name=project_name, actor=email, doelwit=doelwit)
                return await _step_up_refusal(
                    request, actie.handeling, tekst, velden=await _velden(request, velden), methode=request.method
                )

            logger.info(
                "Verse step-up gebruikt om %s: doel '%s', project '%s', door %s",
                actie.handeling,
                doelwit,
                project_name,
                email,
            )
            return await func(*args, **kwargs)

        setattr(wrapper, REQUIRES_STEP_UP, actie)
        return wrapper

    return decorator


async def _velden(request: Request, namen: tuple[str, ...]) -> dict[str, str]:
    """De genoemde velden uit het formulier van dit verzoek, voor de herhaling."""
    if not namen:
        return {}
    formulier = await request.form()
    return {naam: str(formulier[naam]) for naam in namen if naam in formulier}


async def _step_up_refusal(
    request: Request, handeling: str, tekst: str, *, velden: dict[str, str] | None = None, methode: str = "POST"
) -> Any:
    """De dialoog voor een htmx-verzoek, dezelfde tekst als 403 voor al het andere.

    De dialoog komt uit de weblaag; die importeren op modulehoogte zou een kring maken,
    want de weblaag leunt zelf op dit bestand. ``methode`` is die van dit verzoek, zodat
    de herhaling na een geslaagde code dezelfde methode gebruikt.
    """
    from opi.web.step_up_dialog import render_step_up_dialog, step_up_dialog_response

    if request.headers.get("HX-Request"):
        html = await render_step_up_dialog(
            request, vervolg=request.url.path, handeling=handeling, vervolg_velden=velden, methode=methode
        )
        return step_up_dialog_response(html)
    raise HTTPException(status_code=403, detail=tekst)
