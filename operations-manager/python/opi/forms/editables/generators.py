"""Computed value generators for editables.

Generators produce values at submit time from the merged YAML data.
They are executed in a defined order so that later generators can
depend on values produced by earlier ones (e.g., the encrypted
private key depends on the public key).
"""

from __future__ import annotations

import logging
from typing import Any

from opi.connectors.subdomain import (
    get_project_allowed_domain_config,
    get_supported_base_domains,
)
from opi.core import config as opi_config
from opi.core.cluster_config import get_domain_issuer
from opi.services.catalog.publish_on_web.domain_config import DomainSetting, get_domain_setting

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
        deployments = yaml_data.get("deployments", [])
        if len(deployments) <= self.deployment_index:
            return None
        dep = deployments[self.deployment_index]
        if not isinstance(dep, dict):
            return None

        base_domain = get_domain_setting(dep, DomainSetting.BASE_DOMAIN)
        if not base_domain:
            return None

        cluster = opi_config.settings.CLUSTER_MANAGER
        issuer = get_domain_issuer(cluster, base_domain)
        if issuer:
            return issuer

        # Custom domains (not in cluster's supported_domains): check project config first
        if base_domain not in get_supported_base_domains(cluster=cluster):
            custom_config = get_project_allowed_domain_config(yaml_data, base_domain)
            if custom_config and custom_config.get("issuer"):
                return custom_config["issuer"]
            return "letsencrypt"

        return None


class AttachmentStagingResolveGenerator:
    """Resolve staged attachment uploads into encrypted catalog entries.

    During the create-wizard a file is parked in the staging store and the
    catalog entry carries ``content: "staging:<token>"``. At submit time (after
    ``AGEKeyPairGenerator`` has produced the project public key) this generator
    reads each staged file, encrypts it into an AGE block, replaces the
    placeholder, and removes the staged file. Uses a ``_generated`` path - the
    return value is discarded during cleanup.
    """

    _PREFIX = "staging:"

    def generate(self, yaml_data: dict[str, Any]) -> Any:
        from ruamel.yaml.scalarstring import LiteralScalarString

        from opi.services import upload_staging
        from opi.utils.age import encrypt_file_to_age_block_sync

        public_key = yaml_data.get("config", {}).get("age-public-key")

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


class UserEnvVarsEncryptGenerator:
    """Encrypt user-env-vars with the project's AGE public key, on BOTH layers.

    Encrypts any non-empty ``user-env-vars`` string value that is not already an AGE
    block. Skips values that are already encrypted.

    Both layers, not just ``components[*]``: the same service owns
    ``deployments[*]/components[*]/user-env-vars`` (RC-25), and a deployment override
    holds exactly the same kind of secret as the component value it overrides. This
    generator is the backstop for when the converter did not encrypt (no context data,
    a hand-edited file), and a backstop that covers half the layers leaves the other
    half in plaintext in git -- which is what it did until RC-55.

    Must run after ``AGEKeyPairGenerator`` so the project public key exists.
    Uses a ``_generated`` path - the return value is discarded during cleanup.
    """

    def generate(self, yaml_data: dict[str, Any]) -> Any:
        public_key = yaml_data.get("config", {}).get("age-public-key")
        if not public_key:
            logger.debug("No project public key available, skipping user-env-vars encryption")
            return True

        for component in yaml_data.get("components", []):
            self._encrypt(component, public_key, component.get("name") if isinstance(component, dict) else None)
        for deployment in yaml_data.get("deployments", []):
            if not isinstance(deployment, dict):
                continue
            for component in deployment.get("components", []):
                self._encrypt(
                    component,
                    public_key,
                    component.get("reference") if isinstance(component, dict) else None,
                )

        return True

    @staticmethod
    def _encrypt(component: Any, public_key: str, label: str | None) -> None:
        from ruamel.yaml.scalarstring import LiteralScalarString

        from opi.utils.age import encrypt_age_content_sync

        if not isinstance(component, dict):
            return
        user_env_vars = component.get("user-env-vars")
        if not user_env_vars or not isinstance(user_env_vars, str):
            return
        if "BEGIN AGE ENCRYPTED FILE" in user_env_vars:
            return
        component["user-env-vars"] = LiteralScalarString(encrypt_age_content_sync(user_env_vars, public_key))
        logger.debug("Encrypted user-env-vars for component %s", label or "unknown")


class ComponentAliasesEncryptGenerator:
    """Encrypt each component alias value with the project's AGE public key.

    Aliases are stored as a ``name -> value`` map; values may hold secrets
    (e.g. a password), so each value is encrypted independently while the
    alias names stay readable. Skips values that are already AGE-encrypted.

    Must run after ``AGEKeyPairGenerator`` so the project public key exists.
    Uses a ``_generated`` path - the return value is discarded during cleanup.
    """

    def generate(self, yaml_data: dict[str, Any]) -> Any:
        from ruamel.yaml.scalarstring import LiteralScalarString

        from opi.utils.age import encrypt_age_content_sync

        public_key = yaml_data.get("config", {}).get("age-public-key")
        if not public_key:
            logger.debug("No project public key available, skipping aliases encryption")
            return True

        for component in yaml_data.get("components", []):
            if not isinstance(component, dict):
                continue
            aliases = component.get("aliases")
            if not isinstance(aliases, dict):
                continue
            for alias_name, alias_value in aliases.items():
                if not isinstance(alias_value, str) or "BEGIN AGE ENCRYPTED FILE" in alias_value:
                    continue
                aliases[alias_name] = LiteralScalarString(encrypt_age_content_sync(alias_value, public_key))
                logger.debug(
                    "Encrypted alias '%s' for component %s",
                    alias_name,
                    component.get("name", "unknown"),
                )

        return True
