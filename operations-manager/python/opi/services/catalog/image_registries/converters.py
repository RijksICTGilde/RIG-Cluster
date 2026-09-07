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
from opi.utils.age import carries_encrypted_value, decrypt_password_smart_sync, encrypt_age_content_sync

logger = logging.getLogger(__name__)


class ProjectAgeSecretConverter:
    """Leest een opgeslagen geheim als leesbare tekst en schrijft het terug als AGE-blok.

    De leeskant kent alle vormen die het veld mag dragen (armored blok, ``base64+age:``,
    ``plain:``), de schrijfkant maakt er een armored blok van. Een vorm die de leeskant
    niet herkent zou als cijfertekst op het scherm komen en bij de eerstvolgende opslag als
    NIEUW token versleuteld worden -- weg echte token.
    """

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> str:
        if not isinstance(value, str) or not value:
            return ""
        if not carries_encrypted_value(value):
            # Niet versleuteld, maar wel mogelijk een opslagVORM: ``plain:`` markeert een
            # bewust leesbaar token en die prefix hoort niet op het scherm en niet in de
            # nieuwe cijfertekst. ``decrypt_password_smart_sync`` haalt hem eraf en laat
            # kale tekst met rust; zonder sleutel kan hij op deze vormen niet falen.
            return decrypt_password_smart_sync(value, None)
        private_key = resolve_project_private_key(context_data)
        if private_key:
            try:
                return decrypt_password_smart_sync(value, private_key)
            except ValueError:
                logger.warning("[ProjectAgeSecretConverter] Token niet te ontsleutelen met de projectsleutel")
        # NIET "" teruggeven. Het formulier zou dan een leeg tokenveld tonen, de gebruiker
        # slaat op zonder het aan te raken, en de schrijfkant leest die lege waarde als
        # "gewist" -- weg token, zonder dat iemand daarom vroeg. De opgeslagen waarde
        # ongewijzigd teruggeven is lelijk op het scherm maar eerlijk: de schrijfkant ziet
        # dat het een versleutelde vorm is en laat hem staan, en overschrijven kan gewoon.
        logger.warning("[ProjectAgeSecretConverter] Token niet te ontsleutelen; de opgeslagen waarde blijft staan")
        return value

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return ""
        # Beide versleutelde vormen, niet alleen het armored blok: een ``base64+age:``
        # waarde die hier als NIEUW token opnieuw versleuteld wordt, maakt het echte token
        # onbereikbaar -- precies wat de comment in ``read()`` probeert te voorkomen.
        if carries_encrypted_value(text):
            return value
        public_key = (context_data or {}).get("config", {}).get("age-public-key")
        if not public_key:
            logger.warning("[ProjectAgeSecretConverter] Geen project-publieke sleutel; token blijft leesbaar")
            return text
        return LiteralScalarString(encrypt_age_content_sync(text, public_key))

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> str:
        return "Versleuteld opgeslagen" if value else "Niet ingevuld"
