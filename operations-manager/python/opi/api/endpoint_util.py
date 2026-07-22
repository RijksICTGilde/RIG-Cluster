import logging
import re
import secrets
from collections.abc import Callable  # noqa: TC003 — used in decorator signatures at runtime
from functools import wraps
from typing import Any

from fastapi import HTTPException
from opi.core.config import settings
from opi.services.project_store import get_project_store
from starlette.requests import Request  # noqa: TC002 — FastAPI needs Request at runtime


def validate_api_token(func: Callable[..., Any]) -> Callable[..., Any]:
    """
    Decorator to validate API token for a route.

    This decorator requires project-specific API key via X-API-Key header.
    ALWAYS validates that the API key matches the project_name from the route.
    Returns 401 if project_name is missing from the route parameters.

    Args:
        func: The route function to decorate

    Returns:
        The decorated function that requires a valid API token and project_name
    """

    @wraps(func)
    async def wrapper(*args: Any, request: Request, **kwargs: Any) -> Any:
        logger = logging.getLogger(__name__)
        logger.debug(f"API route {func.__name__} called with authentication")

        x_api_key = request.headers.get("X-API-Key")
        if not x_api_key:
            logger.warning(f"Authentication failed for route {func.__name__} - no X-API-Key provided")
            raise HTTPException(status_code=401, detail="Authentication required - provide X-API-Key header")

        # ALWAYS require project_name parameter and validate it matches
        project_name_from_url = kwargs.get("project_name")

        if not project_name_from_url:
            logger.warning(f"Missing project_name parameter for route {func.__name__}")
            raise HTTPException(status_code=401, detail="Missing project_name parameter")

        project = get_project_store().get(project_name_from_url)

        if not project or not secrets.compare_digest(project.api_key, x_api_key):
            logger.warning(f"Authentication failed for route {func.__name__} - invalid API key")
            raise HTTPException(status_code=401, detail="Invalid API key")

        logger.debug(f"Project API key validation successful for route {func.__name__} (project: {project.name})")
        # Add project_id to kwargs so the route function can access it if needed
        kwargs["project_name"] = project.name
        return await func(*args, request=request, **kwargs)

    return wrapper


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


def normalize_project_name(text: str) -> str:
    return re.sub(r"[^a-z0-9_]", "", text.lower())


def validate_admin_api_key(func: Callable[..., Any]) -> Callable[..., Any]:
    """
    Decorator to validate admin API key for maintenance operations.

    This decorator requires the ADMIN_API_KEY via X-API-Key header.
    Used for admin operations like cleanup, reconciliation, and resource management.

    Args:
        func: The route function to decorate

    Returns:
        The decorated function that requires a valid admin API key
    """

    @wraps(func)
    async def wrapper(*args: Any, request: Request, **kwargs: Any) -> Any:
        logger = logging.getLogger(__name__)
        logger.debug(f"Admin API route {func.__name__} called with admin key authentication")

        x_api_key = request.headers.get("X-API-Key")
        if not x_api_key:
            logger.warning(f"Authentication failed for route {func.__name__} - no X-API-Key provided")
            raise HTTPException(status_code=401, detail="Authentication required - provide X-API-Key header")

        if not settings.ADMIN_API_KEY:
            logger.warning(f"Admin API key not configured - route {func.__name__} is disabled")
            raise HTTPException(
                status_code=501,
                detail="This endpoint requires ADMIN_API_KEY to be configured",
            )

        if not secrets.compare_digest(x_api_key, settings.ADMIN_API_KEY):
            logger.warning(f"Authentication failed for route {func.__name__} - invalid admin API key")
            raise HTTPException(status_code=401, detail="Invalid API key")

        logger.debug(f"Admin API key validation successful for route {func.__name__}")
        return await func(*args, request=request, **kwargs)

    return wrapper


def validate_master_api_key(func: Callable[..., Any]) -> Callable[..., Any]:
    """
    Decorator to validate master API key for admin operations.

    This decorator requires the MASTER_API_KEY via X-API-Key header.
    Used for operations that don't have a project context (e.g., namespace-based backups).

    Args:
        func: The route function to decorate

    Returns:
        The decorated function that requires a valid master API key
    """

    @wraps(func)
    async def wrapper(*args: Any, request: Request, **kwargs: Any) -> Any:
        logger = logging.getLogger(__name__)
        logger.debug(f"Admin API route {func.__name__} called with master key authentication")

        x_api_key = request.headers.get("X-API-Key")
        if not x_api_key:
            logger.warning(f"Authentication failed for route {func.__name__} - no X-API-Key provided")
            raise HTTPException(status_code=401, detail="Authentication required - provide X-API-Key header")

        if not settings.MASTER_API_KEY:
            logger.warning(f"Master API key not configured - route {func.__name__} is disabled")
            raise HTTPException(
                status_code=501,
                detail="This endpoint requires MASTER_API_KEY to be configured",
            )

        if not secrets.compare_digest(x_api_key, settings.MASTER_API_KEY):
            logger.warning(f"Authentication failed for route {func.__name__} - invalid master API key")
            raise HTTPException(status_code=401, detail="Invalid API key")

        logger.debug(f"Master API key validation successful for route {func.__name__}")
        return await func(*args, request=request, **kwargs)

    return wrapper
