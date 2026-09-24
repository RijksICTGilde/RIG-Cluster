"""Which issuer signs the certificate for a deployment's own web address.

The ``issuer`` field in a deployment's publish-on-web config is a stored OVERRIDE, not the
only source of truth. The portal writes it (``IssuerGenerator`` derives it from the chosen
base domain at submit time), but the API and task write paths do not: ``configure_service``
runs no editable generators, and no service fills this field in. A deployment configured
through zad-cli or the deploy action therefore arrived with a base domain and no issuer,
which left its ingress without a cert-manager annotation AND without a ``secretName``, so
the platform issued nothing and the router served its own wildcard certificate instead.
Nothing failed anywhere: the manifests were valid, ArgoCD was green, and only the browser
knew. Measured on mzs-3ik/site-admin (moza-site.rijks.app, 2026-09-20).

So the manifest generation derives the issuer itself when the field is absent
(:func:`effective_issuer`) rather than trusting whoever wrote the file, and every write path
gets a certificate on the same terms. A stored value still wins, which is how a project
points at an issuer of its own.

This lives next to -- not inside -- ``domain_config`` because the derivation needs
``connectors/subdomain``, and that module imports ``domain_config``.
"""

from __future__ import annotations

from typing import Any

from opi.connectors.subdomain import (
    get_project_allowed_domain_config,
    get_supported_base_domains,
    is_deployment_domain_approved,
)
from opi.core.cluster_config import get_domain_issuer
from opi.services.catalog.publish_on_web.domain_config import DomainSetting, get_domain_setting


def derive_issuer(project_data: dict[str, Any], deployment: dict[str, Any], cluster: str) -> str | None:
    """The issuer the deployment's base domain implies, whatever the file stores.

    None when the deployment has no base domain of its own: it then publishes on the cluster
    address, which the platform certificate already covers.
    """
    base_domain = get_domain_setting(deployment, DomainSetting.BASE_DOMAIN)
    if not base_domain:
        return None

    issuer = get_domain_issuer(cluster, base_domain)
    if issuer:
        return issuer

    # A domain the cluster does not list is one of the project's own. Its entry in the
    # project's ``domains`` block may name an issuer; otherwise Let's Encrypt over an ACME
    # HTTP-01 challenge, the same default the portal offers.
    if base_domain not in get_supported_base_domains(cluster=cluster):
        custom_config = get_project_allowed_domain_config(project_data, base_domain)
        if custom_config and custom_config.get("issuer"):
            return custom_config["issuer"]
        return "letsencrypt"

    return None


def effective_issuer(project_data: dict[str, Any], deployment: dict[str, Any], cluster: str) -> str | None:
    """The issuer to generate manifests with: the stored value, else the derived one.

    The derivation is skipped while the domain is not approved for this project, because the
    hostname then falls back to the cluster address (``get_component_ingress_map``) and an
    ingress on that name must not carry an issuer for a domain nobody granted -- cert-manager
    would chase a challenge for a name the ingress does not serve. Approval makes the issuer
    appear on the next run, since the manifests are regenerated from the project file every
    time.
    """
    stored = get_domain_setting(deployment, DomainSetting.ISSUER)
    if stored:
        return stored

    base_domain = get_domain_setting(deployment, DomainSetting.BASE_DOMAIN)
    subdomain = get_domain_setting(deployment, DomainSetting.SUBDOMAIN)
    if not is_deployment_domain_approved(project_data, base_domain, subdomain, cluster):
        return None

    return derive_issuer(project_data, deployment, cluster)
