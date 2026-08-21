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

from opi.services.catalog.publish_on_web.domain_config import DomainFormatId
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
        description=(
            "The domain to publish on; the cluster's own domain when absent. For a domain of your own, "
            "write the domain name itself here (for example 'domein.nl'); it does not have to be one "
            "the cluster offers, and there is no separate field or marker for it. A hostname on your "
            "own domain is the COMBINATION with a subdomain: base-domain 'domein.nl' + domain-format "
            "'subdomain' + subdomain 'mijn' serves mijn.domein.nl (the bare 'domein.nl' itself is only "
            "served via expose-component-on-bare-domain). A domain of your own needs an administrator's "
            "approval (that your project may use it), DNS records set by your own organisation, AND a "
            "certificate, and those are separate: not every cluster can obtain a certificate for a "
            "domain it does not offer, and on one that cannot, the deployment is created and reachable "
            "but serves an invalid certificate. Read `custom-domain-certificates` from "
            "GET /api/v2/projects/{project}/clusters before setting this; where it is false, supply "
            "your own certificate with the component's tls='provided'."
        ),
    )
    subdomain: str | None = Field(
        default=None,
        description=(
            "The subdomain under the base domain, for the hostname formats that use one. On a "
            "platform-managed domain it is a request an administrator approves, so two projects cannot "
            "claim the same name; on a domain of your own no approval is involved -- which names exist "
            "there is your organisation's business."
        ),
    )
    domain_mode: str | None = Field(
        default=None,
        alias="domain-mode",
        description="LEGACY hostname strategy, superseded by domain-format. Only read for files that predate it.",
    )
    domain_format: DomainFormatId | None = Field(
        default=None,
        alias="domain-format",
        description=(
            "Which hostname template composes the address (see DOMAIN_FORMAT_TEMPLATES). The id set is "
            "closed, so this field carries an enum; which of those ids you can actually use here is "
            "narrower and depends on base-domain, because the dotted variants need a domain that supports "
            "dot-separated hostnames. Read x-choices-source on this field for the list that fits."
        ),
    )
    issuer: str | None = Field(
        default=None,
        description="Certificate issuer for this deployment's ingress; the platform default when absent.",
    )
    root_component: str | None = Field(
        default=None,
        alias="root-component",
        description=(
            "For the dotted formats with a component segment: the component that ALSO serves the "
            "address without that segment (component.deployment.project also serves "
            "deployment.project, typically the frontend). Absent means no component does."
        ),
    )
    expose_component_on_bare_domain: str | bool | None = Field(
        default=None,
        alias="expose-component-on-bare-domain",
        description=(
            "The component served on the bare custom domain itself: base-domain 'domein.nl' with no "
            "name part in front. This is an EXTRA address next to the ones domain-format composes, "
            "not the way to publish on a named address -- mijn.domein.nl is base-domain + a "
            "subdomain format + subdomain. False/absent for none, which is the normal case."
        ),
    )


class PublishOnWebComponentConfig(BaseModel):
    """Component layer: this component uses the service, and how TLS is terminated."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    tls: TlsMode | None = Field(
        default=None,
        description=(
            "How TLS is terminated: 'standard' uses the platform certificate, 'passthrough' lets the pod "
            "present its own, 'provided' terminates a certificate you supply as an attachment. Absent means "
            "inherit (deployment > component > root > standard). 'standard' covers the domains the cluster "
            "offers; for a domain of your own on a cluster that reports custom-domain-certificates=false "
            "(GET /api/v2/projects/{project}/clusters) it yields no certificate at all, and 'provided' is "
            "the way to a working address there."
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
        #
        # De tekst zegt wat je moet DOEN en niet alleen wat er mis is: hij komt via
        # ``project_validation.validation_reasons`` ongewijzigd op het scherm van iemand
        # die een webadres wijzigt, en die stond eerder met een afkeuring zonder uitweg.
        if self.tls == "provided" and not self.attachment:
            raise ValueError(
                "tls 'provided' vereist een 'attachment' met het certificaat: kies 'Standaard certificaat', "
                "of upload eerst een certificaat bij Bijlagen en kies die hier"
            )
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
