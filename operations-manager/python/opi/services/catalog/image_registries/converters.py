"""Converter voor het registry-token.

Het token wordt versleuteld met de sleutel van het PROJECT, niet met de systeemsleutel,
net als ``user-env-vars`` en de andere waarden die een project van zichzelf opgeeft.
``AGEEncryptConverter`` kan dat niet: die gebruikt ``settings.SOPS_AGE_PUBLIC_KEY``.

De vorm is dezelfde als bij ``KeyValueConverter``: het formulier krijgt de leesbare waarde
terug en stuurt hem terug, de schrijfkant versleutelt hem opnieuw, en de processor houdt
het opgeslagen cijfertekstblok als de leesbare inhoud niet veranderd is
(``keep_existing_ciphertext_if_unchanged``) -- anders zou elke opslag het blok in git
herschrijven.
"""

from __future__ import annotations

import logging
from typing import Any

from ruamel.yaml.scalarstring import LiteralScalarString

from opi.forms.editables.converters import resolve_project_private_key
from opi.utils.age import decrypt_age_content_sync, encrypt_age_content_sync

logger = logging.getLogger(__name__)

_AGE_MARKER = "BEGIN AGE ENCRYPTED FILE"


class ProjectAgeSecretConverter:
    """Leest een AGE-blok als leesbare tekst en schrijft leesbare tekst terug als AGE-blok."""

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> str:
        if not isinstance(value, str) or not value:
            return ""
        if _AGE_MARKER not in value:
            return value
        private_key = resolve_project_private_key(context_data)
        decrypted = decrypt_age_content_sync(value, private_key) if private_key else None
        if decrypted is not None:
            return decrypted
        # NIET "" teruggeven. Het formulier zou dan een leeg tokenveld tonen, de gebruiker
        # slaat op zonder het aan te raken, en de schrijfkant leest die lege waarde als
        # "gewist" -- weg token, zonder dat iemand daarom vroeg. Het blok ongewijzigd
        # teruggeven is lelijk op het scherm maar eerlijk: de schrijfkant ziet de
        # AGE-markering en laat hem staan, en overschrijven kan gewoon.
        logger.warning("[ProjectAgeSecretConverter] Token niet te ontsleutelen; het opgeslagen blok blijft staan")
        return value

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return ""
        if _AGE_MARKER in text:
            return value
        public_key = (context_data or {}).get("config", {}).get("age-public-key")
        if not public_key:
            logger.warning("[ProjectAgeSecretConverter] Geen project-publieke sleutel; token blijft leesbaar")
            return text
        return LiteralScalarString(encrypt_age_content_sync(text, public_key))

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> str:
        return "Versleuteld opgeslagen" if value else "Niet ingevuld"
