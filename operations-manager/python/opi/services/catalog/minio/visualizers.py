"""Visualizers for the minio-storage service: project level (RC-25) and the grens op oude
versies op beide lagen (RC-380)."""

from __future__ import annotations

from opi.forms.editables.editable import WidgetType
from opi.forms.visualizers.config_setting_fields import setting_field
from opi.forms.visualizers.visualizer import EditableVisualizer
from opi.services.catalog.base import ConfigLayer
from opi.services.catalog.minio.editables import MINIO_ENABLE_VERSIONING_EDITABLE
from opi.services.catalog.shared.noncurrent_versions import MAX_NONCURRENT_VERSIONS, NONCURRENT_EXPIRE_DAYS
from opi.services.services_enums import ServiceType

MINIO_ENABLE_VERSIONING = EditableVisualizer(
    editable=MINIO_ENABLE_VERSIONING_EDITABLE,
    widget=WidgetType.CHECKBOX,
    label="Versiebeheer op de bucket",
    description="Bewaar eerdere versies van objecten.",
    help_text=(
        "Aan: MinIO bewaart oudere versies van een object, zodat een overschrijving of "
        "verwijdering terug te draaien is. Dit kost extra opslag, evenredig met hoe vaak "
        "objecten wijzigen."
    ),
    # Live hertekenen: het blok "Oude versies" staat achter een Fieldset-poort op dit
    # vinkje; zonder dit attribuut wordt de toggle pas op de volgende stap zichtbaar.
    attributes={"data-rerender": "true"},
)


#: De grens op oude versies, op het project en per deployment. ``inherits``: een leeg veld
#: zegt niets, zodat de minder specifieke laag of de standaard geldt.
MAX_NONCURRENT_VERSIONS_FIELD = setting_field(
    MAX_NONCURRENT_VERSIONS,
    ServiceType.MINIO_STORAGE,
    ConfigLayer.PROJECT,
    widget=WidgetType.SELECT,
    values_provider="MaxNoncurrentVersionsOptionsProvider",
    inherits=True,
)

NONCURRENT_EXPIRE_DAYS_FIELD = setting_field(
    NONCURRENT_EXPIRE_DAYS,
    ServiceType.MINIO_STORAGE,
    ConfigLayer.PROJECT,
    widget=WidgetType.SELECT,
    values_provider="NoncurrentExpireDaysOptionsProvider",
    inherits=True,
)

# No virtualize: the per-deployment modal targets one deployment, like the connection limit.
DEPLOYMENT_MAX_NONCURRENT_VERSIONS_FIELD = setting_field(
    MAX_NONCURRENT_VERSIONS,
    ServiceType.MINIO_STORAGE,
    ConfigLayer.DEPLOYMENT,
    widget=WidgetType.SELECT,
    values_provider="MinioDeploymentMaxNoncurrentVersionsOptionsProvider",
    virtualize=None,
    inherits=True,
)

DEPLOYMENT_NONCURRENT_EXPIRE_DAYS_FIELD = setting_field(
    NONCURRENT_EXPIRE_DAYS,
    ServiceType.MINIO_STORAGE,
    ConfigLayer.DEPLOYMENT,
    widget=WidgetType.SELECT,
    values_provider="MinioDeploymentNoncurrentExpireDaysOptionsProvider",
    virtualize=None,
    inherits=True,
)
