"""Editable definitions for the attachments service (component-level "uses" sequence).

The deployment-level attachment editables (``DEPLOYMENT_COMP_ATTACHMENT_USE_*``) are a
separate, independent definition set in ``opi.forms.editables.fields.deployments`` and
are not affected by this module.
"""

from __future__ import annotations

from opi.forms.editables.editable import Editable
from opi.forms.editables.validators import PathValidator

ATTACHMENT_USE_REFERENCE_EDITABLE = Editable(
    yaml_path="components[*]/services{attachments}/config[*]/reference",
    values_provider="AttachmentOptionsProvider",
    required=True,
)

ATTACHMENT_USE_PROVIDE_AS_EDITABLE = Editable(
    yaml_path="components[*]/services{attachments}/config[*]/provide-as",
    values_provider="AttachmentProvideAsOptionsProvider",
    required=True,
    default="file",
)

ATTACHMENT_USE_PATH_EDITABLE = Editable(
    yaml_path="components[*]/services{attachments}/config[*]/path",
    validator=PathValidator(),
    remove_when_none=True,
    depends_on="components[*]/services{attachments}/config[*]/provide-as",
    show_when={"value": ["file"]},
)

ATTACHMENT_USE_ENV_NAME_EDITABLE = Editable(
    yaml_path="components[*]/services{attachments}/config[*]/env-name",
    remove_when_none=True,
    depends_on="components[*]/services{attachments}/config[*]/provide-as",
    show_when={"value": ["env-var"]},
)

ATTACHMENT_USE_SEQUENCE_EDITABLE = Editable(
    yaml_path="components[*]/services{attachments}/config",
    depends_on="components[*]/services",
    show_when={"contains": "attachments"},
    virtualize=("services", "_services-config"),
    min_items=0,
    remove_when_none=True,
    children=[
        ATTACHMENT_USE_REFERENCE_EDITABLE,
        ATTACHMENT_USE_PROVIDE_AS_EDITABLE,
        ATTACHMENT_USE_PATH_EDITABLE,
        ATTACHMENT_USE_ENV_NAME_EDITABLE,
    ],
)
