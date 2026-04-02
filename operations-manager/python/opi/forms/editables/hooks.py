"""Lifecycle hooks for editable form processing.

Hooks execute at specific FormState stages during form submission.
They receive the full yaml_data and may mutate it in place.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class SubdomainRequestHook:
    """Creates a subdomain request entry in domains.allowed-subdomains.

    Runs at PRE_SAVE. For each deployment that has ``_request-subdomain``
    checked (transient field, still available at PRE_SAVE), adds the
    subdomain to the allow-list with ``status: requested`` and a history entry.
    """

    order: int = 0

    async def execute(self, yaml_data: dict[str, Any], context: dict[str, Any]) -> None:
        from datetime import UTC, datetime

        from opi.connectors.subdomain import get_subdomain_status, get_supported_base_domains
        from opi.core.cluster_config import is_domain_subdomain_restricted
        from opi.core.config import settings
        from opi.utils.naming import DOMAIN_FORMAT_TEMPLATES

        cluster = settings.CLUSTER_MANAGER
        domains_section = yaml_data.get("domains")

        for dep in yaml_data.get("deployments", []):
            if not isinstance(dep, dict):
                continue
            if not dep.get("_request-subdomain"):
                continue

            subdomain = dep.get("subdomain")
            domain_format = dep.get("domain-format", "")

            # Resolve base-domain using transient_value_when_none if not explicitly set
            from opi.forms.editables.resolvers import get_effective_value

            resolvers = context.get("resolvers")
            dep_index = yaml_data.get("deployments", []).index(dep)
            base_domain = get_effective_value(yaml_data, f"deployments[{dep_index}]/base-domain", resolvers)
            template = DOMAIN_FORMAT_TEMPLATES.get(domain_format, "")

            if not subdomain or not base_domain or "{subdomain}" not in template:
                continue

            supported = get_supported_base_domains(cluster)
            if base_domain in supported:
                if not is_domain_subdomain_restricted(cluster, base_domain):
                    continue
            else:
                continue

            if get_subdomain_status(yaml_data, base_domain, subdomain) is not None:
                continue

            if not domains_section:
                domains_section = {}
                yaml_data["domains"] = domains_section
            allowed_subdomains = domains_section.setdefault("allowed-subdomains", [])

            domain_entry = None
            for entry in allowed_subdomains:
                if isinstance(entry, dict) and entry.get("domain") == base_domain:
                    domain_entry = entry
                    break
            if domain_entry is None:
                domain_entry = {"domain": base_domain, "subdomains": []}
                allowed_subdomains.append(domain_entry)

            now = datetime.now(UTC).isoformat()
            domain_entry["subdomains"].append(
                {
                    "name": subdomain.lower(),
                    "status": "requested",
                    "history": [{"date": now, "status": "requested"}],
                }
            )
            logger.info("Subdomain request created: %s.%s", subdomain, base_domain)


class StripTransientsHook:
    """Removes transient field values from the output data.

    Runs at PRE_SAVE with high order (last). Transient fields participate
    in form state and are available to earlier PRE_SAVE hooks, but must
    not persist to the final YAML output.
    """

    order: int = 999

    def __init__(self, editables: list) -> None:
        self._editables = editables

    async def execute(self, yaml_data: dict[str, Any], context: dict[str, Any]) -> None:
        from opi.forms.editables.processor import EditableFormProcessor

        processor = EditableFormProcessor()
        processor.strip_transients_from(yaml_data, self._editables)
