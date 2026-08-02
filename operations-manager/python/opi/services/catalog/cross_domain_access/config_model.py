"""Typed config model for the ``cross-domain-access`` service (RC-15).

This service is about NETWORK access between projects, not DNS domains. "Domain" here
means the security perimeter of another project, not a hostname; publish-on-web and the
domain-approvals own the DNS side. The service lets a project declare, per named port,
which other projects/deployments/components may reach its pods (``inbound``) and where it
itself may connect (``outbound``). Each rule becomes a NetworkPolicy peer next to the
tenant baseline.

A rule always has two sides, ``from`` and ``to``. The peer is the side that carries a
``project``; which side that is follows from the list the rule sits in:

* ``inbound``  -- ``from`` is the peer, ``to`` is my side.
* ``outbound`` -- ``from`` is my side, ``to`` is the peer.

The port always sits on ``to``: an ingress rule filters on the receiving pod's port, an
egress rule on the destination's port. By putting the port structurally on ``to`` it can
never be filled on the wrong side.

Two validation levels, because the deployment layer is a *patch* on the project layer
(see ``merge.py``):

* The **stored** models (``*Patch``) are what a project file carries at BOTH the project
  and deployment layer. Everything except ``name`` is optional, so a deployment rule can
  set just ``to.deployment`` and inherit the rest, and a root rule may leave the peer
  deployment open. These are what ``validate_service_configs`` checks. ``config_model``
  points here.
* The **full** models (``PeerRef``/``PeerTarget``/``LocalRef``/``LocalTarget`` +
  ``InboundRule``/``OutboundRule``) describe a COMPLETE rule and are applied after the
  merge (in ``merge.py``). The only field that may still be open there is the peer
  ``deployment`` -- a rule left without one is skipped with a warning, not an error.

The typing does the shape validation for free: ``project`` on the own side and ``port`` on
the ``from`` side simply do not exist in the relevant model and are rejected by
``extra="forbid"``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: DNS-1123 label: the shape a project / deployment / component name has, mirroring the
#: patterns in ``project_v2.json``. Names are the identity we select pods and namespaces on.
DNS1123_LABEL = r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"

#: A rule name is a short DNS-1123 label; it is the key the deployment-layer patch merges on.
_NAME_MAX_LENGTH = 40


# --- full (post-merge) models: a COMPLETE rule ---------------------------------------


class PeerRef(BaseModel):
    """The peer side of an inbound rule: another project's deployment/component.

    ``deployment`` may be open on a root rule (the second layer fills it); every other
    field is required for a complete rule.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    project: str = Field(pattern=DNS1123_LABEL)
    deployment: str | None = Field(default=None, pattern=DNS1123_LABEL)
    component: str = Field(pattern=DNS1123_LABEL)


class PeerTarget(BaseModel):
    """The peer side of an outbound rule: a peer plus the destination port."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    project: str = Field(pattern=DNS1123_LABEL)
    deployment: str | None = Field(default=None, pattern=DNS1123_LABEL)
    component: str = Field(pattern=DNS1123_LABEL)
    port: int = Field(ge=1, le=65535)


class LocalRef(BaseModel):
    """My side of an outbound rule: only a component (the deployment follows from where
    the rule applies, and the project is myself)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    component: str = Field(pattern=DNS1123_LABEL)


class LocalTarget(BaseModel):
    """My side of an inbound rule: my component plus the port it is reached on."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    component: str = Field(pattern=DNS1123_LABEL)
    port: int = Field(ge=1, le=65535)


class InboundRule(BaseModel):
    """A complete inbound rule: the peer (``from``) may reach my component (``to``)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str = Field(pattern=DNS1123_LABEL, max_length=_NAME_MAX_LENGTH)
    from_: PeerRef = Field(alias="from")
    to: LocalTarget
    disabled: bool = False


class OutboundRule(BaseModel):
    """A complete outbound rule: my component (``from``) may reach the peer (``to``)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str = Field(pattern=DNS1123_LABEL, max_length=_NAME_MAX_LENGTH)
    from_: LocalRef = Field(alias="from")
    to: PeerTarget
    disabled: bool = False


# --- stored (patch) models: everything but ``name`` optional --------------------------


class PeerRefPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    project: str | None = Field(default=None, pattern=DNS1123_LABEL)
    deployment: str | None = Field(default=None, pattern=DNS1123_LABEL)
    component: str | None = Field(default=None, pattern=DNS1123_LABEL)


class PeerTargetPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    project: str | None = Field(default=None, pattern=DNS1123_LABEL)
    deployment: str | None = Field(default=None, pattern=DNS1123_LABEL)
    component: str | None = Field(default=None, pattern=DNS1123_LABEL)
    port: int | None = Field(default=None, ge=1, le=65535)


class LocalRefPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    component: str | None = Field(default=None, pattern=DNS1123_LABEL)


class LocalTargetPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    component: str | None = Field(default=None, pattern=DNS1123_LABEL)
    port: int | None = Field(default=None, ge=1, le=65535)


class InboundRulePatch(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str = Field(pattern=DNS1123_LABEL, max_length=_NAME_MAX_LENGTH)
    from_: PeerRefPatch | None = Field(default=None, alias="from")
    to: LocalTargetPatch | None = None
    disabled: bool = False


class OutboundRulePatch(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str = Field(pattern=DNS1123_LABEL, max_length=_NAME_MAX_LENGTH)
    from_: LocalRefPatch | None = Field(default=None, alias="from")
    to: PeerTargetPatch | None = None
    disabled: bool = False


class CrossDomainAccessConfig(BaseModel):
    """Project- or deployment-level cross-domain-access config: two lists of named rules.

    This is the STORED shape (patch models), because a root rule may leave the peer
    deployment open and a deployment rule is a partial patch keyed on ``name``. The
    completeness of a rule is judged after the merge, not here.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    inbound: list[InboundRulePatch] = Field(default_factory=list)
    outbound: list[OutboundRulePatch] = Field(default_factory=list)

    @model_validator(mode="after")
    def _names_unique_per_direction(self) -> CrossDomainAccessConfig:
        """A ``name`` is the merge key, so it must be unique within a direction within a
        layer; a duplicate is a mistake, not a silent last-wins."""
        for direction, rules in (("inbound", self.inbound), ("outbound", self.outbound)):
            seen: set[str] = set()
            for rule in rules:
                if rule.name in seen:
                    raise ValueError(f"dubbele regelnaam '{rule.name}' in {direction}")
                seen.add(rule.name)
        return self
