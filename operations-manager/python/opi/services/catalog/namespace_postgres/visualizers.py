"""Visualizers for the namespace-postgresql-database service (project-level instances,
storage and registry)."""

from __future__ import annotations

from opi.forms.editables.editable import WidgetType
from opi.forms.visualizers.visualizer import EditableVisualizer
from opi.services.catalog.namespace_postgres.editables import (
    POSTGRESQL_INSTANCES_EDITABLE,
    POSTGRESQL_REGISTRY_EDITABLE,
    POSTGRESQL_STORAGE_EDITABLE,
)

POSTGRESQL_INSTANCES = EditableVisualizer(
    editable=POSTGRESQL_INSTANCES_EDITABLE,
    widget=WidgetType.NUMBER,
    label="Aantal instanties",
)

POSTGRESQL_STORAGE = EditableVisualizer(
    editable=POSTGRESQL_STORAGE_EDITABLE,
    widget=WidgetType.SELECT,
    label="Opslaggrootte",
)

POSTGRESQL_REGISTRY = EditableVisualizer(
    editable=POSTGRESQL_REGISTRY_EDITABLE,
    widget=WidgetType.SELECT,
    label="Registry",
    help_text=(
        "Waar het database-image vandaan komt. Kies je een eigen registry, dan haalt de database "
        "het image daarachter op met het token daarvan. Voeg een registry toe bij de dienst Eigen "
        "container registries."
    ),
)
