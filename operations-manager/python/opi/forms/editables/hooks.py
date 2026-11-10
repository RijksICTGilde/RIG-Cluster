"""Lifecycle hooks for editable form processing.

Hooks execute at specific FormState stages during form submission.
They receive the full yaml_data and may mutate it in place.
"""

from __future__ import annotations

import logging
from typing import Any

from opi.core import config as opi_config
from opi.core.cluster_config import get_ingress_postfix
from opi.forms.editables.processor import EditableFormProcessor
from opi.forms.editables.resolvers import get_effective_value
from opi.services.catalog.publish_on_web.domain_config import (
    DomainSetting,
    domain_setting_path,
    get_domain_setting,
    set_domain_setting,
)
from opi.services.catalog.publish_on_web.subdomain import ensure_domain_requests

logger = logging.getLogger(__name__)


def _resolve_missing_base_domains(yaml_data: dict[str, Any], context: dict[str, Any]) -> None:
    """Fill in None base-domain values from the resolvers, mutating the deployments in place.

    A base-domain select the user never touched is None in the wizard state, while the
    resolvers know the cluster default, and ``ensure_domain_requests`` needs the actual domain.

    The cluster default itself is never materialised: a cluster-default deployment carries no
    explicit ``base-domain`` (its absence *means* "use the default"), so writing it would add
    a spurious key and a phantom domain request.
    """
    resolvers = context.get("resolvers")
    if not resolvers:
        return

    cluster_default = get_ingress_postfix(opi_config.settings.CLUSTER_MANAGER).lstrip(".")
    for i, dep in enumerate(yaml_data.get("deployments", [])):
        if isinstance(dep, dict) and not get_domain_setting(dep, DomainSetting.BASE_DOMAIN):
            resolved = get_effective_value(yaml_data, domain_setting_path(DomainSetting.BASE_DOMAIN, i), resolvers)
            if resolved and resolved != cluster_default:
                set_domain_setting(dep, DomainSetting.BASE_DOMAIN, resolved)


class SubdomainRequestHook:
    """Creates subdomain request entries at PRE_SAVE.

    Only runs when the ``_request-subdomain`` transient checkbox is checked.
    Delegates to ``ensure_domain_requests`` for the actual logic.
    """

    order: int = 0

    async def execute(self, yaml_data: dict[str, Any], context: dict[str, Any]) -> None:
        for dep in yaml_data.get("deployments", []):
            if isinstance(dep, dict) and dep.get("_request-subdomain"):
                _resolve_missing_base_domains(yaml_data, context)
                ensure_domain_requests(yaml_data, opi_config.settings.CLUSTER_MANAGER)
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
                _resolve_missing_base_domains(yaml_data, context)
                ensure_domain_requests(yaml_data, opi_config.settings.CLUSTER_MANAGER)
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
        processor = EditableFormProcessor()
        processor.strip_transients_from(yaml_data, self._editables)


class ResolveAttachmentsHook:
    """Inject staged attachment uploads into the catalog and encrypt them (edit flow).

    The modal-edit flow already has the project AGE key in ``yaml_data`` at PRE_SAVE,
    so staged uploads (passed via ``context['staged_attachments']``, a map id ->
    {filename, content="staging:<token>"}) are written into the project-level
    attachments service ``data`` list and encrypted here. The create flow does the
    same via AttachmentStagingResolveGenerator instead, because its key is generated
    only after PRE_SAVE.
    """

    order: int = 1

    async def execute(self, yaml_data: dict[str, Any], context: dict[str, Any]) -> None:
        from opi.forms.editables.generators import AttachmentStagingResolveGenerator
        from opi.services.project_file_handler import merge_staged_attachments

        staged = context.get("staged_attachments") or {}
        if not staged:
            return

        merge_staged_attachments(yaml_data, staged)
        AttachmentStagingResolveGenerator().generate(yaml_data)


class PreserveAttachmentContentHook:
    """Re-attach the encrypted content of existing attachments at save (smart merge).

    The modal wizard strips attachment ``content`` from its session state (the catalog only
    needs ``id`` and ``filename``), so the saved catalog entries arrive without it. This
    restores it from the original project data, captured before the form merge and passed via
    ``context['original_attachment_content']`` ({id -> content}). It covers existing
    attachments; a new upload goes through ``ResolveAttachmentsHook``. Runs before
    ``StripTransientsHook``, so the restored content survives.
    """

    order: int = 1

    async def execute(self, yaml_data: dict[str, Any], context: dict[str, Any]) -> None:
        from opi.services.project_file_handler import find_attachment_data_list

        original = context.get("original_attachment_content") or {}
        data = find_attachment_data_list(yaml_data.get("services")) if original else None
        if not data:
            return
        for att in data:
            if isinstance(att, dict) and not att.get("content"):
                content = original.get(att.get("id"))
                if content:
                    att["content"] = content
