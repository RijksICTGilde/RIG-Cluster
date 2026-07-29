"""publish-on-web service.

Owns its component-level config (TLS mode + attachment) hooked into the per-component
form, AND the domain/subdomain approval capability: the `ApprovalSpec`s (declare +
check + list + record) and the approval *state*, which now lives under this service's
config at ``services/[publish-on-web]/config/domains`` (migrated lazily from the legacy
project-root ``domains:`` block via the resolvers in connectors/subdomain.py).

Still NOT owned here -- cross-project platform infrastructure the service depends on but
does not own: the deployment-level "Webadres" domain wizard (DOMAIN_SECTION), the
generic catalog-driven approver interface (opi/services/approvals.py +
router_approvals, no longer domain-specific), the global subdomain registry DB
(connectors/subdomain.py), and ingress generation (project_manager / naming.py).
"""

from __future__ import annotations

from typing import Any

from opi.services.catalog.approval import (
    ApprovalItem,
    ApprovalNotice,
    ApprovalSpec,
    ApprovalStatus,
    ApproverScope,
)
from opi.services.catalog.base import ConfigLayer, Service
from opi.services.services_enums import ServiceType


def _to_status(stored: str | None) -> ApprovalStatus:
    """Map a persisted status string onto ApprovalStatus; anything else -> NONE."""
    try:
        return ApprovalStatus(stored)
    except ValueError:
        return ApprovalStatus.NONE


# --- CHECK: is a requested domain / subdomain approved? --------------------------


def _domain_status(project_data: dict[str, Any], value: Any) -> ApprovalStatus:
    """Approval status of a requested domain (``value`` is the domain string).

    Reads the stored state via the existing pure predicate -- no rules duplicated here.
    """
    from opi.connectors.subdomain import get_project_allowed_domain_config

    cfg = get_project_allowed_domain_config(project_data, value)
    if not isinstance(cfg, dict):
        return ApprovalStatus.NONE
    return _to_status(cfg.get("status"))


def _subdomain_status(project_data: dict[str, Any], value: Any) -> ApprovalStatus:
    """Approval status of a requested subdomain (``value`` is a ``(domain, subdomain)``)."""
    from opi.connectors.subdomain import get_subdomain_status

    domain, subdomain = value
    return _to_status(get_subdomain_status(project_data, domain, subdomain))


# --- LIST: enumerate the approvable items in a project ---------------------------
# These read the state via get_domains_config (service config, root fallback). The wire
# shape is the established ApprovalItem contract (byte-identical to the old router code).


def _domain_items(project_data: dict[str, Any]) -> list[ApprovalItem]:
    from opi.connectors.subdomain import get_domains_config

    domains = get_domains_config(project_data)
    if not isinstance(domains, dict):
        return []
    items: list[ApprovalItem] = []
    for entry in domains.get("allowed-domains", []):
        if not isinstance(entry, dict):
            continue
        items.append(
            {
                "type": "domain",
                "domain": entry.get("domain", ""),
                "name": entry.get("domain", ""),
                "current_status": entry.get("status", ""),
                "status": "skip",
                "history": entry.get("history", []),
            }
        )
    return items


def _subdomain_items(project_data: dict[str, Any]) -> list[ApprovalItem]:
    from opi.connectors.subdomain import get_domains_config

    domains = get_domains_config(project_data)
    if not isinstance(domains, dict):
        return []
    items: list[ApprovalItem] = []
    for entry in domains.get("allowed-subdomains", []):
        if not isinstance(entry, dict):
            continue
        base_domain = entry.get("domain", "")
        for sub in entry.get("subdomains", []):
            if not isinstance(sub, dict):
                continue
            items.append(
                {
                    "type": "subdomain",
                    "domain": base_domain,
                    "name": sub.get("name", ""),
                    "current_status": sub.get("status", ""),
                    "status": "skip",
                    "history": sub.get("history", []),
                }
            )
    return items


# --- NOTICE: what an ungranted approval means for a deployment -------------------
# The deployment still runs: naming.apply_domain_approval_fallback publishes it on the
# cluster address instead of the requested one. That consequence is this service's
# knowledge, so the sentence is written here and generic code only renders it.

_FALLBACK = "Deze deployment is daarom bereikbaar op het standaard clusteradres."


def _deployment_domain(deployment: dict[str, Any]) -> tuple[str | None, str | None]:
    """The (base domain, subdomain) a deployment publishes on, custom input resolved."""
    base_domain = deployment.get("base-domain")
    if base_domain == "__custom__":
        base_domain = deployment.get("base-domain:custom")
    return base_domain or None, deployment.get("subdomain") or None


def _last_verdict(history: Any) -> dict[str, Any]:
    """The most recent history entry, or an empty dict when there is none."""
    if isinstance(history, list) and history and isinstance(history[-1], dict):
        return history[-1]
    return {}


def _notice(label_kind: str, subject: str, status: ApprovalStatus, verdict: dict[str, Any]) -> ApprovalNotice:
    if status is ApprovalStatus.DENIED:
        text = f"Het {label_kind} {subject} is afgewezen. {_FALLBACK}"
    elif status is ApprovalStatus.REQUESTED:
        text = f"Het {label_kind} {subject} is aangevraagd en wacht op goedkeuring. {_FALLBACK}"
    else:
        text = f"Het {label_kind} {subject} is niet goedgekeurd. {_FALLBACK}"
    return {
        "subject": subject,
        "status": status.value,
        "text": text,
        "by": verdict.get("by"),
        "date": verdict.get("date"),
        "message": verdict.get("message"),
    }


def _domain_notices(project_data: dict[str, Any], deployment: dict[str, Any]) -> list[ApprovalNotice]:
    from opi.connectors.subdomain import get_ingress_postfix, get_project_allowed_domain_config
    from opi.core.config import settings

    base_domain, _ = _deployment_domain(deployment)
    if not base_domain or base_domain == get_ingress_postfix(settings.CLUSTER_MANAGER).lstrip("."):
        return []  # No domain of its own, or the cluster's own domain: nothing to approve
    status = _domain_status(project_data, base_domain)
    if status is ApprovalStatus.APPROVED:
        return []
    config = get_project_allowed_domain_config(project_data, base_domain) or {}
    return [_notice("domein", base_domain, status, _last_verdict(config.get("history")))]


def _subdomain_notices(project_data: dict[str, Any], deployment: dict[str, Any]) -> list[ApprovalNotice]:
    from opi.connectors.subdomain import get_project_allowed_subdomains

    base_domain, subdomain = _deployment_domain(deployment)
    if not base_domain or not subdomain:
        return []
    status = _subdomain_status(project_data, (base_domain, subdomain))
    if status is ApprovalStatus.APPROVED or status is ApprovalStatus.NONE:
        # NONE means the domain does not restrict subdomains, so there is nothing to
        # request; a restricted domain always has an entry once a deployment asks for it.
        return []
    detail = next(
        (
            d
            for d in get_project_allowed_subdomains(project_data, base_domain)
            if isinstance(d, dict) and d.get("name", "").lower() == subdomain.lower()
        ),
        {},
    )
    return [_notice("subdomein", f"{subdomain}.{base_domain}", status, _last_verdict(detail.get("history")))]


# --- RECORD: persist an approver verdict onto the stored state -------------------
# The generic layer builds the uniform ``history_entry`` (date/status/by/message);
# each spec locates its own entry and writes the new status + history.


def _domain_record(project_data: dict[str, Any], item: ApprovalItem, history_entry: dict[str, Any]) -> None:
    from opi.connectors.subdomain import ensure_domains_config

    domains = ensure_domains_config(project_data)
    for entry in domains.get("allowed-domains", []):
        if isinstance(entry, dict) and entry.get("domain") == item.get("domain", ""):
            entry["status"] = item.get("status", "skip")
            entry.setdefault("history", []).append(history_entry)
            break


def _subdomain_record(project_data: dict[str, Any], item: ApprovalItem, history_entry: dict[str, Any]) -> None:
    from opi.connectors.subdomain import ensure_domains_config

    domains = ensure_domains_config(project_data)
    for entry in domains.get("allowed-subdomains", []):
        if not isinstance(entry, dict) or entry.get("domain") != item.get("domain", ""):
            continue
        for sub in entry.get("subdomains", []):
            if isinstance(sub, dict) and sub.get("name") == item.get("name", ""):
                sub["status"] = item.get("status", "skip")
                sub.setdefault("history", []).append(history_entry)
                break


class PublishOnWebService(Service):
    service_type = ServiceType.PUBLISH_ON_WEB
    config_component_order = 30

    # Component-level config: TLS mode + attachment. No config_model yet (tls/attachment
    # are not modelled as Pydantic), so config_api_fields stays default.

    def config_editables(self, layer: ConfigLayer):
        if layer is not ConfigLayer.COMPONENT:
            return []
        from opi.services.catalog.publish_on_web.editables import (
            PUBLISH_ON_WEB_ATTACHMENT_EDITABLE,
            PUBLISH_ON_WEB_TLS_EDITABLE,
        )

        return [PUBLISH_ON_WEB_TLS_EDITABLE, PUBLISH_ON_WEB_ATTACHMENT_EDITABLE]

    def config_component_visualizers(self):
        from opi.services.catalog.publish_on_web.visualizers import PUBLISH_ON_WEB_ATTACHMENT, PUBLISH_ON_WEB_TLS

        return [PUBLISH_ON_WEB_TLS, PUBLISH_ON_WEB_ATTACHMENT]

    def config_approvals(self, layer: ConfigLayer):
        # A deployment's requested domain / subdomain needs platform-admin approval
        # before ingress is generated for it. The rule (status_of) reuses the existing
        # pure predicates; the state lives under this service's config
        # (services/[publish-on-web]/config/domains, resolved by connectors/subdomain.py).
        if layer is not ConfigLayer.DEPLOYMENT:
            return []
        return [
            ApprovalSpec(
                key="domain",
                label="Domein",
                approver=ApproverScope.PLATFORM_ADMIN,
                status_of=_domain_status,
                list_items=_domain_items,
                record=_domain_record,
                notices_for=_domain_notices,
            ),
            ApprovalSpec(
                key="subdomain",
                label="Subdomein",
                approver=ApproverScope.PLATFORM_ADMIN,
                status_of=_subdomain_status,
                list_items=_subdomain_items,
                record=_subdomain_record,
                notices_for=_subdomain_notices,
            ),
        ]

    def config_component_layout(self):
        from opi.forms.layout import Fieldset

        svc = self.service_type.value
        return [
            Fieldset(
                legend="Publicatie op het web",
                depends_on="services",
                show_when={"contains": svc},
                children=[f"services{{{svc}}}/config/tls", f"services{{{svc}}}/config/attachment"],
            )
        ]
