"""Identity section editables: display name, description, clusters."""

from __future__ import annotations

from opi.core.config import settings
from opi.forms.editables.converters import ListSingleSelectConverter
from opi.forms.editables.editable import ProjectEditable
from opi.forms.editables.validators import MinMaxLengthValidator

DISPLAY_NAME = ProjectEditable(
    yaml_path="display-name",
    widget="text",
    label="Weergavenaam",
    description="Een beschrijvende naam voor uw project",
    required=True,
    validator=MinMaxLengthValidator(3, 100),
)

DESCRIPTION = ProjectEditable(
    yaml_path="description",
    widget="textarea",
    label="Projectomschrijving",
    description="Korte beschrijving van het doel en de scope van het project",
    required=True,
)

CLUSTERS = ProjectEditable(
    yaml_path="clusters",
    widget="select",
    label="Cluster",
    description="Selecteer het cluster waar dit project op draait",
    options_provider="ClusterOptionsProvider",
    required=True,
    default=[settings.CLUSTER_MANAGER],
    converter=ListSingleSelectConverter(),
)
