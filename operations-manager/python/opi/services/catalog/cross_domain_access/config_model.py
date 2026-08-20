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

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: DNS-1123 label: the shape a project / deployment / component name has, mirroring the
#: patterns in ``project_v2.json``. Names are the identity we select pods and namespaces on.
DNS1123_LABEL = r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"

#: A rule name is a short DNS-1123 label; it is the key the deployment-layer patch merges on.
_NAME_MAX_LENGTH = 40


#: The peer project that means "no project limit" (RC-142).
#:
#: A normal inbound rule names ONE peer; the receiver decides per consumer. That is right
#: when the consumers are a short, known list. It is wrong for a shared facility that any
#: project may use, where the list is "whoever takes the service" and keeping it by hand
#: makes the facility's owner the gatekeeper of a self-service platform.
#:
#: ``from: {project: "*"}`` renders as an ingress entry with no ``from`` selector, on
#: exactly the named port: any source may reach that component there. It is a real
#: widening, and it is spelled out rather than being what an empty ``from`` happens to
#: mean -- a rule that lost its peer by accident would otherwise silently become open.
#: It stays narrow in the other directions: only this port, only this component, and the
#: caller still needs its own egress rule to get there at all. Authorizing what the caller
#: may then DO is the receiving application's own business (VLAM checks its API key).
WILDCARD_PROJECT = "*"

#: DNS-1123 label OR the wildcard. Only the peer of an INBOUND rule takes this; the
#: outbound peer keeps the plain label pattern, so an outbound rule can never open a way
#: out to "anything".
DNS1123_LABEL_OR_WILDCARD = r"^(\*|[a-z0-9]([-a-z0-9]*[a-z0-9])?)$"

_WILDCARD_DESCRIPTION = (
    " Use '*' for no project limit: the port is opened to every source instead of to one "
    "named peer, and 'deployment' and 'component' must then be left empty."
)


def _check_wildcard_peer(peer: Any) -> Any:
    """A wildcard peer names nothing else: no deployment, no component.

    Refused rather than ignored. A rule reading ``{project: '*', component: 'api'}`` looks
    like it is scoped to one component and is not -- silently dropping the component would
    render a policy that is wider than the rule says it is.
    """
    if peer.project != WILDCARD_PROJECT:
        return peer
    named = [field for field in ("deployment", "component") if getattr(peer, field, None)]
    if named:
        raise ValueError(
            f"peer-project '*' betekent geen projectlimiet; laat {' en '.join(named)} dan leeg "
            f"(nu gezet: {', '.join(named)})"
        )
    return peer


# --- full (post-merge) models: a COMPLETE rule ---------------------------------------


class PeerRef(BaseModel):
    """The peer side of an inbound rule: the peer deployment/component, in any project including this one.

    ``deployment`` may be open on a root rule (the second layer fills it); every other
    field is required for a complete rule -- unless ``project`` is the wildcard, which
    means there is no peer to name at all (see ``WILDCARD_PROJECT``).
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    project: str = Field(
        pattern=DNS1123_LABEL_OR_WILDCARD,
        description="Project of the peer, as a DNS-1123 label." + _WILDCARD_DESCRIPTION,
    )
    deployment: str | None = Field(
        default=None,
        pattern=DNS1123_LABEL,
        description="Deployment of the peer; may be left open on a project-level rule and filled in per deployment.",
    )
    component: str | None = Field(
        default=None,
        pattern=DNS1123_LABEL,
        description="Component of the peer, as a DNS-1123 label; empty only when 'project' is '*'.",
    )

    @model_validator(mode="after")
    def _wildcard_names_nothing_else(self) -> PeerRef:
        _check_wildcard_peer(self)
        if self.project != WILDCARD_PROJECT and not self.component:
            raise ValueError("peer-component is verplicht, behalve bij peer-project '*'")
        return self


class PeerTarget(BaseModel):
    """The peer side of an outbound rule: a peer plus the destination port."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    project: str = Field(pattern=DNS1123_LABEL, description="Project of the peer, as a DNS-1123 label.")
    deployment: str | None = Field(
        default=None,
        pattern=DNS1123_LABEL,
        description="Deployment of the peer; may be left open on a project-level rule and filled in per deployment.",
    )
    component: str = Field(pattern=DNS1123_LABEL, description="Component of the peer, as a DNS-1123 label.")
    port: int = Field(ge=1, le=65535, description="Port on the peer component that is reached.")


class LocalRef(BaseModel):
    """My side of an outbound rule: only a component (the deployment follows from where
    the rule applies, and the project is myself)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    component: str = Field(pattern=DNS1123_LABEL, description="My component the rule is about.")


class LocalTarget(BaseModel):
    """My side of an inbound rule: my component plus the port it is reached on."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    component: str = Field(pattern=DNS1123_LABEL, description="My component the peer reaches.")
    port: int = Field(ge=1, le=65535, description="Port on my component the peer reaches it on.")


class InboundRule(BaseModel):
    """A complete inbound rule: the peer (``from``) may reach my component (``to``).

    With ``from.project == "*"`` there is no peer at all and the port is opened to every
    source; see ``WILDCARD_PROJECT``.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str = Field(
        pattern=DNS1123_LABEL,
        max_length=_NAME_MAX_LENGTH,
        description="Name of this rule; it is the key a deployment-level rule patches on.",
    )
    from_: PeerRef = Field(alias="from", description="The peer that is allowed to reach me.")
    to: LocalTarget = Field(description="My component and port the peer is allowed to reach.")
    disabled: bool = Field(default=False, description="Keep the rule but do not apply it.")


class OutboundRule(BaseModel):
    """A complete outbound rule: my component (``from``) may reach the peer (``to``)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str = Field(
        pattern=DNS1123_LABEL,
        max_length=_NAME_MAX_LENGTH,
        description="Name of this rule; it is the key a deployment-level rule patches on.",
    )
    from_: LocalRef = Field(alias="from", description="My component that is allowed to reach out.")
    to: PeerTarget = Field(description="The peer component and port it may reach.")
    disabled: bool = Field(default=False, description="Keep the rule but do not apply it.")


# --- stored (patch) models: everything but ``name`` optional --------------------------


class PeerRefPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    project: str | None = Field(
        default=None,
        pattern=DNS1123_LABEL_OR_WILDCARD,
        description="Project of the peer, as a DNS-1123 label." + _WILDCARD_DESCRIPTION,
    )
    deployment: str | None = Field(
        default=None,
        pattern=DNS1123_LABEL,
        description="Deployment of the peer; may be left open on a project-level rule and filled in per deployment.",
    )
    component: str | None = Field(
        default=None, pattern=DNS1123_LABEL, description="Component of the peer, as a DNS-1123 label."
    )

    @model_validator(mode="after")
    def _wildcard_names_nothing_else(self) -> PeerRefPatch:
        """Refused at the STORED layer too: this combination is never on its way to
        becoming valid, whatever a deployment patch adds later."""
        return _check_wildcard_peer(self)


class PeerTargetPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    project: str | None = Field(
        default=None, pattern=DNS1123_LABEL, description="Project of the peer, as a DNS-1123 label."
    )
    deployment: str | None = Field(
        default=None,
        pattern=DNS1123_LABEL,
        description="Deployment of the peer; may be left open on a project-level rule and filled in per deployment.",
    )
    component: str | None = Field(
        default=None, pattern=DNS1123_LABEL, description="Component of the peer, as a DNS-1123 label."
    )
    port: int | None = Field(default=None, ge=1, le=65535, description="Port on the peer component that is reached.")


class LocalRefPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    component: str | None = Field(default=None, pattern=DNS1123_LABEL, description="My component the rule is about.")


class LocalTargetPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    component: str | None = Field(default=None, pattern=DNS1123_LABEL, description="My component the peer reaches.")
    port: int | None = Field(default=None, ge=1, le=65535, description="Port on my component the peer reaches it on.")


class InboundRulePatch(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str = Field(
        pattern=DNS1123_LABEL,
        max_length=_NAME_MAX_LENGTH,
        description="Name of this rule; a deployment-level entry with the same name patches the project rule.",
    )
    from_: PeerRefPatch | None = Field(
        default=None, alias="from", description="The peer that is allowed to reach me; may be partial."
    )
    to: LocalTargetPatch | None = Field(
        default=None, description="My component and port the peer reaches; may be partial."
    )
    disabled: bool = Field(default=False, description="Keep the rule but do not apply it.")


class OutboundRulePatch(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str = Field(
        pattern=DNS1123_LABEL,
        max_length=_NAME_MAX_LENGTH,
        description="Name of this rule; a deployment-level entry with the same name patches the project rule.",
    )
    from_: LocalRefPatch | None = Field(
        default=None, alias="from", description="My component that reaches out; may be partial."
    )
    to: PeerTargetPatch | None = Field(
        default=None, description="The peer component and port it may reach; may be partial."
    )
    disabled: bool = Field(default=False, description="Keep the rule but do not apply it.")


class CrossDomainAccessConfig(BaseModel):
    """Project- or deployment-level cross-domain-access config: two lists of named rules.

    This is the STORED shape (patch models), because a root rule may leave the peer
    deployment open and a deployment rule is a partial patch keyed on ``name``. The
    completeness of a rule is judged after the merge, not here.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    #: Both directions are patchable entry by entry, keyed on the ``name`` that already
    #: is the merge key between the project and deployment layer. Adding one rule with
    #: the PUT meant resending every other rule, so the rule you forgot was gone. See
    #: ``opi/services/config_lists.py``.
    ITEM_KEYS: ClassVar[dict[str, str | None]] = {"inbound": "name", "outbound": "name"}

    inbound: list[InboundRulePatch] = Field(
        default_factory=list,
        description="Rules letting another deployment's component reach one of mine; the peer may be another project or your own.",
    )
    outbound: list[OutboundRulePatch] = Field(
        default_factory=list,
        description="Rules letting one of my components reach another deployment's; the peer may be another project or your own.",
    )

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
