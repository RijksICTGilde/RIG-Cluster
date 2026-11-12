"""Converters for the ``image-registries`` service.

The token is encrypted with the PROJECT key, so the key comes from the project file and
never from ``settings.SOPS_AGE_*``.
"""

from __future__ import annotations

import logging
from typing import Any

from ruamel.yaml.scalarstring import LiteralScalarString

from opi.services.catalog.image_registries.upstream import normalize_upstream
from opi.utils.age import (
    AgeEncryptionError,
    carries_encrypted_value,
    decrypt_password_smart_sync,
    encrypt_age_content_sync,
    get_project_public_key,
    project_private_key_or_none,
)

logger = logging.getLogger(__name__)


class UpstreamConverter:
    """Turns whatever was pasted into the upstream we need.

    The same job as the ``BeforeValidator`` in ``config_model.py``: the editable layer
    cannot reach a pydantic before-validator.
    """

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> str:  # noqa: ANN401  # a project-file value
        return value if isinstance(value, str) else ""

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:  # noqa: ANN401  # a project-file value
        return normalize_upstream(value) if isinstance(value, str) else value

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> str:  # noqa: ANN401  # a project-file value
        return str(value) if value else "Niet ingevuld"


class ProjectAgeSecretConverter:
    """Reads a stored secret as readable text and writes it back as an AGE block.

    The read side knows all three shapes the field may carry (armored block,
    ``base64+age:``, ``plain:``); an unrecognised shape would be encrypted as a new token on
    the next save. The write side refuses rather than storing the token readable.
    """

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> str:  # noqa: ANN401  # a project-file value
        if not isinstance(value, str) or not value:
            return ""
        if not carries_encrypted_value(value):
            # Strips a ``plain:`` prefix and leaves bare text alone.
            return decrypt_password_smart_sync(value, None)
        private_key = project_private_key_or_none(context_data)
        if private_key:
            try:
                return decrypt_password_smart_sync(value, private_key)
            except ValueError:
                logger.warning("[ProjectAgeSecretConverter] Token niet te ontsleutelen met de projectsleutel")
        # Not "" : the write side reads an empty field as "cleared".
        logger.warning("[ProjectAgeSecretConverter] Token niet te ontsleutelen; de opgeslagen waarde blijft staan")
        return value

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:  # noqa: ANN401  # a project-file value
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return ""
        # Both encrypted shapes, otherwise a ``base64+age:`` value is encrypted again and
        # the real token is gone.
        if carries_encrypted_value(text):
            return value
        public_key = get_project_public_key(context_data or {})
        if not public_key:
            raise AgeEncryptionError(
                "Het registry-token kan niet worden opgeslagen: het project heeft geen AGE-publieke sleutel."
            )
        return LiteralScalarString(encrypt_age_content_sync(text, public_key))

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> str:  # noqa: ANN401  # a project-file value
        return "Versleuteld opgeslagen" if value else "Niet ingevuld"
