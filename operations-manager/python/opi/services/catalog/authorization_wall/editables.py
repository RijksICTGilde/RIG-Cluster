"""Editable definitions for the authorization-wall service (project-level banner)."""

from __future__ import annotations

from opi.forms.editables.converters import EmptyToNoneConverter
from opi.forms.editables.editable import SERVICE_VIRTUALIZE, Editable
from opi.services.catalog.base import ConfigLayer, config_path
from opi.services.services_enums import ServiceType

AUTH_WALL_BANNER_EDITABLE = Editable(
    yaml_path=config_path(ConfigLayer.PROJECT, ServiceType.AUTHORIZATION_WALL, "config", "banner"),
    # Optional free-text field: an empty submission leaves no key rather than writing
    # banner: "" or null (checklist 4). banner defaults to None in the model, not a bool,
    # so remove_when_none is safe here.
    converter=EmptyToNoneConverter(),
    remove_when_none=True,
    virtualize=SERVICE_VIRTUALIZE,
)
