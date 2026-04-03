"""Lifecycle hooks for editable form processing.

Hooks execute at specific FormState stages during form submission.
They receive the full yaml_data and may mutate it in place.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _resolve_missing_base_domains(yaml_data: dict[str, Any], context: dict[str, Any]) -> None:
    """Fill in None base-domain values from resolvers before processing.

    When the user didn't interact with the base-domain select, the value
    is None in the wizard state. The resolvers know the cluster default.
    This mutates the deployment dicts in place so ensure_domain_requests
    sees the actual domain.
    """
    resolvers = context.get("resolvers")
    if not resolvers:
        return

    from opi.forms.editables.resolvers import get_effective_value

    for i, dep in enumerate(yaml_data.get("deployments", [])):
        if isinstance(dep, dict) and not dep.get("base-domain"):
            resolved = get_effective_value(yaml_data, f"deployments[{i}]/base-domain", resolvers)
            if resolved:
                dep["base-domain"] = resolved


class SubdomainRequestHook:
    """Creates subdomain request entries at PRE_SAVE.

    Only runs when the ``_request-subdomain`` transient checkbox is checked.
    Delegates to ``ensure_domain_requests`` for the actual logic.
    """

    order: int = 0

    async def execute(self, yaml_data: dict[str, Any], context: dict[str, Any]) -> None:
        for dep in yaml_data.get("deployments", []):
            if isinstance(dep, dict) and dep.get("_request-subdomain"):
                from opi.connectors.subdomain import ensure_domain_requests
                from opi.core.config import settings

                _resolve_missing_base_domains(yaml_data, context)
                ensure_domain_requests(yaml_data, settings.CLUSTER_MANAGER)
                return


class DomainRequestHook:
    """Creates domain request entries at PRE_SAVE.

    Only runs when the ``_request-domain`` transient checkbox is checked.
    Delegates to ``ensure_domain_requests`` for the actual logic.
    """

    order: int = 0

    async def execute(self, yaml_data: dict[str, Any], context: dict[str, Any]) -> None:
        for dep in yaml_data.get("deployments", []):
            if isinstance(dep, dict) and dep.get("_request-domain"):
                from opi.connectors.subdomain import ensure_domain_requests
                from opi.core.config import settings

                _resolve_missing_base_domains(yaml_data, context)
                ensure_domain_requests(yaml_data, settings.CLUSTER_MANAGER)
                return


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
