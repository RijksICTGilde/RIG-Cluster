"""Converters voor de dienst ``image-registries``.

De regel zelf staat in ``config_model.py`` en in ``upstream.py``; de converters hieronder
roepen die aan in plaats van hem over te schrijven, zodat het formulier en de API dezelfde
omzetting doen.

Versleuteld met de sleutel van het PROJECT en niet met de systeemsleutel, dus
``AGEEncryptConverter`` (die ``settings.SOPS_AGE_PUBLIC_KEY`` gebruikt) kan dit niet.
"""

from __future__ import annotations

import logging
from typing import Any

from ruamel.yaml.scalarstring import LiteralScalarString

from opi.forms.editables.converters import resolve_project_private_key
from opi.services.catalog.image_registries.upstream import normalize_upstream
from opi.utils.age import carries_encrypted_value, decrypt_password_smart_sync, encrypt_age_content_sync

logger = logging.getLogger(__name__)


class UpstreamConverter:
    """Maakt van wat er geplakt is de upstream die wij nodig hebben.

    De omzetting zelf staat in ``upstream.py`` en hangt als ``BeforeValidator`` aan het
    veld in ``config_model.py``, zodat de API hem ook krijgt. Dit is de schrijfkant van het
    formulier, die dezelfde functie aanroept -- de editable-laag kan niet bij een pydantic
    before-validator, en twee omzettingen naast elkaar zouden uit elkaar lopen.
    """

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> str:
        return value if isinstance(value, str) else ""

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:
        return normalize_upstream(value) if isinstance(value, str) else value

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> str:
        return str(value) if value else "Niet ingevuld"


class ProjectAgeSecretConverter:
    """Leest een opgeslagen geheim als leesbare tekst en schrijft het terug als AGE-blok.

    De leeskant kent alle drie de vormen die het veld mag dragen (armored blok,
    ``base64+age:``, ``plain:``); een onherkende vorm zou bij de volgende opslag als nieuw
    token versleuteld worden.
    """

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> str:
        if not isinstance(value, str) or not value:
            return ""
        if not carries_encrypted_value(value):
            # Haalt een ``plain:``-prefix eraf en laat kale tekst met rust.
            return decrypt_password_smart_sync(value, None)
        private_key = resolve_project_private_key(context_data)
        if private_key:
            try:
                return decrypt_password_smart_sync(value, private_key)
            except ValueError:
                logger.warning("[ProjectAgeSecretConverter] Token niet te ontsleutelen met de projectsleutel")
        # Niet "" teruggeven: de schrijfkant leest een leeg veld als "gewist".
        logger.warning("[ProjectAgeSecretConverter] Token niet te ontsleutelen; de opgeslagen waarde blijft staan")
        return value

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return ""
        # Beide versleutelde vormen, anders wordt een ``base64+age:``-waarde opnieuw
        # versleuteld en is het echte token weg.
        if carries_encrypted_value(text):
            return value
        public_key = (context_data or {}).get("config", {}).get("age-public-key")
        if not public_key:
            logger.warning("[ProjectAgeSecretConverter] Geen project-publieke sleutel; token blijft leesbaar")
            return text
        return LiteralScalarString(encrypt_age_content_sync(text, public_key))

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> str:
        return "Versleuteld opgeslagen" if value else "Niet ingevuld"
