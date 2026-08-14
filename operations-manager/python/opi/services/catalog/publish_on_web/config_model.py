"""Typed config models for the ``publish-on-web`` service -- one per layer.

The three layers this service carries config on each answer a different question, so each
gets its own model, selected by ``PublishOnWebService.config_model_for(layer)``:

- **project** (:class:`PublishOnWebProjectConfig`) -- *which domains is this project allowed
  to publish on*: the ``domains`` approval block, which the v2.5 migration relocated here
  from the project root (``normalize_domains_location``). ``connectors/subdomain.py`` is the
  single authority on where it lives; this model only says what may be in it. It also
  carries the project-wide ``tls`` default, which is level 3 of the certificate cascade.
- **deployment** (:class:`PublishOnWebDeploymentConfig`) -- *how this deployment's hostname
  is composed and with which certificate*: the seven fields the v2.7 migration relocated
  here from the deployment root (RC-60). ``domain_config.py`` is the single authority on
  where they live.
- **component** (and the per-deployment-component override,
  :class:`PublishOnWebComponentConfig`) -- *this component uses the service, thus*: ``tls``,
  plus ``attachment`` when the certificate is supplied by the project. Resolution is
  deployment > component > root > ``standard``.

One model per layer rather than one bag of ten optional fields: with a single model nothing
stops ``tls`` from landing on a deployment or ``subdomain`` on a component, and "every field
optional" is exactly what makes such a file validate. ``config_model`` names the component
model -- the layer this ``ServiceBinding.COMPONENT`` service is actually bound at, and the
one whose entries carry a stamped ``schema-version`` -- so that is what the committed
fragment documents; the other two are OPI-written and validated shape-first (see
``project_validation._validate_one_config``).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from opi.services.config_managed import PLATFORM_MANAGED

#: ``standard`` = platform certificate (Let's Encrypt); ``passthrough`` = the pod presents
#: its own certificate; ``provided`` = own certificate terminated on the ingress.
TlsMode = Literal["standard", "passthrough", "provided"]
#: Approval states shared by domains, subdomains and their history entries.
DomainApprovalStatus = Literal["requested", "approved", "denied"]


class DomainHistoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    date: str = Field(description="When this decision was recorded, ISO 8601.")
    status: DomainApprovalStatus = Field(description="What was decided: requested, approved or denied.")
    by: str | None = Field(default=None, description="Who decided it.")
    message: str | None = Field(default=None, description="The reason given with the decision.")


class AllowedSubdomainDetail(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str = Field(description="The subdomain, without the domain it sits under.")
    status: DomainApprovalStatus = Field(description="Approval state of this subdomain.")
    history: list[DomainHistoryEntry] = Field(default=[], description="Every decision made about it, oldest first.")


class AllowedSubdomainEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    domain: str = Field(description="The domain these subdomains sit under.")
    subdomains: list[AllowedSubdomainDetail] = Field(
        default=[], description="The subdomains requested under it, with their approval state."
    )


class AllowedDomainEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    domain: str = Field(description="The domain this project may publish on.")
    status: DomainApprovalStatus = Field(description="Approval state of the domain itself.")
    supports_dots: bool | None = Field(
        default=None,
        alias="supports-dots",
        description="Whether a dotted subdomain is allowed under it; a wildcard certificate covers one level only.",
    )
    issuer: str | None = Field(
        default=None, description="Certificate issuer to use for this domain; the platform default when absent."
    )
    restricted_subdomains: bool | None = Field(
        default=None,
        alias="restricted-subdomains",
        description="Whether every subdomain under it needs its own approval.",
    )
    history: list[DomainHistoryEntry] = Field(default=[], description="Every decision made about it, oldest first.")


class DomainsConfig(BaseModel):
    """The project-wide domain approval block."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    allowed_subdomains: list[AllowedSubdomainEntry] = Field(
        default_factory=list,
        alias="allowed-subdomains",
        description="Per domain, the subdomains this project requested and their approval state.",
    )
    allowed_domains: list[AllowedDomainEntry] = Field(
        default_factory=list,
        alias="allowed-domains",
        description="The domains this project may publish on, with their approval state.",
    )


class PublishOnWebDeploymentConfig(BaseModel):
    """Deployment layer: how this deployment's web address is composed (RC-60).

    Every field is optional because a deployment on the platform's own cluster domain
    needs none of them: the format defaults, the base domain comes from the cluster and the
    issuer from the domain entry. ``domain-mode`` is legacy -- superseded by
    ``domain-format`` and read only by ``HostnameFormat.from_domain_mode`` for old files --
    but it describes the same subject, so it moved along rather than staying behind as a
    second place to look.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    base_domain: str | None = Field(
        default=None,
        alias="base-domain",
        description="The domain to publish on; the cluster's own domain when absent.",
    )
    subdomain: str | None = Field(
        default=None,
        description="The subdomain under the base domain, for the hostname formats that use one.",
    )
    domain_mode: str | None = Field(
        default=None,
        alias="domain-mode",
        description="LEGACY hostname strategy, superseded by domain-format. Only read for files that predate it.",
    )
    domain_format: str | None = Field(
        default=None,
        alias="domain-format",
        description="Which hostname template composes the address (see DOMAIN_FORMAT_TEMPLATES).",
    )
    issuer: str | None = Field(
        default=None,
        description="Certificate issuer for this deployment's ingress; the platform default when absent.",
    )
    root_component: str | None = Field(
        default=None,
        alias="root-component",
        description="The component served on the address without a component segment, for the formats that have one.",
    )
    expose_component_on_bare_domain: str | bool | None = Field(
        default=None,
        alias="expose-component-on-bare-domain",
        description="Component served on the bare custom domain, or false/absent for none.",
    )


class PublishOnWebComponentConfig(BaseModel):
    """Component layer: this component uses the service, and how TLS is terminated."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    tls: TlsMode | None = Field(
        default=None,
        description=(
            "How TLS is terminated: 'standard' uses the platform certificate, 'passthrough' lets the pod "
            "present its own, 'provided' terminates a certificate you supply as an attachment. Absent means "
            "inherit (deployment > component > root > standard)."
        ),
    )
    attachment: str | None = Field(
        default=None,
        description="Id of the attachment holding the certificate PEM. Required when tls is 'provided'.",
    )

    @model_validator(mode="after")
    def _provided_needs_an_attachment(self) -> PublishOnWebComponentConfig:
        # Mirrors the if/then in $defs/publish-on-web-config: without the PEM there is
        # nothing to terminate with, and the failure would surface at render time.
        if self.tls == "provided" and not self.attachment:
            raise ValueError("tls 'provided' requires an 'attachment' naming the certificate")
        return self


class PublishOnWebProjectConfig(PublishOnWebComponentConfig):
    """Project layer: which domains this project may publish on, plus the TLS default.

    Two things, because the code already holds both there. ``domains`` is the approval
    block the v2.5 migration relocated here. ``tls`` / ``attachment`` are inherited rather
    than repeated: level 3 of the cascade in
    ``project_file_handler._resolve_publish_on_web_config`` is the project-level entry, so
    a project can set one certificate mode for every component that does not override it.
    Dropping them here would have made every such project file fail validation.
    """

    domains: DomainsConfig | None = Field(
        default=None,
        # Platform-written, and it shares this block with the inherited user settings tls
        # and attachment -- so a PUT that sets only tls used to take every domain verdict
        # and its history with it. An approval is an approver's decision plus an audit
        # trail; a caller may not clear it by not mentioning it. See
        # opi/services/config_managed.py.
        json_schema_extra={PLATFORM_MANAGED: True},
        description=(
            "Project-level domain approvals. Written by the platform's approval flow and carried over on a "
            "write, so a caller neither has to send it nor can lose it by leaving it out."
        ),
    )
