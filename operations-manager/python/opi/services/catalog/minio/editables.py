"""Editable definitions for the minio-storage service (project-level) -- RC-25.

``enable-versioning`` is the one real user setting on ``MinioStorageConfig``; the rest of
the model is OPI-written clone state on the deployment layer (see ``opi_managed_layers``
on the service). It was reachable through the API and the model but had no form field
anywhere, so a project could only get bucket versioning by hand-editing its file.
"""

from __future__ import annotations

from opi.forms.editables.converters import BooleanConverter
from opi.forms.editables.editable import SERVICE_VIRTUALIZE, Editable
from opi.services.catalog.base import ConfigLayer, config_path
from opi.services.services_enums import ServiceType

MINIO_ENABLE_VERSIONING_EDITABLE = Editable(
    yaml_path=config_path(ConfigLayer.PROJECT, ServiceType.MINIO_STORAGE, "config", "enable-versioning"),
    converter=BooleanConverter(),
    # The model default is None ("not set"), so an unticked box leaves no key rather than
    # writing enable-versioning: false into a project that never asked about it.
    remove_when_none=True,
    virtualize=SERVICE_VIRTUALIZE,
)
