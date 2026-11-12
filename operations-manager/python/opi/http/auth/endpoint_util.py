import logging
import secrets
from collections.abc import Callable, Iterable  # noqa: TC003 -- used in decorator signatures at runtime
from functools import wraps
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from starlette.requests import Request  # noqa: TC002 — FastAPI needs Request at runtime

from opi.core.actor import set_actor
from opi.core.audit_trail import capture_fields
from opi.core.authz.actions import Action, action_of, deployment_of, marking_if_absent_of, weighed_action_of
from opi.core.authz.engine import RuleError, rule_for
from opi.core.config import settings
from opi.http.auth.principal import (
    AccessDeniedError,
    NotRecognisedError,
    Principal,
    PrincipalKind,
    count_usage,
    decide_action,
    recognise_principal,
)
from opi.http.auth.user_token_auth import (
    REQUIRES_PLATFORM_ADMIN_TOKEN,
    REQUIRES_USER_TOKEN,
    UserTokenError,
    extract_bearer_token,
)
from opi.http.rate_limiter import refuse_after_auth_failure, refuse_when_key_failures_exhausted
from opi.manager.run_support import find_deployment
from opi.services.api_step_up_service import ApiStepUpService, personal_token_credential
from opi.services.catalog.publish_on_web.domain_config import DomainSetting, get_domain_setting
from opi.services.project_environment import DEFAULT_ENVIRONMENT, Omgeving, effective_environment
from opi.services.project_roles import build_resource, resource_path
from opi.services.project_store import get_project_store
from opi.services.services_enums import CloneFromType

if TYPE_CHECKING:
    from opi.services.project_service import ProjectSummary

REQUIRES_PROJECT_NAME = "zad_requires_project_name"
"""Marker on a route that is guarded with a project key."""

#: The two messages on a credential that is not recognised. A constant, because the image
#: push's pre-check gives the same 401 as the gate here.
_NO_KEY = "Authentication required - provide X-API-Key header"
_WRONG_KEY = "Invalid API key"


REQUIRES_METRICS_SCRAPE_TOKEN = "zad_requires_metrics_scrape_token"  # noqa: S105  # a route marker, not a secret
"""Marker on a route that is guarded with PROMETHEUS_METRICS_AUTH_TOKEN."""


def validate_api_token(func: Callable[..., Any]) -> Callable[..., Any]:
    """Require a project-specific X-API-Key, validated against project_name in the route."""

    @wraps(func)
    async def wrapper(*args: Any, request: Request, **kwargs: Any) -> Any:
        logger = logging.getLogger(__name__)
        logger.debug(f"API route {func.__name__} called with authentication")

        x_api_key = request.headers.get("X-API-Key")
        if not x_api_key:
            logger.warning(f"Authentication failed for route {func.__name__} - no X-API-Key provided")
            raise HTTPException(status_code=401, detail=_NO_KEY)

        # ALWAYS require project_name parameter and validate it matches
        project_name_from_url = kwargs.get("project_name")

        if not project_name_from_url:
            # A route error, not a call error: the caller can do nothing about it.
            logger.error(
                f"Route {func.__name__} draagt validate_api_token maar kreeg geen project_name; "
                "zonder project valt een projectsleutel niet te controleren"
            )
            raise HTTPException(status_code=401, detail="Missing project_name parameter")

        refuse_when_key_failures_exhausted(x_api_key)

        project = get_project_store().get(project_name_from_url)
        if not project:
            # Not distinguished from a wrong key: whether a project exists is itself
            # something an unauthorised caller should not be able to read off.
            logger.warning(f"Authentication failed for route {func.__name__} - unknown project")
            refuse_after_auth_failure(request, x_api_key)
            raise HTTPException(status_code=401, detail=_WRONG_KEY)

        await enforce(request, x_api_key, wrapper, project, route_kwargs=kwargs)

        # Add project_id to kwargs so the route function can access it if needed
        kwargs["project_name"] = project.name
        return await func(*args, request=request, **kwargs)

    setattr(wrapper, REQUIRES_PROJECT_NAME, True)
    return wrapper


async def enforce(
    request: Request,
    presented: str,
    endpoint: Callable[..., Any],
    project: ProjectSummary,
    *,
    route_kwargs: dict[str, Any] | None = None,
) -> None:
    """Recognise this ``X-API-Key`` and allow this route's action.

    A route puts its own endpoint object in here, because that carries the marker
    :func:`opi.core.authz.actions.action` put on it. Shared with the routers that do their
    key check by hand (tasks, sleep mode), so that a token works the same there as on the
    rest of the API.

    The project comes in as an object, because the caller has had to read it already to know
    that it exists; another read here would decide the same request on two snapshots.

    When the token belongs to a PERSON, that person lands here on ``request.state.user``.
    That is where :func:`opi.http.auth.otp_step_up.verify_api_step_up` reads the owner of the
    token, so that a step-up on a ``zad_`` token runs exactly the same way as on an SSO
    bearer. A service account and the old project key leave it empty, and so bounce there on
    ``principal-kind-not-allowed``.

    Raises:
        HTTPException: 401 when the credential is not recognised, 403 when it does not open
            this action here.
    """
    logger = logging.getLogger(__name__)
    principal = await _weigh(request, presented, endpoint, project, route_kwargs=route_kwargs)

    # The principal on the request, so that a handler can have a SECOND action checked
    # without recognising the credential again (:func:`require_api_action`).
    request.state.authz_principal = principal
    count_usage(project.name, principal.kind)
    if principal.kind is PrincipalKind.HUMAN:
        request.state.user = {"email": principal.name}
        request.state.step_up_credential = personal_token_credential(presented)
        request.state.step_up_fresh = await ApiStepUpService().is_fresh(
            request.state.step_up_credential, principal.name
        )
    logger.debug("Toegestaan: %s mag %s op %s", principal.actor, action_of(endpoint), project.name)


async def _weigh(
    request: Request,
    presented: str,
    endpoint: Callable[..., Any],
    project: ProjectSummary,
    *,
    route_kwargs: dict[str, Any] | None = None,
) -> Principal:
    """Recognise the credential and allow ``endpoint``'s action, or refuse.

    Shared with :func:`image_push_refusal`, which has to weigh the same before the multipart
    parser as the route behind it: what the pre-check decides and what the gate decides can
    then not diverge.

    Raises:
        HTTPException: 401 when the credential is not recognised, 403 when it does not open
            this action here.
    """
    logger = logging.getLogger(__name__)
    project_name = project.name
    requested = action_of(endpoint)
    if requested is None:
        # The startup check should make this impossible; if it does pass here anyway,
        # letting it through silently is the wrong outcome.
        raise RuntimeError(f"Route {getattr(endpoint, '__name__', endpoint)} draagt geen handeling")

    try:
        principal = await recognise_principal(presented, project)
    except NotRecognisedError as exc:
        logger.warning("Authenticatie mislukt voor project %s: %s", project_name, exc)
        refuse_after_auth_failure(request, presented)
        raise HTTPException(status_code=401, detail=_WRONG_KEY) from exc

    # Here and not after the decision: a REFUSAL has to name the acting party (BIO2
    # 8.15.01), and that is known as soon as the credential is recognised. Measured on the
    # sandbox: without this line a refused API call carried an empty actor.
    set_actor(principal.actor)

    # For the audit, and only after recognising: the hook runs after the answer, when this
    # request's fields can no longer be read, and an unknown caller should not get a body
    # parsed.
    await capture_fields(request, endpoint)

    # The deployment and the component out of the PATH, so that the production scopes on the
    # API weigh the same as in the portal. When the deployment is not in the path, the route
    # says with ``@deployment_from`` where it is; without that declaration ``build_resource``
    # weighs the call against the project's highest marking.
    deployment = request.path_params.get("deployment_name") or deployment_of(endpoint, route_kwargs or {})
    component = request.path_params.get("component_name")
    resource = build_resource(
        project,
        project_name=project_name,
        path=resource_path(project_name, deployment, component),
        deployment=deployment,
        component=component,
        marking_if_absent=marking_if_absent_of(endpoint, route_kwargs or {}),
    )
    requested = weighed_action_of(endpoint, route_kwargs or {}) or requested

    try:
        decide_action(principal, project=project, action=requested, resource=resource, request=request)
    except AccessDeniedError as exc:
        logger.warning("Geweigerd voor %s op %s (%s): %s", principal.actor, project_name, requested.value, exc)
        # The reason as a field of its own and not only in the text: a caller has to be able
        # to tell "this kind of caller may never do this" from "your role is too narrow",
        # because those are two different things to solve.
        raise HTTPException(status_code=403, detail={"cause": exc.cause, "message": str(exc)}) from exc

    return principal


async def image_push_refusal(
    request: Request, presented: str | None, project_name: str, endpoint: Callable[..., Any]
) -> HTTPException | None:
    """Check whether this credential may do ``image.push``; return an answer on a refusal."""
    logger = logging.getLogger(__name__)
    project = get_project_store().get(project_name)
    if not presented or project is None:
        # Hide whether the project exists or the key is wrong.
        reason = "onbekend project" if presented else "geen X-API-Key"
        logger.warning("Authenticatie mislukt voor project %s: %s", project_name, reason)
        refuse_after_auth_failure(request, presented)
        return HTTPException(status_code=401, detail=_WRONG_KEY if presented else _NO_KEY)

    try:
        await _weigh(request, presented, endpoint, project)
    except HTTPException as refused:
        return refused
    return None


def require_api_action(request: Request, project_name: str, action: Action, *, deployment: str | None = None) -> None:
    """Allow a SECOND action on this request, beside the route's own.

    For a body that asks more than the path shows: writing a deployment that also sets the
    OTAP marking asks ``environment.set`` beside ``deployment.write``. This request's
    principal is already on the request, so the credential is not recognised again and the
    two checks cannot come out on different callers.

    *deployment* says on WHICH deployment this second action works, when that is another
    than the route's: an upsert with ``cloneFrom`` reads the database of the SOURCE, and
    without that target this second check would weigh against the project's highest marking
    instead of against the source (RC-392 security review). Without a name the behaviour
    stays what it was.

    Raises:
        HTTPException: 403 when the credential does not open this action here.
        RuntimeError: When the route carried no key check; there is then no principal, and
            letting it through silently would be the wrong outcome.
    """
    logger = logging.getLogger(__name__)
    principal = getattr(request.state, "authz_principal", None)
    project = get_project_store().get(project_name)
    if principal is None or project is None:
        raise RuntimeError(f"'{action.value}' getoetst op een verzoek zonder herkend bewijsmiddel")
    target = (
        build_resource(
            project,
            project_name=project_name,
            path=resource_path(project_name, deployment),
            deployment=deployment,
        )
        if deployment
        else None
    )
    try:
        decide_action(principal, project=project, action=action, resource=target, request=request)
    except AccessDeniedError as exc:
        logger.warning("Geweigerd voor %s op %s (%s): %s", principal.actor, project_name, action.value, exc)
        raise HTTPException(status_code=403, detail={"cause": exc.cause, "message": str(exc)}) from exc


def deployment_in_body(deployment_data: Any = None, **_: Any) -> str | None:
    """The deployment an upsert body points at, for :func:`deployment_from`.

    The name sits there in the field ``deploymentName``; without this declaration the upsert
    route would weigh against the project's highest marking, and a pipeline with
    ``developer-ci`` could do nothing as soon as the project has production somewhere.
    """
    name = getattr(deployment_data, "deploymentName", None)
    return str(name) if name else None


def marking_in_body(deployment_data: Any = None, **_: Any) -> str:
    """The marking a deployment created by this upsert gets: the body's, or the default.

    Creating one on production still needs ``environment.set``, weighed separately.
    """
    return str(getattr(deployment_data, "environment", None) or DEFAULT_ENVIRONMENT)


def default_marking(**_: Any) -> str:
    """The marking of a deployment without one, for a delete of a deployment that is gone.

    Deleting nothing touches nothing, so the route may answer with its own 404 instead of a
    403 that breaks ``zad deployment delete --ignore-not-found``.
    """
    return DEFAULT_ENVIRONMENT.value


def image_only_upsert(project_name: str = "", deployment_data: Any = None, **_: Any) -> Action | None:
    """``deployment.image-replace`` for an upsert that only replaces images, else None.

    On an existing deployment the upsert updates the images of the components it already
    runs and adds any others; it never removes one. With only known components, no clone,
    the same marking and no domain change, nothing but images moves, which is what
    ``ops-ci`` may do on every deployment. The deploy action always sends an upsert, so
    without this ``ops-ci`` could not deploy to production through it.
    """
    if deployment_data is None:
        return None
    target = _deployment_named(project_name, deployment_data.deploymentName)
    if target is None or deployment_data.cloneFrom or deployment_data.forceClone:
        return None
    if deployment_data.environment is not None and Omgeving(deployment_data.environment) is not effective_environment(
        target
    ):
        return None
    if _changes_domain(target, deployment_data):
        return None
    running = {c.get("reference") for c in target.get("components") or [] if isinstance(c, dict)}
    if any(c.reference not in running for c in deployment_data.components):
        return None
    return Action.DEPLOYMENT_IMAGE_REPLACE


def require_upsert_body_targets(request: Request, project_name: str, deployment_data: Any) -> None:
    """Check the targets in an upsert body that the route gate does not see.

    The gate weighs ``deploymentName``; in the same body three fields point at something
    else (RC-392 security review). ``cloneFrom`` copies the database of the SOURCE here, so
    that source is weighed separately: cloning from production is ops, whatever the target.
    ``forceClone`` WITHOUT ``cloneFrom`` lets the clone run that is already on the
    deployment, because the skip on "mode once and finished" gives way for force_clone
    (``opi/manager/database_manager.py``); the source then sits in the project file and
    weighs there. The domain trio moves the hostname of an EXISTING deployment, which
    through the per-project approval can be the same as production's; a new deployment gets
    its domain fields out of ``deployment.write``. A field that repeats the stored value
    moves nothing; otherwise every repeated pipeline deploy that sends ``subdomain`` gets a
    403.

    Raises:
        HTTPException: 403 when the credential does not open one of these actions.
    """
    target = _deployment_named(project_name, deployment_data.deploymentName)
    stored_clone = _stored_clone(target)
    if deployment_data.cloneFrom:
        require_api_action(request, project_name, Action.DEPLOYMENT_CLONE_FROM, deployment=deployment_data.cloneFrom)
    elif deployment_data.forceClone and stored_clone is not None:
        require_api_action(
            request, project_name, Action.DEPLOYMENT_CLONE_FROM, deployment=_source_deployment(stored_clone)
        )
    if target is not None and _changes_domain(target, deployment_data):
        require_api_action(request, project_name, Action.DEPLOYMENT_DOMAIN_SET)


def _changes_domain(deployment: dict[str, Any], deployment_data: Any) -> bool:
    """Whether the body sends a domain field that differs from what the deployment stores."""
    return any(
        value is not None and value != get_domain_setting(deployment, setting)
        for setting, value in (
            (DomainSetting.DOMAIN_FORMAT, deployment_data.domain_format),
            (DomainSetting.SUBDOMAIN, deployment_data.subdomain),
            (DomainSetting.BASE_DOMAIN, deployment_data.base_domain),
        )
    )


def _deployment_named(project_name: str, deployment_name: str) -> dict[str, Any] | None:
    """The deployment with this name in this project, or None."""
    project = get_project_store().get(project_name)
    if project is None:
        return None
    return find_deployment(project.data or {}, deployment_name)


def _stored_clone(deployment: dict[str, Any] | None) -> dict[str, Any] | None:
    """The ``clone-from`` block that is already on this deployment."""
    clone = (deployment or {}).get("clone-from")
    return clone if isinstance(clone, dict) else None


def _source_deployment(clone: dict[str, Any]) -> str | None:
    """The deployment this block clones from, or None when the source is not one.

    A backup or an external source is not a deployment of this project; without a target the
    check weighs against the project's highest marking, and so more strictly.
    """
    if clone.get("type") != CloneFromType.DEPLOYMENT.value:
        return None
    reference = clone.get("reference")
    return str(reference) if reference else None


def parse_ports(ports_str: str) -> list[int]:
    """
    Parse comma-separated ports string into list of integers.

    Args:
        ports_str: Comma-separated port numbers

    Returns:
        List of port numbers as integers
    """
    if not ports_str:
        return []
    try:
        return [int(port.strip()) for port in ports_str.split(",") if port.strip()]
    except ValueError:
        return []


def _require_shared_secret(
    request: Request,
    presented: str | None,
    configured: str | None,
    setting_name: str,
    route_name: str,
    missing_detail: str,
) -> None:
    """Let the call through only when ``presented`` is exactly the configured secret.

    A secret that is not configured keeps the route closed, never open. The answer is then a
    404: that this instance does not have the facility is no server error.
    """
    logger = logging.getLogger(__name__)

    if not presented:
        logger.warning(f"Authentication failed for route {route_name} ({setting_name}) - no credential provided")
        raise HTTPException(status_code=401, detail=missing_detail)

    refuse_when_key_failures_exhausted(presented)

    if not configured:
        logger.warning(f"{setting_name} not configured - route {route_name} is disabled")
        raise HTTPException(status_code=404, detail="Not Found")

    # Compared on bytes: compare_digest on two str refuses a non-ASCII character with a
    # TypeError, and the header comes from outside with arbitrary bytes.
    if not secrets.compare_digest(presented.encode(), configured.encode()):
        logger.warning(f"Authentication failed for route {route_name} ({setting_name}) - invalid credential")
        refuse_after_auth_failure(request, presented)
        raise HTTPException(status_code=401, detail="Invalid API key")

    logger.debug(f"{setting_name} validation successful for route {route_name}")


def validate_metrics_scrape_token(func: Callable[..., Any]) -> Callable[..., Any]:
    """Require ``Authorization: Bearer <PROMETHEUS_METRICS_AUTH_TOKEN>`` on a metrics route.

    That is the header our Prometheus already sends, with the token the metrics-scraper
    service also gives to project apps.
    """

    @wraps(func)
    async def wrapper(*args: Any, request: Request, **kwargs: Any) -> Any:
        try:
            token: str | None = extract_bearer_token(request)
        except UserTokenError:
            token = None

        _require_shared_secret(
            request,
            token,
            settings.PROMETHEUS_METRICS_AUTH_TOKEN,
            "PROMETHEUS_METRICS_AUTH_TOKEN",
            func.__name__,
            "Authentication required - provide an Authorization: Bearer header",
        )
        return await func(*args, request=request, **kwargs)

    setattr(wrapper, REQUIRES_METRICS_SCRAPE_TOKEN, True)
    return wrapper


def check_route_actions(routes: Iterable[Any]) -> None:
    """Refuse to start when a GUARDED route names no action, or an action has no rule.

    Guarded means the route carries one of the recognitions (a session, a project key, a
    personal token or a platform admin token). A new route that gets such a decorator must
    therefore pick an action, and that action must have a rule in
    ``opi/core/authz/rules.py``. A route without an action would stand silently open for
    everything the caller could already do, and an action without a rule would fail on every
    call instead of at startup.

    The routers that do their key check by hand (``task_router``, sleep mode) carry no marker
    and are not found here; ``tests/test_actions.py`` records those five separately.

    Raises:
        RuntimeError: When a guarded route carries no action or the action has no rule.
    """
    guarded = ("_requires_sso", REQUIRES_PROJECT_NAME, REQUIRES_USER_TOKEN, REQUIRES_PLATFORM_ADMIN_TOKEN)
    without_action = []
    without_rule = []
    for route in routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None or not any(getattr(endpoint, marker, False) for marker in guarded):
            continue
        methods = ",".join(sorted(getattr(route, "methods", None) or []))
        requested = action_of(endpoint)
        if requested is None:
            without_action.append(f"{methods} {getattr(route, 'path', '?')}")
            continue
        try:
            rule_for(requested)
        except RuleError:
            without_rule.append(f"{methods} {getattr(route, 'path', '?')} ({requested.value})")

    if without_action:
        raise RuntimeError(
            "Deze routes zijn bewaakt maar noemen geen action: "
            f"{', '.join(sorted(without_action))}. Zet @action(Action.X) boven de authenticatiedecorator."
        )
    if without_rule:
        raise RuntimeError(
            f"Deze routes noemen een action zonder regel in opi/core/authz/rules.py: {', '.join(sorted(without_rule))}."
        )
