"""Editable definitions for the redis service (project-level ACL setting).

``acl-key-prefix`` is the field that widens a project's Redis ACL beyond its own key
prefix; the model and the API carry it too.
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
