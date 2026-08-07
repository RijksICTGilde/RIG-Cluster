"""Editable definitions for the redis service (project-level ACL setting) -- RC-25.

``acl-key-prefix`` has always been a real user setting on the model and reachable through
the API, but it had no form field anywhere, so the only way to widen a project's Redis ACL
was to hand-edit the project file. This is that field.
"""

from __future__ import annotations

from opi.forms.editables.converters import BooleanConverter
from opi.forms.editables.editable import SERVICE_VIRTUALIZE, Editable
from opi.services.catalog.base import ConfigLayer, config_path
from opi.services.services_enums import ServiceType

REDIS_ACL_KEY_PREFIX_EDITABLE = Editable(
    yaml_path=config_path(ConfigLayer.PROJECT, ServiceType.REDIS, "config", "acl-key-prefix"),
    converter=BooleanConverter(),
    default=True,
    virtualize=SERVICE_VIRTUALIZE,
)
