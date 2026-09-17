"""The connection limit of a deployment's database roles (RC-201). The bound is a
platform decision, so it lives here and not in the project file."""

from __future__ import annotations

from typing import Any, Final

from opi.services.catalog.base import ConfigLayer
from opi.services.catalog.config_settings import IntegerSetting, resolve_setting
from opi.services.services import service_entry_config, service_entry_name
from opi.services.services_enums import ServiceType

CONNECTION_LIMIT: Final = IntegerSetting(
    path="connection-limit",
    layers=(ConfigLayer.PROJECT, ConfigLayer.DEPLOYMENT),
    default=20,
    minimum=1,
    maximum=500,
    label="Connectielimiet",
)

#: The steps the wizard offers. A convenience, not a bound: any value inside
#: ``CONNECTION_LIMIT`` is valid, and a stored value outside this list is still shown.
CONNECTION_LIMIT_STEPS: Final[tuple[int, ...]] = (10, 20, 40, 50, 75, 100, 150, 200, 250, 500)


def _limit_in(entries: Any) -> Any:
    for entry in entries or []:
        if service_entry_name(entry) == ServiceType.POSTGRESQL_DATABASE.value:
            config = service_entry_config(entry)
            return config.get(CONNECTION_LIMIT.path) if isinstance(config, dict) else None
    return None


def project_connection_limit(project_data: dict[str, Any]) -> int:
    """What a deployment without a value of its own gets: the project's value, else the default."""
    return resolve_setting(CONNECTION_LIMIT, {ConfigLayer.PROJECT: _limit_in(project_data.get("services"))})


def deployment_connection_limit(project_data: dict[str, Any], deployment_name: str) -> int:
    """The effective limit for ``deployment_name``: deployment over project over the default."""
    deployment = next(
        (d for d in project_data.get("deployments") or [] if isinstance(d, dict) and d.get("name") == deployment_name),
        {},
    )
    return resolve_setting(
        CONNECTION_LIMIT,
        {
            ConfigLayer.PROJECT: _limit_in(project_data.get("services")),
            ConfigLayer.DEPLOYMENT: _limit_in(deployment.get("services")),
        },
    )
