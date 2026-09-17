"""Visualizers for the postgresql-database service (project-level config).

One EditableVisualizer per editable. The ``schemas`` sequence renders each extra schema
as a card with its postfix, description and a mark-for-deletion toggle. The help text
spells out both what the postfix becomes (the full schema name) and which variable it
produces, so a consumer does not have to guess the generated name.
"""

from __future__ import annotations

from opi.forms.editables.editable import WidgetType
from opi.forms.visualizers.config_setting_fields import setting_field
from opi.forms.visualizers.visualizer import EditableVisualizer
from opi.services.catalog.base import ConfigLayer
from opi.services.catalog.postgresql_database.connection_limit import CONNECTION_LIMIT
from opi.services.catalog.postgresql_database.editables import (
    SCHEMA_DESCRIPTION_EDITABLE,
    SCHEMA_MARKED_EDITABLE,
    SCHEMA_POSTFIX_EDITABLE,
    SCHEMAS_EDITABLE,
)
from opi.services.services_enums import ServiceType

SCHEMA_POSTFIX = EditableVisualizer(
    editable=SCHEMA_POSTFIX_EDITABLE,
    widget=WidgetType.TEXT,
    label="Postfix",
    help_text=(
        "Korte naam voor het schema. De volledige schemanaam wordt "
        "{project}_{deployment}_{postfix}, en er komt een variabele DATABASE_SCHEMA_{POSTFIX} "
        "(hoofdletters) beschikbaar in de pod. Alleen kleine letters, cijfers en underscores, "
        "beginnend met een letter."
    ),
)

SCHEMA_DESCRIPTION = EditableVisualizer(
    editable=SCHEMA_DESCRIPTION_EDITABLE,
    widget=WidgetType.TEXT,
    label="Omschrijving",
    help_text="Waar dit schema voor gebruikt wordt. Alleen ter documentatie.",
)

SCHEMA_MARKED = EditableVisualizer(
    editable=SCHEMA_MARKED_EDITABLE,
    widget=WidgetType.CHECKBOX,
    label="Markeer voor verwijdering",
    # Niet in de aanmaakwizard: daar ben je een schema aan het VERZINNEN, en de vraag of je
    # het beheer ervan wilt stoppen slaat dan nergens op - je laat het schema gewoon weg.
    # Het veld gaat over een schema dat er AL is, en dat is precies het geval bij bewerken.
    alleen_bij_bewerken=True,
    help_text=(
        "Aangevinkt stopt het beheer van dit schema en verbergt zijn variabele, maar het schema "
        "en zijn data blijven in de database staan. Verwijderen gebeurt nooit automatisch."
    ),
)

POSTGRESQL_SCHEMAS = EditableVisualizer(
    editable=SCHEMAS_EDITABLE,
    widget=WidgetType.SEQUENCE,
    label="Extra schema's",
    help_text=(
        "Naast het standaardschema kan een deployment extra schema's in dezelfde database hebben. "
        "Ze zijn project-breed: elke deployment krijgt dezelfde schema's in zijn eigen database."
    ),
    children=[SCHEMA_POSTFIX, SCHEMA_DESCRIPTION, SCHEMA_MARKED],
)

POSTGRESQL_SCHEMAS_VISUALIZERS = [POSTGRESQL_SCHEMAS]

CONNECTION_LIMIT_FIELD = setting_field(
    CONNECTION_LIMIT,
    ServiceType.POSTGRESQL_DATABASE,
    ConfigLayer.PROJECT,
    widget=WidgetType.SELECT,
    values_provider="ConnectionLimitOptionsProvider",
    inherits=True,
)

# No virtualize: the per-deployment modal targets one deployment, like cross-domain-access.
DEPLOYMENT_CONNECTION_LIMIT_FIELD = setting_field(
    CONNECTION_LIMIT,
    ServiceType.POSTGRESQL_DATABASE,
    ConfigLayer.DEPLOYMENT,
    widget=WidgetType.SELECT,
    values_provider="DeploymentConnectionLimitOptionsProvider",
    virtualize=None,
    inherits=True,
)
