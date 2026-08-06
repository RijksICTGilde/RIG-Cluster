"""Editable definitions for the publish-on-web service (component-level TLS + attachment).

Only the per-component TLS mode + certificate attachment live here; the deployment-level
domain wizard and root ``domains:`` handling are platform-infra, not owned by this service.
"""

from __future__ import annotations

from opi.forms.editables.editable import SERVICE_VIRTUALIZE, Editable

PUBLISH_ON_WEB_TLS_EDITABLE = Editable(
    yaml_path="components[*]/services{publish-on-web}/config/tls",
    values_provider="PublishTlsModeOptionsProvider",
    default="standard",
    virtualize=SERVICE_VIRTUALIZE,
    depends_on="components[*]/services",
    show_when={"contains": "publish-on-web"},
)

PUBLISH_ON_WEB_ATTACHMENT_EDITABLE = Editable(
    yaml_path="components[*]/services{publish-on-web}/config/attachment",
    values_provider="AttachmentOptionsProvider",
    virtualize=SERVICE_VIRTUALIZE,
    remove_when_none=True,
    depends_on="components[*]/services{publish-on-web}/config/tls",
    show_when={"value": ["provided"]},
)
