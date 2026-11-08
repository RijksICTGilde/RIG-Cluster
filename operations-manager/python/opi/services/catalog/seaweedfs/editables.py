"""The editable of ``seaweedfs-storage`` (project layer).

An editable of its own rather than minio's reused: an ``Editable`` IS its ``yaml_path``, and
that path names the service. The same object would write to the minio key in both services.
"""

from __future__ import annotations

from opi.forms.editables.converters import BooleanConverter
from opi.forms.editables.editable import SERVICE_VIRTUALIZE, Editable
from opi.services.catalog.base import ConfigLayer, config_path
from opi.services.services_enums import ServiceType

SEAWEEDFS_ENABLE_VERSIONING_EDITABLE = Editable(
    yaml_path=config_path(ConfigLayer.PROJECT, ServiceType.SEAWEEDFS_STORAGE, "config", "enable-versioning"),
    converter=BooleanConverter(),
    # As with minio: the model default is None ("not chosen"), so an unticked box leaves no
    # key in a project that never asked about it.
    remove_when_none=True,
    virtualize=SERVICE_VIRTUALIZE,
)
