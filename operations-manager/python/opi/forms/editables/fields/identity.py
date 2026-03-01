"""Identity section editables: display name, description, clusters."""

from __future__ import annotations

from opi.core.config import settings
from opi.forms.editables.converters import ListSingleSelectConverter
from opi.forms.editables.editable import Editable
from opi.forms.editables.validators import MinMaxLengthValidator

# --- Pure Editable definitions (data logic only) ---

DISPLAY_NAME_EDITABLE = Editable(
    yaml_path="display-name",
    validator=MinMaxLengthValidator(3, 100),
    required=True,
)

DESCRIPTION_EDITABLE = Editable(
    yaml_path="description",
    required=True,
)

CLUSTERS_EDITABLE = Editable(
    yaml_path="clusters",
    converter=ListSingleSelectConverter(),
    values_provider="ClusterOptionsProvider",
    required=True,
    default=[settings.CLUSTER_MANAGER],
)
