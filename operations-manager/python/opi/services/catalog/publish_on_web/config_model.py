"""Typed config model for the ``publish-on-web`` service.

Two layers with different content, and ``config_model`` is one class per service, so both
live in one model with every field optional:

- component (and per-deployment-component override): ``tls``, plus ``attachment`` when the
  certificate is supplied by the project. Resolution is deployment > component > root >
  ``standard``.
- project: the ``domains`` approval block, which the v2.5 migration relocated here from the
  project root (``normalize_domains_location``). ``connectors/subdomain.py`` is the single
  authority on where it lives; this model only says what may be in it.

The shape mirrors ``$defs/publish-on-web-config`` and ``$defs/domains`` in
``project_v2.json``, which still hold the same knowledge. Those defs are the next thing to
retire now that the service owns the contract, but they are referenced from three places in
the global schema, so that is a separate change.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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


class PublishOnWebConfig(BaseModel):
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
    domains: DomainsConfig | None = Field(
        default=None, description="Project-level domain approvals. Written by the platform's approval flow."
    )

    @model_validator(mode="after")
    def _provided_needs_an_attachment(self) -> PublishOnWebConfig:
        # Mirrors the if/then in $defs/publish-on-web-config: without the PEM there is
        # nothing to terminate with, and the failure would surface at render time.
        if self.tls == "provided" and not self.attachment:
            raise ValueError("tls 'provided' requires an 'attachment' naming the certificate")
        return self
