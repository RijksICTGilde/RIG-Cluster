"""The secret files a manifest round writes: their names, labels and previous contents."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from opi.generation.manifests import CONFIG_HASH_IGNORE_LABEL_KEY, CONFIG_HASH_IGNORE_LABEL_VALUE
from opi.utils.sops import decrypt_sops_with_key
from opi.utils.yaml_util import YAMLError, load_yaml_safe

if TYPE_CHECKING:
    from opi.services.catalog.base import SecretFileSpec

logger = logging.getLogger(__name__)


def attachment_secret_prefix(deployment_name: str) -> str:
    """Prefix of the Secrets that carry the file-mode attachments of a deployment."""
    return f"{deployment_name}-attch-"


def existing_secret_pairs(sops_path: str, private_key: str | None) -> dict[str, str]:
    """The ``stringData`` of a previously written SOPS secret, empty on any doubt."""
    if private_key is None or not os.path.exists(sops_path):
        return {}
    decrypted = decrypt_sops_with_key(sops_path, private_key)
    if decrypted is None:
        return {}
    try:
        doc = load_yaml_safe(decrypted)
    except YAMLError:
        logger.info(f"Could not parse decrypted {os.path.basename(sops_path)}, writing fresh values")
        return {}
    string_data = doc.get("stringData") if isinstance(doc, dict) else None
    if not isinstance(string_data, dict):
        return {}
    return {key: value for key, value in string_data.items() if isinstance(value, str)}


def secret_labels_for(spec: SecretFileSpec) -> dict[str, str]:
    """The Secret's metadata labels, including the config-hash opt-out when it applies.

    A service declares ``include_in_config_hash=False`` when only its own auxiliary pod
    reads the secret; translating that into the label happens here, once, for every
    secret the manager writes.
    """
    labels = dict(spec.secret_labels or {})
    if not spec.include_in_config_hash:
        labels[CONFIG_HASH_IGNORE_LABEL_KEY] = CONFIG_HASH_IGNORE_LABEL_VALUE
    return labels
