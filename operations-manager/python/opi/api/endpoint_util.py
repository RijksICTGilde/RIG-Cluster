import logging
import re
from collections.abc import Callable
from functools import wraps
from typing import Any

from fastapi import HTTPException
from opi.services.project_service import get_project_service
from starlette.requests import Request


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

        project_service = get_project_service()
        project = project_service.get_project(project_name_from_url)

        if not project or project.api_key != x_api_key:
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
