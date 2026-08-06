"""Editable definitions for the attachments service (component-level "uses" sequence).

The deployment-level attachment editables (``DEPLOYMENT_COMP_ATTACHMENT_USE_*``) are a
separate, independent definition set in ``opi.forms.editables.fields.deployments`` and
are not affected by this module.
"""

from __future__ import annotations

from opi.forms.editables.editable import Editable
from opi.forms.editables.validators import AttachmentIdValidator, EnvNameValidator, PathValidator

#: The identifier of a catalog entry -- the DEFINE side of the service.
#:
#: Its yaml_path is the catalog itself (``services{attachments}/data[*]/id``), which is
#: where the value ends up. There is no form field bound to that path: the upload section
#: is a template partial, and the API upload is multipart. It is an Editable all the same
#: because both roads into the system have to agree on what an id may look like, and the
#: only way to guarantee that is for them to run the same rule. The wizard upload endpoint
#: and the declared ``attachments`` API action both point here.
#:
#: Uniqueness is part of the validator but only fires when the caller supplies
#: ``existing_attachment_ids`` in the context. The wizard does (a duplicate is a typo to
#: correct in the field); the API does not, because there "the id already exists" is the
#: verb's business: it is a 409 for CREATE and the normal case for UPSERT.
ATTACHMENT_ID_EDITABLE = Editable(
    yaml_path="services{attachments}/data[*]/id",
    validator=AttachmentIdValidator(),
    required=True,
)

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
    validator=EnvNameValidator(),
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
