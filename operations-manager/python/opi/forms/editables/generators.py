"""Computed value generators for editables.

Generators produce values at submit time from the merged YAML data.
They are executed in the order of ``GENERATED_EDITABLES_PURE``, so a later
generator may depend on what an earlier one produced.
"""

from __future__ import annotations

import logging
from typing import Any

from opi.core import config as opi_config
from opi.services.catalog.publish_on_web.issuer import derive_issuer
from opi.utils.age import get_project_public_key

logger = logging.getLogger(__name__)


class ProjectNameGenerator:
    """Generate a technical project name from the display-name field."""

    def generate(self, yaml_data: dict[str, Any]) -> Any:
        from opi.utils.project_names import generate_project_name

        display_name = yaml_data.get("display-name", "")
        if not display_name:
            raise ValueError("Display-name is required to generate project name")
        technical_name, _ = generate_project_name(display_name)
        return technical_name


class IssuerGenerator:
    """Compute the TLS issuer based on the deployment's base-domain.

    Stores what the chosen domain implies, so the field reads as a normal setting the user
    can then override. The manifest generation no longer depends on it being there: it
    derives the same value through ``effective_issuer`` when the field is absent, which is
    what the API and task write paths leave behind.

    The deployment index determines which deployment to read the
    base-domain from. It is set during editable materialization
    (``[*]`` -> ``[N]``).
    """

    def __init__(self, deployment_index: int = 0) -> None:
        self.deployment_index = deployment_index

    def generate(self, yaml_data: dict[str, Any]) -> Any:
        deployments = yaml_data.get("deployments", [])
        if len(deployments) <= self.deployment_index:
            return None
        dep = deployments[self.deployment_index]
        if not isinstance(dep, dict):
            return None

        return derive_issuer(yaml_data, dep, opi_config.settings.CLUSTER_MANAGER)


class AttachmentStagingResolveGenerator:
    """Resolve staged attachment uploads into encrypted catalog entries.

    During the create-wizard a file is parked in the staging store and the
    catalog entry carries ``content: "staging:<token>"``. At submit time this generator
    reads each staged file, encrypts it with the project public key into an AGE block,
    replaces the placeholder, and removes the staged file. Uses a ``_generated`` path - the
    return value is discarded during cleanup.
    """

    _PREFIX = "staging:"

    def generate(self, yaml_data: dict[str, Any]) -> Any:
        from ruamel.yaml.scalarstring import LiteralScalarString

        from opi.services import upload_staging
        from opi.utils.age import encrypt_file_to_age_block_sync

        public_key = get_project_public_key(yaml_data)

        for service in yaml_data.get("services", []):
            if not isinstance(service, dict) or not isinstance(service.get("attachments"), dict):
                continue
            for entry in service["attachments"].get("data", []) or []:
                if not isinstance(entry, dict):
                    continue
                content = entry.get("content")
                if not isinstance(content, str) or not content.startswith(self._PREFIX):
                    continue
                token = content[len(self._PREFIX) :]
                staged = upload_staging.read_staged(token)
                if staged is None:
                    logger.warning("Staged attachment '%s' not found; skipping", entry.get("id"))
                    continue
                if not public_key:
                    # Fail closed: returning here would commit the literal
                    # 'staging:<token>' placeholder, which later fails AGE
                    # decryption at deploy time. Abort the save instead.
                    raise ValueError(
                        f"Kan bijlage '{entry.get('id')}' niet versleutelen: project heeft geen AGE-publieke sleutel."
                    )
                raw_bytes, _ = staged
                entry["content"] = LiteralScalarString(encrypt_file_to_age_block_sync(raw_bytes, public_key))
                upload_staging.delete_staged(token)
                logger.debug("Resolved staged attachment '%s'", entry.get("id"))

        return True
