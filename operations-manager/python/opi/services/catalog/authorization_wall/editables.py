"""Editable definitions for the authorization-wall service (project-level banner)."""

from __future__ import annotations

from opi.forms.editables.editable import Editable
from opi.services.catalog.base import ConfigLayer, config_path
from opi.services.services_enums import ServiceType

AUTH_WALL_BANNER_EDITABLE = Editable(
    yaml_path=config_path(ConfigLayer.PROJECT, ServiceType.AUTHORIZATION_WALL, "config", "banner"),
    virtualize=("services", "_services-config"),
)
