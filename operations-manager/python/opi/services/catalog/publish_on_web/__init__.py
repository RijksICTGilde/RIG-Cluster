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
from opi.services.catalog.publish_on_web.config_model import (
    PublishOnWebComponentConfig,
    PublishOnWebDeploymentConfig,
    PublishOnWebProjectConfig,
)
from opi.services.catalog.publish_on_web.domain_config import DomainSetting, get_domain_setting
from opi.services.catalog.publish_on_web.variables import WebVariables
from opi.services.services import ServiceDefinition
from opi.services.services_enums import ServiceBinding, ServiceType


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
    base_domain = get_domain_setting(deployment, DomainSetting.BASE_DOMAIN)
    if base_domain == "__custom__":
        base_domain = deployment.get("base-domain:custom")
    return base_domain or None, get_domain_setting(deployment, DomainSetting.SUBDOMAIN) or None


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
    definition = ServiceDefinition(
        name="Publiceren op het web",
        description="Maak de applicatie toegankelijk via het publieke internet",
        help_template="publish_on_web/help.md",
        icon="wereldbol",
        color="hemelblauw",
        binding=ServiceBinding.COMPONENT,
        variables=[var.value for var in WebVariables],
    )
    #: The component model: this service is bound at the component layer, and its entries
    #: are the ones stamped with a ``schema-version``, so that is what the committed
    #: fragment documents. The other two layers answer through ``config_model_for``.
    config_model = PublishOnWebComponentConfig
    config_schema_version = "1.0"
    config_component_order = 30

    def config_model_for(self, layer: ConfigLayer):
        """One model per layer -- three questions, three shapes (RC-60).

        Project: which domains may this project publish on. Deployment: how is this
        deployment's address composed. Component: does this component use the service, and
        how is TLS terminated. A single model spanning all three would be ten optional
        fields with nothing to stop ``tls`` from landing on a deployment.
        """
        if layer is ConfigLayer.PROJECT:
            return PublishOnWebProjectConfig
        if layer is ConfigLayer.DEPLOYMENT:
            return PublishOnWebDeploymentConfig
        # Component and deployment-component: the same tls/attachment pair, since the
        # deployment-component entry exists precisely to override the component's.
        return PublishOnWebComponentConfig

    def config_editables(self, layer: ConfigLayer):
        if layer is ConfigLayer.DEPLOYMENT:
            from opi.services.catalog.publish_on_web.editables import PUBLISH_ON_WEB_DEPLOYMENT_EDITABLES

            return list(PUBLISH_ON_WEB_DEPLOYMENT_EDITABLES)
        if layer is not ConfigLayer.COMPONENT:
            return []
        from opi.services.catalog.publish_on_web.editables import (
            PUBLISH_ON_WEB_ATTACHMENT_EDITABLE,
            PUBLISH_ON_WEB_TLS_EDITABLE,
        )

        return [PUBLISH_ON_WEB_TLS_EDITABLE, PUBLISH_ON_WEB_ATTACHMENT_EDITABLE]

    def config_form_section(self, layer: ConfigLayer):
        """The "Webadres" step at the deployment layer; the base default elsewhere."""
        if layer is ConfigLayer.DEPLOYMENT:
            return self.deployment_form_section()
        return super().config_form_section(layer)

    def deployment_form_section(self, deployment_index: int | None = None, *, edit_mode: bool = False):
        """The "Webadres" section, optionally materialized for one deployment (RC-60).

        The wizard step and the edit modal are the same form with a different heading and a
        different post-save action, which is why one builder answers both. It used to be a
        hand-authored ``DOMAIN_SECTION`` in ``forms/visualizers/wizard_sections.py``, a file
        no part of this service could see -- so the fields, their display and their step all
        lived apart from the values they configure. ``cross_domain_access`` already builds
        its deployment section this way.

        Without ``deployment_index`` the section describes the LAYER (wildcards intact),
        which is what ``config_form_section`` answers and ``test_service_config_layers``
        measures. With one, the wildcards are materialized so the form reads and writes the
        right deployment.
        """
        from opi.forms.editables.editable import SERVICE_VIRTUALIZE, apply_virtualize
        from opi.forms.editables.enforcers import DomainConfigEnforcer
        from opi.forms.editables.reindex import materialize_wildcard_layout, materialize_wildcard_visualizer
        from opi.forms.layout import DisplayBlock, TemplatePartial
        from opi.forms.visualizers.display_blocks import compute_url_preview
        from opi.forms.visualizers.sections import FormSection
        from opi.services.catalog.publish_on_web.domain_config import DomainSetting, domain_setting_path
        from opi.services.catalog.publish_on_web.editables import CUSTOM_BASE_DOMAIN_PATH
        from opi.services.catalog.publish_on_web.visualizers import DOMAIN_CONFIG

        index = 0 if deployment_index is None else deployment_index

        def field(setting: DomainSetting) -> str:
            # The renderer keys a virtualized field by its VIRTUAL path, so the layout has
            # to name it the same way or the field is simply not laid out -- it disappears
            # from the step without an error anywhere.
            return apply_virtualize(domain_setting_path(setting), SERVICE_VIRTUALIZE)

        layout = [
            TemplatePartial(template="wizard/partials/domain_info.html.j2"),
            field(DomainSetting.BASE_DOMAIN),
            "deployments[*]/_request-domain",
            CUSTOM_BASE_DOMAIN_PATH,
            field(DomainSetting.DOMAIN_FORMAT),
            field(DomainSetting.SUBDOMAIN),
            "deployments[*]/_request-subdomain",
            field(DomainSetting.ROOT_COMPONENT),
            field(DomainSetting.BARE_DOMAIN_COMPONENT),
            DisplayBlock(
                display_id="url-preview",
                compute=compute_url_preview,
                template="wizard/partials/url_preview.html.j2",
                context={"deployment_index": index},
            ),
        ]

        if edit_mode:
            section_id = f"domain-edit-{index}"
            title = "Webadres bewerken"
            description = "Wijzig het webadres voor deze deployment"
            post_save_action = "process_project"
        else:
            section_id = "domains"
            title = "Webadres"
            description = "Configureer hoe je applicatie bereikbaar wordt"
            post_save_action = "save_only"

        return FormSection(
            section_id=section_id,
            title=title,
            icon="wereldbol",
            description=description,
            enforcer=DomainConfigEnforcer(deployment_index=index),
            editables=[materialize_wildcard_visualizer(DOMAIN_CONFIG, index)],
            layout=materialize_wildcard_layout(layout, index),
            post_save_action=post_save_action,
        )

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
