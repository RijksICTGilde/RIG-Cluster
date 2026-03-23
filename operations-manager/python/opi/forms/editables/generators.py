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


class IssuerGenerator:
    """Compute the TLS issuer based on the deployment's base-domain.

    Looks up the domain in the cluster's supported_domains configuration.
    Returns the per-domain issuer (e.g. ``"letsencrypt"``) or ``None``
    when no issuer is needed (the cluster's default handles TLS).

    The deployment index determines which deployment to read the
    base-domain from. It is set during editable materialization
    (``[*]`` -> ``[N]``).
    """

    def __init__(self, deployment_index: int = 0) -> None:
        self.deployment_index = deployment_index

    def generate(self, yaml_data: dict[str, Any]) -> Any:
        from opi.core.cluster_config import get_domain_issuer
        from opi.core.config import settings

        deployments = yaml_data.get("deployments", [])
        if len(deployments) <= self.deployment_index:
            return None
        dep = deployments[self.deployment_index]
        if not isinstance(dep, dict):
            return None

        base_domain = dep.get("base-domain")
        if not base_domain:
            return None

        cluster = settings.CLUSTER_MANAGER
        issuer = get_domain_issuer(cluster, base_domain)
        if issuer:
            return issuer

        # Custom domains (not in cluster's supported_domains) need letsencrypt
        from opi.connectors.subdomain import get_supported_base_domains

        if base_domain not in get_supported_base_domains(cluster=cluster):
            return "letsencrypt"

        return None


class UserEnvVarsEncryptGenerator:
    """Encrypt user-env-vars on each component with the project's AGE public key.

    Iterates over all components and encrypts any non-empty ``user-env-vars``
    string value. Skips values that are already AGE-encrypted.

    Must run after ``AGEKeyPairGenerator`` so the project public key exists.
    Uses a ``_generated`` path - the return value is discarded during cleanup.
    """

    def generate(self, yaml_data: dict[str, Any]) -> Any:
        from ruamel.yaml.scalarstring import LiteralScalarString

        from opi.utils.age import encrypt_age_content_sync

        public_key = yaml_data.get("config", {}).get("age-public-key")
        if not public_key:
            logger.debug("No project public key available, skipping user-env-vars encryption")
            return True

        for component in yaml_data.get("components", []):
            if not isinstance(component, dict):
                continue
            user_env_vars = component.get("user-env-vars")
            if not user_env_vars or not isinstance(user_env_vars, str):
                continue
            if "BEGIN AGE ENCRYPTED FILE" in user_env_vars:
                continue
            encrypted = encrypt_age_content_sync(user_env_vars, public_key)
            component["user-env-vars"] = LiteralScalarString(encrypted)
            logger.debug(
                "Encrypted user-env-vars for component %s",
                component.get("name", "unknown"),
            )

        return True
