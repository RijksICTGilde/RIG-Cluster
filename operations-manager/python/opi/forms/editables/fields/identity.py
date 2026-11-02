"""Identity section editables: display name, description, source-code links, clusters."""

from __future__ import annotations

from opi.core.config import settings
from opi.forms.editables.converters import ListSingleSelectConverter
from opi.forms.editables.editable import Editable
from opi.forms.editables.validators import MinMaxLengthValidator, ModelFieldValidator
from opi.forms.models.project_file import (
    SOURCE_CODE_MAX_LINKS,
    SOURCE_CODE_NAME_MAX_LENGTH,
    SOURCE_CODE_URL_MAX_LENGTH,
    SourceCodeLinkModel,
)

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

SOURCE_CODE_URL_EDITABLE = Editable(
    yaml_path="source-code[*]/url",
    validator=ModelFieldValidator(
        SourceCodeLinkModel,
        "url",
        f"Moet een https-link zijn zonder spaties en maximaal {SOURCE_CODE_URL_MAX_LENGTH} tekens lang",
    ),
    required=True,
)

SOURCE_CODE_NAME_EDITABLE = Editable(
    yaml_path="source-code[*]/name",
    validator=ModelFieldValidator(
        SourceCodeLinkModel, "name", f"Mag maximaal {SOURCE_CODE_NAME_MAX_LENGTH} tekens bevatten"
    ),
    remove_when_none=True,
)

#: Informatieve links naar de broncode. Een project zonder links draagt het veld niet, dus
#: een lege lijst haalt het weg in plaats van ``source-code: []`` te schrijven.
SOURCE_CODE_EDITABLE = Editable(
    yaml_path="source-code",
    children=[SOURCE_CODE_URL_EDITABLE, SOURCE_CODE_NAME_EDITABLE],
    max_items=SOURCE_CODE_MAX_LINKS,
    remove_when_none=True,
)

CLUSTERS_EDITABLE = Editable(
    yaml_path="clusters",
    converter=ListSingleSelectConverter(),
    values_provider="ClusterOptionsProvider",
    required=True,
    default=[settings.CLUSTER_MANAGER],
)
