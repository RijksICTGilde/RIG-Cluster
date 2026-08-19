"""Tests voor scripts/mail_identity_check.py, de identiteitstoets van de mailrelay.

Getoetst wordt de enige regel in dat script die niet rechtstreeks uit een gemeten waarde
volgt: hoe de Received-keten wordt beoordeeld. De rest van het script vergelijkt letterlijk
adressen die het zelf verstuurd heeft, en heeft een draaiende relay nodig.

De valkuil die deze test vastlegt: de ONTVANGENDE server zet zelf een Received-regel, na
het moment dat de relay het bericht uit handen geeft. Een toets die op de kale aanwezigheid
van Received afgaat, keurt daarom ook een relay af die precies doet wat hij moet doen.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from mail_identity_check import received_fouten  # noqa: E402

#: Zoals de sink hem schrijft, gemeten op de sandbox op 19 augustus 2026.
ONTVANGER_REGEL = (
    "from rig-mail-relay (10-244-0-33.rig-mail-relay.rig-ron.svc.cluster.local. "
    "[10.244.0.33]) by rig-mail-sink-76c44cc489-r48bs (Mailpit) with SMTP "
    "for <ontvanger@example.org>; Wed, 19 Aug 2026 15:12:37 +0000 (UTC)"
)

#: De hop die de relay hoort weg te halen: hiermee kwam de applicatie binnen.
INZENDER_REGEL = "from [127.0.0.1] by rig-mail-relay with ESMTPSA; Wed, 19 Aug 2026 15:12:37 +0000"


def test_alleen_de_regel_van_de_ontvanger_is_goed() -> None:
    """Precies een Received: die van de ontvangende server. Dat is de goede toestand."""
    assert received_fouten([ONTVANGER_REGEL]) == []


def test_doorgegeven_keten_valt_op() -> None:
    """Geeft de relay zijn eigen hop door, dan staan er twee en faalt de toets."""
    fouten = received_fouten([INZENDER_REGEL, ONTVANGER_REGEL])

    assert len(fouten) == 1
    assert "2" in fouten[0]


def test_helemaal_geen_received_valt_ook_op() -> None:
    """Geen enkele Received betekent dat er niet gemeten is, niet dat het goed ging."""
    fouten = received_fouten([])

    assert len(fouten) == 1
    assert "0" in fouten[0]
