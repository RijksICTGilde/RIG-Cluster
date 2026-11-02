"""De OTP-kolommen hebben EEN eigenaar: UserOtpService (RC-329).

Een tweede schrijver op ``otp_secret`` of ``otp_confirmed_at`` zou de telling van
mislukte pogingen, de blokkade en het hergebruik van een tijdstap stilletjes omzeilen.
Deze test zoekt in ``opi/`` naar elke plek die zo'n kolom noemt en laat alleen de
eigenaar, het model en de migratie over.
"""

from __future__ import annotations

import re
from pathlib import Path

OPI = Path(__file__).resolve().parents[1] / "opi"

OTP_KOLOMMEN = (
    "otp_secret",
    "otp_key_version",
    "otp_confirmed_at",
    "otp_last_used_step",
    "otp_failed_attempts",
    "otp_locked_until",
)

#: Wie deze kolommen mag noemen, en waarom.
TOEGESTAAN = {
    "services/user_otp_service.py",  # de eigenaar
    "services/persistence/users.py",  # het model
    "core/user_schema.py",  # de schema-as-code spiegel
    "migrations/versions/009_add_user_otp.py",  # de migratie
}

# Met een grens ervoor, anders telt ``totp_secret`` van de Keycloak-realm-admin mee.
PATROON = re.compile(r"(?<![0-9A-Za-z_])(" + "|".join(OTP_KOLOMMEN) + ")")


def test_alleen_de_eigenaar_noemt_de_otp_kolommen() -> None:
    gevonden = {
        str(pad.relative_to(OPI)) for pad in OPI.rglob("*.py") if PATROON.search(pad.read_text(encoding="utf-8"))
    }
    assert gevonden == TOEGESTAAN
