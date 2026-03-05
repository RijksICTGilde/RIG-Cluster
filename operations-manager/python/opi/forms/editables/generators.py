"""Computed value generators for editables.

Generators produce values at submit time from the merged YAML data.
They are executed in a defined order so that later generators can
depend on values produced by earlier ones (e.g., the encrypted
private key depends on the public key).
"""

from __future__ import annotations

import logging
from typing import Any

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


class AGEKeyPairGenerator:
    """Generate an AGE key pair and store both keys.

    Stores the public key at its own yaml_path and puts the plain
    private key into ``_generated/age-private-key-plain`` for use
    by the ``EncryptedPrivateKeyGenerator``.
    """

    def generate(self, yaml_data: dict[str, Any]) -> Any:
        from opi.utils.sops import generate_sops_key_pair

        private_key, public_key = generate_sops_key_pair()
        # Stash plain private key in a temp namespace for later encryption
        yaml_data.setdefault("_generated", {})["age-private-key-plain"] = private_key
        logger.debug("Generated AGE key pair for project")
        return public_key


class EncryptedPrivateKeyGenerator:
    """Encrypt the generated AGE private key with the system AGE key."""

    def generate(self, yaml_data: dict[str, Any]) -> Any:
        from opi.utils.age import encrypt_age_content_sync

        generated = yaml_data.get("_generated", {})
        plain_private_key = generated.get("age-private-key-plain")
        if not plain_private_key:
            raise ValueError("AGE private key not found in _generated data")

        from opi.core.config import settings

        encrypted = encrypt_age_content_sync(plain_private_key, settings.SOPS_AGE_PUBLIC_KEY)
        return encrypted


class DomainModeGenerator:
    """Derive domain-mode from the selected domain-format.

    Uses the reverse of DOMAIN_MODE_DEFAULT_FORMAT so the backend
    domain-mode stays in sync when domain-format is the primary UI control.
    """

    def generate(self, yaml_data: dict[str, Any]) -> Any:
        from opi.utils.naming import DOMAIN_MODE_DEFAULT_FORMAT

        deployments = yaml_data.get("deployments", [])
        if not deployments or not isinstance(deployments[0], dict):
            return "component-specific"
        domain_format = deployments[0].get("domain-format", "component-deployment-project")
        for mode, fmt in DOMAIN_MODE_DEFAULT_FORMAT.items():
            if fmt == domain_format:
                return mode
        return "component-specific"


class EncryptedAPIKeyGenerator:
    """Generate an API key and encrypt it with the project's public key."""

    def generate(self, yaml_data: dict[str, Any]) -> Any:
        from opi.utils.age import encrypt_age_content_sync
        from opi.utils.api_keys import generate_api_key

        public_key = yaml_data.get("config", {}).get("age-public-key")
        if not public_key:
            raise ValueError("Project public key required to encrypt API key")

        plain_api_key = generate_api_key()
        encrypted = encrypt_age_content_sync(plain_api_key, public_key)
        return encrypted
