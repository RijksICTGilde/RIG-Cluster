"""Editable definitions for the cross-domain-access project-level form (RC-15).

Two sequences, ``inbound`` and ``outbound``. Every field maps DIRECTLY to its nested
``from``/``to`` storage path, exactly like the per-component ``publish-on-web/config/tls``
select does inside the deployment-components sequence -- so create AND edit read/write/
prefill naturally, with no transient split step.

The peer side (the side carrying ``project``) uses three plain fields rather than one
composite select: the framework cannot cascade per-row dependent selects, and a composite
select would have no natural edit-prefill path back from the stored ``from``/``to`` object.
Three direct fields avoid both problems and keep the stored form (nested ``from``/``to``,
2.3) the single source of truth. The peer ``project`` is a select fed from the authorized
projects; ``deployment`` and ``component`` are free text (a component name cannot be
cascaded from the chosen project without per-row dependent options). The own side and the
port are selects fed from the project's own components / ports.
"""

from __future__ import annotations

from opi.forms.editables.converters import IntegerConverter
from opi.forms.editables.editable import Editable
from opi.forms.editables.validators import KubernetesNameValidator, RangeValidator
from opi.services.catalog.base import ConfigLayer, config_path
from opi.services.services_enums import ServiceType

_VIRTUALIZE = ("services", "_services-config")


def _cp(*segments: str) -> str:
    return config_path(ConfigLayer.PROJECT, ServiceType.CROSS_DOMAIN_ACCESS, "config", *segments)


def _name(direction: str) -> Editable:
    return Editable(
        yaml_path=_cp(f"{direction}[*]", "name"),
        validator=KubernetesNameValidator("Regelnaam"),
        required=True,
        virtualize=_VIRTUALIZE,
    )


def _peer_project(direction: str, side: str) -> Editable:
    return Editable(
        yaml_path=_cp(f"{direction}[*]", side, "project"),
        values_provider="CrossDomainProjectOptionsProvider",
        required=True,
        virtualize=_VIRTUALIZE,
    )


def _peer_deployment(direction: str, side: str) -> Editable:
    # Optional at rest: a root rule may leave the peer deployment open for the deployment
    # layer to fill (2.3). remove_when_none so an empty field leaves no key.
    return Editable(
        yaml_path=_cp(f"{direction}[*]", side, "deployment"),
        validator=KubernetesNameValidator("Deployment"),
        remove_when_none=True,
        virtualize=_VIRTUALIZE,
    )


def _peer_component(direction: str, side: str) -> Editable:
    return Editable(
        yaml_path=_cp(f"{direction}[*]", side, "component"),
        validator=KubernetesNameValidator("Component"),
        required=True,
        virtualize=_VIRTUALIZE,
    )


def _local_component(direction: str, side: str) -> Editable:
    return Editable(
        yaml_path=_cp(f"{direction}[*]", side, "component"),
        values_provider="CrossDomainLocalComponentOptionsProvider",
        required=True,
        virtualize=_VIRTUALIZE,
    )


def _port(direction: str) -> Editable:
    # The port always lives on the ``to`` side (the receiving pod), for both directions.
    return Editable(
        yaml_path=_cp(f"{direction}[*]", "to", "port"),
        values_provider="CrossDomainPortOptionsProvider",
        converter=IntegerConverter(),
        validator=RangeValidator(1, 65535),
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
