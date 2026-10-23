"""Editable van ``seaweedfs-storage`` (projectlaag).

Eigen Editable en niet die van minio hergebruikt: een ``Editable`` is zijn ``yaml_path``,
en dat pad noemt de dienst. Hetzelfde object zou in beide diensten naar de minio-sleutel
schrijven.
"""

from __future__ import annotations

from opi.forms.editables.converters import BooleanConverter
from opi.forms.editables.editable import SERVICE_VIRTUALIZE, Editable
from opi.services.catalog.base import ConfigLayer, config_path
from opi.services.services_enums import ServiceType

SEAWEEDFS_ENABLE_VERSIONING_EDITABLE = Editable(
    yaml_path=config_path(ConfigLayer.PROJECT, ServiceType.SEAWEEDFS_STORAGE, "config", "enable-versioning"),
    converter=BooleanConverter(),
    # Zoals bij minio: de modelstandaard is None ("niet gekozen"), dus een leeg vinkje
    # laat geen sleutel achter in een project dat er nooit om gevraagd heeft.
    remove_when_none=True,
    virtualize=SERVICE_VIRTUALIZE,
)
