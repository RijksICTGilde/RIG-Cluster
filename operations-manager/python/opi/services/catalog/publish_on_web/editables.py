"""Editable definitions for the publish-on-web service, per layer.

Component level: the TLS mode and the certificate attachment.

Deployment level (RC-60): the seven fields that decide the web address itself. They used
to live in ``forms/editables/fields/domains.py`` AND, with different providers and
validators, a second time in ``forms/editables/fields/deployments.py`` -- two definitions
for one path, in two flows, which is exactly the kind of duplication where converting one
leaves the other writing the old shape. There is one set now, it belongs to the service
that owns the values, and both flows use it.

Deployment-component level (RC-78): the same ``tls``/``attachment`` pair once more, as an
override for one component inside one deployment -- which is what the deployment-component
entry exists for. It is declared here, not in the forms layer, so the deployment form picks
it up through ``deployment_component_service_visualizers()`` without knowing this service
exists.

Every deployment-level path is built with ``config_path(ConfigLayer.DEPLOYMENT, ...)``
rather than written out, so the storage location is stated once (in
``catalog/base.py``) and this file cannot drift from ``domain_config.py``.
"""

from __future__ import annotations

import dataclasses

from opi.forms.editables.conditions import (
    DomainNeedsRequestCondition,
    SentinelValueCondition,
    SubdomainNeedsRequestCondition,
)
from opi.forms.editables.converters import CustomDomainSelectConverter
from opi.forms.editables.editable import SERVICE_VIRTUALIZE, Editable, FormState
from opi.forms.editables.enforcers import DomainConfigEnforcer
from opi.forms.editables.generators import IssuerGenerator
from opi.forms.editables.hooks import DomainRequestHook, SubdomainRequestHook
from opi.forms.editables.resolvers import ClusterDefaultDomain
from opi.forms.editables.validators import (
    BaseDomainValidator,
    CustomDomainValidator,
    DomainFormatValidator,
    SubdomainValidator,
)
from opi.services.catalog.base import ConfigLayer, config_path
from opi.services.catalog.publish_on_web.domain_config import DomainSetting, domain_setting_path
from opi.services.services_enums import ServiceType
from opi.utils.naming import ROOT_COMPONENT_FORMAT_IDS, SUBDOMAIN_FORMAT_IDS

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


# --- the deployment-component layer: the same pair, as an override (RC-78) ------------
#
# The model is the same one (``PublishOnWebComponentConfig``, twice): the entry exists
# precisely to override the component's. The fields are therefore DERIVED from the two
# above rather than written a second time -- a validator or a provider added there arrives
# here without anyone remembering to copy it.
#
# What a plain segment swap cannot do is the path. The component layer stores its services
# as the mixed string/dict LIST, so its path carries ``services{publish-on-web}`` plus
# ``virtualize``; the deployment component stores them as a real MAP, so the path is plain
# ``services/publish-on-web`` and virtualization would break it. The relocation goes
# through ``config_path`` for that reason, and the four remaining differences are the
# deliberate ones:
#
# * no ``default``: an empty value here means "follow the component", not "standard". A
#   default would silently write an override the moment a deployment form is saved.
# * ``remove_when_none``: emptying the field removes the override rather than storing an
#   empty string the cascade would then have to interpret.
# * a different provider: the override list leads with the inherit option, which names
#   what the component actually says (``PublishTlsOverrideOptionsProvider``).
# * no ``depends_on`` on the mode: "does this component use publish-on-web" is answered by
#   the component's own services list, which is not a field of this form. The layout
#   fieldset carries that condition instead.


def _as_deployment_component_override(editable: Editable, key: str, **overrides: object) -> Editable:
    """Relocate a component-level editable onto the deployment-component layer."""
    return dataclasses.replace(
        editable,
        yaml_path=config_path(ConfigLayer.DEPLOYMENT_COMPONENT, ServiceType.PUBLISH_ON_WEB, "config", key),
        virtualize=None,
        default=None,
        remove_when_none=True,
        **{"depends_on": None, "show_when": None, **overrides},  # type: ignore[arg-type]
    )


DEPLOYMENT_COMP_PUBLISH_TLS_EDITABLE = _as_deployment_component_override(
    PUBLISH_ON_WEB_TLS_EDITABLE,
    "tls",
    values_provider="PublishTlsOverrideOptionsProvider",
)

DEPLOYMENT_COMP_PUBLISH_ATTACHMENT_EDITABLE = _as_deployment_component_override(
    PUBLISH_ON_WEB_ATTACHMENT_EDITABLE,
    "attachment",
    # The mode it belongs to is a sibling on this same layer, so the dependency relocates
    # with it: 'provided' without a certificate is what the model refuses.
    depends_on=config_path(ConfigLayer.DEPLOYMENT_COMPONENT, ServiceType.PUBLISH_ON_WEB, "config", "tls"),
    show_when={"value": ["provided"]},
)

#: Both override fields, for ``config_editables(ConfigLayer.DEPLOYMENT_COMPONENT)``.
PUBLISH_ON_WEB_DEPLOYMENT_COMPONENT_EDITABLES = [
    DEPLOYMENT_COMP_PUBLISH_TLS_EDITABLE,
    DEPLOYMENT_COMP_PUBLISH_ATTACHMENT_EDITABLE,
]


# --- the deployment layer: the web address ------------------------------------------


def _path(setting: DomainSetting) -> str:
    return domain_setting_path(setting)


#: The custom-domain text input. Transient (never stored), so it stays a plain deployment
#: field: it is form transport, not configuration of the service.
CUSTOM_BASE_DOMAIN_PATH = "deployments[*]/base-domain:custom"

DOMAIN_FORMAT_EDITABLE = Editable(
    yaml_path=_path(DomainSetting.DOMAIN_FORMAT),
    required=True,
    default="component-deployment-project",
    values_provider="DomainFormatOptionsProvider",
    validator=DomainFormatValidator(),
    virtualize=SERVICE_VIRTUALIZE,
)

DOMAIN_SUBDOMAIN_EDITABLE = Editable(
    yaml_path=_path(DomainSetting.SUBDOMAIN),
    required=True,
    virtualize=SERVICE_VIRTUALIZE,
    depends_on=_path(DomainSetting.DOMAIN_FORMAT),
    show_when={"value": SUBDOMAIN_FORMAT_IDS},
    validator=SubdomainValidator(),
)

DOMAIN_BASE_DOMAIN_EDITABLE = Editable(
    yaml_path=_path(DomainSetting.BASE_DOMAIN),
    values_provider="ClusterBaseDomainOptionsProvider",
    validator=BaseDomainValidator(),
    converter=CustomDomainSelectConverter(),
    virtualize=SERVICE_VIRTUALIZE,
    defers_to=CUSTOM_BASE_DOMAIN_PATH,
    defer_when=SentinelValueCondition(),
    remove_when_none=True,
    transient_value_when_none=ClusterDefaultDomain(),
)

DOMAIN_CUSTOM_BASE_DOMAIN_EDITABLE = Editable(
    yaml_path=CUSTOM_BASE_DOMAIN_PATH,
    transient=True,
    depends_on=_path(DomainSetting.BASE_DOMAIN),
    show_when={"value": ["__custom__"]},
    validator=CustomDomainValidator(),
)

DOMAIN_ROOT_COMPONENT_EDITABLE = Editable(
    yaml_path=_path(DomainSetting.ROOT_COMPONENT),
    values_provider="RootComponentOptionsProvider",
    virtualize=SERVICE_VIRTUALIZE,
    depends_on=_path(DomainSetting.DOMAIN_FORMAT),
    show_when={"value": ROOT_COMPONENT_FORMAT_IDS},
    remove_when_none=True,
)

DOMAIN_BARE_DOMAIN_COMPONENT_EDITABLE = Editable(
    yaml_path=_path(DomainSetting.BARE_DOMAIN_COMPONENT),
    values_provider="BareDomainComponentOptionsProvider",
    virtualize=SERVICE_VIRTUALIZE,
    depends_on=_path(DomainSetting.BASE_DOMAIN),
    show_when={"value": ["__custom__"]},
    remove_when_none=True,
)

DOMAIN_ISSUER_EDITABLE = Editable(
    yaml_path=_path(DomainSetting.ISSUER),
    virtualize=SERVICE_VIRTUALIZE,
    depends_on=_path(DomainSetting.BASE_DOMAIN),
    generator=IssuerGenerator(),
    remove_when_none=True,
)

#: Legacy: superseded by ``domain-format`` and only read for files that predate it
#: (``HostnameFormat.from_domain_mode``). It moved along with the rest -- two places for one
#: subject is the fault this change removes -- but it gets no wizard field of its own.
DOMAIN_MODE_EDITABLE = Editable(
    yaml_path=_path(DomainSetting.DOMAIN_MODE),
    values_provider="DomainModeOptionsProvider",
    virtualize=SERVICE_VIRTUALIZE,
)

DOMAIN_REQUEST_DOMAIN_EDITABLE = Editable(
    yaml_path="deployments[*]/_request-domain",
    transient=True,
    required=True,
    show_when=DomainNeedsRequestCondition(),
    hooks={
        FormState.PRE_SAVE: DomainRequestHook(),
    },
)

DOMAIN_REQUEST_SUBDOMAIN_EDITABLE = Editable(
    yaml_path="deployments[*]/_request-subdomain",
    transient=True,
    required=True,
    show_when=SubdomainNeedsRequestCondition(),
    hooks={
        FormState.PRE_SAVE: SubdomainRequestHook(),
    },
)

#: The group the wizard step edits as one unit. Its enforcer validates the fields against
#: each other (a subdomain the format requires, a custom domain actually filled in, the
#: chosen subdomain still available) and raises at the field that is wrong, which is why the
#: group is an editable on the deployment itself rather than on the service config: the
#: transient request checkboxes live there too.
DOMAIN_CONFIG_EDITABLE = Editable(
    yaml_path="deployments[*]",
    enforcer=DomainConfigEnforcer(),
    children=[
        DOMAIN_FORMAT_EDITABLE,
        DOMAIN_BASE_DOMAIN_EDITABLE,
        DOMAIN_REQUEST_DOMAIN_EDITABLE,
        DOMAIN_CUSTOM_BASE_DOMAIN_EDITABLE,
        DOMAIN_SUBDOMAIN_EDITABLE,
        DOMAIN_REQUEST_SUBDOMAIN_EDITABLE,
        DOMAIN_ROOT_COMPONENT_EDITABLE,
        DOMAIN_BARE_DOMAIN_COMPONENT_EDITABLE,
        DOMAIN_ISSUER_EDITABLE,
    ],
)

#: Every deployment-level editable this service owns, in no particular display order --
#: ordering is the section's job. Consumed by ``config_editables(ConfigLayer.DEPLOYMENT)``
#: and, through the registry, by the deployment sequence form.
PUBLISH_ON_WEB_DEPLOYMENT_EDITABLES = [
    DOMAIN_FORMAT_EDITABLE,
    DOMAIN_BASE_DOMAIN_EDITABLE,
    DOMAIN_CUSTOM_BASE_DOMAIN_EDITABLE,
    DOMAIN_SUBDOMAIN_EDITABLE,
    DOMAIN_ROOT_COMPONENT_EDITABLE,
    DOMAIN_BARE_DOMAIN_COMPONENT_EDITABLE,
    DOMAIN_ISSUER_EDITABLE,
    DOMAIN_MODE_EDITABLE,
]
