"""Editable definitions for the cross-domain-access project-level form (RC-15).

Two sequences, ``inbound`` and ``outbound``. Every field maps DIRECTLY to its nested
``from``/``to`` storage path, exactly like the per-component ``publish-on-web/config/tls``
select does inside the deployment-components sequence -- so create AND edit read/write/
prefill naturally, with no transient split step.

The peer side (the side carrying ``project``) uses three plain fields rather than one
composite select: a composite select would have no natural edit-prefill path back from the
stored ``from``/``to`` object. Three direct fields keep the stored form (nested
``from``/``to``, 2.3) the single source of truth, and they cascade: ``project`` is a select
fed from the authorized projects, ``deployment`` is fed from the project chosen in the SAME
row, ``component`` from that deployment (RC-42, via the renderer's per-row ``row_data``).
The own side is a select on the project's own components, and the port is a select on the
RECEIVING side of the rule -- mine for inbound, the peer's for outbound.
"""

from __future__ import annotations

from opi.forms.editables.converters import IntegerConverter
from opi.forms.editables.editable import Editable
from opi.forms.editables.validators import ModelFieldValidator
from opi.services.catalog.base import ConfigLayer, config_path
from opi.services.catalog.cross_domain_access.config_model import (
    InboundRulePatch,
    LocalTargetPatch,
    PeerRefPatch,
)
from opi.services.services_enums import ServiceType

_VIRTUALIZE = ("services", "_services-config")

# The rules themselves live on the config model -- the same model the API endpoints for both
# layers take as their body and the stored project file is validated against -- so the form
# points at them instead of restating them. It used to restate them as KubernetesNameValidator,
# which additionally demands a leading LETTER: a peer project whose name starts with a digit
# was offered in the select, accepted by the schema and the API, and refused by the form.
_LABEL_MESSAGE = (
    "mag alleen kleine letters, cijfers en streepjes bevatten, moet beginnen en eindigen met een letter of cijfer"
)


def _label(model: type, field: str, what: str) -> ModelFieldValidator:
    return ModelFieldValidator(model, field, f"{what} {_LABEL_MESSAGE}")


def _cp(*segments: str) -> str:
    return config_path(ConfigLayer.PROJECT, ServiceType.CROSS_DOMAIN_ACCESS, "config", *segments)


def _name(direction: str) -> Editable:
    return Editable(
        yaml_path=_cp(f"{direction}[*]", "name"),
        validator=_label(InboundRulePatch, "name", "Regelnaam"),
        required=True,
        virtualize=_VIRTUALIZE,
    )


def _peer_project(direction: str, side: str) -> Editable:
    return Editable(
        yaml_path=_cp(f"{direction}[*]", side, "project"),
        values_provider="CrossDomainProjectOptionsProvider",
        validator=_label(PeerRefPatch, "project", "Project"),
        required=True,
        virtualize=_VIRTUALIZE,
    )


def _peer_deployment(direction: str, side: str) -> Editable:
    # Optional at rest: a root rule may leave the peer deployment open for the deployment
    # layer to fill (2.3). remove_when_none so an empty field leaves no key.
    return Editable(
        yaml_path=_cp(f"{direction}[*]", side, "deployment"),
        values_provider="CrossDomainPeerDeploymentOptionsProvider",
        validator=_label(PeerRefPatch, "deployment", "Deployment"),
        remove_when_none=True,
        virtualize=_VIRTUALIZE,
    )


def _peer_component(direction: str, side: str) -> Editable:
    return Editable(
        yaml_path=_cp(f"{direction}[*]", side, "component"),
        values_provider="CrossDomainPeerComponentOptionsProvider",
        validator=_label(PeerRefPatch, "component", "Component"),
        required=True,
        virtualize=_VIRTUALIZE,
    )


def _local_component(direction: str, side: str) -> Editable:
    return Editable(
        yaml_path=_cp(f"{direction}[*]", side, "component"),
        values_provider="CrossDomainLocalComponentOptionsProvider",
        validator=_label(LocalTargetPatch, "component", "Component"),
        required=True,
        virtualize=_VIRTUALIZE,
    )


def _port(direction: str) -> Editable:
    # The port always lives on the ``to`` side (the receiving pod), for both directions.
    return Editable(
        yaml_path=_cp(f"{direction}[*]", "to", "port"),
        values_provider="CrossDomainPortOptionsProvider",
        converter=IntegerConverter(),
        validator=ModelFieldValidator(LocalTargetPatch, "port", "Poort moet tussen 1 en 65535 liggen"),
        required=True,
        virtualize=_VIRTUALIZE,
    )


# inbound: peer is ``from``, own side is ``to`` (which also carries the port).
INBOUND_NAME_EDITABLE = _name("inbound")
INBOUND_PEER_PROJECT_EDITABLE = _peer_project("inbound", "from")
INBOUND_PEER_DEPLOYMENT_EDITABLE = _peer_deployment("inbound", "from")
INBOUND_PEER_COMPONENT_EDITABLE = _peer_component("inbound", "from")
INBOUND_LOCAL_COMPONENT_EDITABLE = _local_component("inbound", "to")
INBOUND_PORT_EDITABLE = _port("inbound")

INBOUND_SEQUENCE_EDITABLE = Editable(
    yaml_path=_cp("inbound"),
    depends_on="services",
    show_when={"contains": ServiceType.CROSS_DOMAIN_ACCESS.value},
    virtualize=_VIRTUALIZE,
    children=[
        INBOUND_NAME_EDITABLE,
        INBOUND_PEER_PROJECT_EDITABLE,
        INBOUND_PEER_DEPLOYMENT_EDITABLE,
        INBOUND_PEER_COMPONENT_EDITABLE,
        INBOUND_LOCAL_COMPONENT_EDITABLE,
        INBOUND_PORT_EDITABLE,
    ],
)

# outbound: own side is ``from``, peer is ``to`` (which also carries the port).
OUTBOUND_NAME_EDITABLE = _name("outbound")
OUTBOUND_LOCAL_COMPONENT_EDITABLE = _local_component("outbound", "from")
OUTBOUND_PEER_PROJECT_EDITABLE = _peer_project("outbound", "to")
OUTBOUND_PEER_DEPLOYMENT_EDITABLE = _peer_deployment("outbound", "to")
OUTBOUND_PEER_COMPONENT_EDITABLE = _peer_component("outbound", "to")
OUTBOUND_PORT_EDITABLE = _port("outbound")

OUTBOUND_SEQUENCE_EDITABLE = Editable(
    yaml_path=_cp("outbound"),
    depends_on="services",
    show_when={"contains": ServiceType.CROSS_DOMAIN_ACCESS.value},
    virtualize=_VIRTUALIZE,
    children=[
        OUTBOUND_NAME_EDITABLE,
        OUTBOUND_LOCAL_COMPONENT_EDITABLE,
        OUTBOUND_PEER_PROJECT_EDITABLE,
        OUTBOUND_PEER_DEPLOYMENT_EDITABLE,
        OUTBOUND_PEER_COMPONENT_EDITABLE,
        OUTBOUND_PORT_EDITABLE,
    ],
)

CROSS_DOMAIN_EDITABLES = [INBOUND_SEQUENCE_EDITABLE, OUTBOUND_SEQUENCE_EDITABLE]


# --- the deployment layer: a PATCH, not a second rule editor -----------------------------
# Two fields only, and that is the point. The deployment layer overrides a project rule keyed
# on its name (merge.py); the field it exists for is the peer deployment, which a project rule
# may deliberately leave open. Repeating the whole rule here would not be a patch but a second
# truth, and the two would have to be kept in step by hand.
#
# The peer deployment is NOT required at either layer: open on the project rule is a valid,
# intended state, and a patch that only wants to disable a rule leaves it open too.


def _dp(*segments: str) -> str:
    return config_path(ConfigLayer.DEPLOYMENT, ServiceType.CROSS_DOMAIN_ACCESS, "config", *segments)


def _patch_name(direction: str) -> Editable:
    return Editable(
        yaml_path=_dp(f"{direction}[*]", "name"),
        values_provider="CrossDomainRuleNameOptionsProvider",
        validator=_label(InboundRulePatch, "name", "Regelnaam"),
        required=True,
    )


def _patch_peer_deployment(direction: str, side: str) -> Editable:
    return Editable(
        yaml_path=_dp(f"{direction}[*]", side, "deployment"),
        values_provider="CrossDomainPeerDeploymentOptionsProvider",
        validator=_label(PeerRefPatch, "deployment", "Deployment"),
        remove_when_none=True,
    )


DEPLOYMENT_INBOUND_NAME_EDITABLE = _patch_name("inbound")
DEPLOYMENT_INBOUND_PEER_DEPLOYMENT_EDITABLE = _patch_peer_deployment("inbound", "from")
DEPLOYMENT_INBOUND_SEQUENCE_EDITABLE = Editable(
    yaml_path=_dp("inbound"),
    children=[DEPLOYMENT_INBOUND_NAME_EDITABLE, DEPLOYMENT_INBOUND_PEER_DEPLOYMENT_EDITABLE],
)

DEPLOYMENT_OUTBOUND_NAME_EDITABLE = _patch_name("outbound")
DEPLOYMENT_OUTBOUND_PEER_DEPLOYMENT_EDITABLE = _patch_peer_deployment("outbound", "to")
DEPLOYMENT_OUTBOUND_SEQUENCE_EDITABLE = Editable(
    yaml_path=_dp("outbound"),
    children=[DEPLOYMENT_OUTBOUND_NAME_EDITABLE, DEPLOYMENT_OUTBOUND_PEER_DEPLOYMENT_EDITABLE],
)

CROSS_DOMAIN_DEPLOYMENT_EDITABLES = [DEPLOYMENT_INBOUND_SEQUENCE_EDITABLE, DEPLOYMENT_OUTBOUND_SEQUENCE_EDITABLE]
