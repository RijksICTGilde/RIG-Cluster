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

    date: str
    status: DomainApprovalStatus
    by: str | None = None
    message: str | None = None


class AllowedSubdomainDetail(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    status: DomainApprovalStatus
    history: list[DomainHistoryEntry] = []


class AllowedSubdomainEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    domain: str
    subdomains: list[AllowedSubdomainDetail] = []


class AllowedDomainEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    domain: str
    status: DomainApprovalStatus
    supports_dots: bool | None = Field(default=None, alias="supports-dots")
    issuer: str | None = None
    restricted_subdomains: bool | None = Field(default=None, alias="restricted-subdomains")
    history: list[DomainHistoryEntry] = []


class DomainsConfig(BaseModel):
    """The project-wide domain approval block."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    allowed_subdomains: list[AllowedSubdomainEntry] = Field(default_factory=list, alias="allowed-subdomains")
    allowed_domains: list[AllowedDomainEntry] = Field(default_factory=list, alias="allowed-domains")


class PublishOnWebConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    tls: TlsMode | None = None
    #: Name of the catalog attachment holding the certificate PEM; required for ``provided``.
    attachment: str | None = None
    domains: DomainsConfig | None = None

    @model_validator(mode="after")
    def _provided_needs_an_attachment(self) -> PublishOnWebConfig:
        # Mirrors the if/then in $defs/publish-on-web-config: without the PEM there is
        # nothing to terminate with, and the failure would surface at render time.
        if self.tls == "provided" and not self.attachment:
            raise ValueError("tls 'provided' requires an 'attachment' naming the certificate")
        return self
