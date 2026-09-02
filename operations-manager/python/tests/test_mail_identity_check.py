"""Tests voor scripts/mail_identity_check.py, de identiteitstoets van de mailrelay.

Getoetst worden de twee regels in dat script die niet rechtstreeks uit een gemeten waarde
volgen: hoe de Received-keten wordt beoordeeld, en hoe de envelope tegen de From: wordt
gelegd. De rest van het script vergelijkt letterlijk adressen die het zelf verstuurd heeft,
en heeft een draaiende relay nodig.

De valkuil bij Received: de ONTVANGENDE server zet zelf een Received-regel, na het moment
dat de relay het bericht uit handen geeft. Een toets die op de kale aanwezigheid van
Received afgaat, keurt daarom ook een relay af die precies doet wat hij moet doen.

De valkuil bij de envelope: het script eiste "envelope == From: == een adres", en dat is
strenger dan het ontwerp belooft. De relay leidt de ENVELOPE af uit de accountnaam terwijl
het sieve-script alleen de From: overschrijft, dus een account met een eigen afzenderadres
(zad-keycloak, sinds RC-159) loopt in het LOKALE deel uiteen. Wat DMARC eist is uitlijning
van de DOMEINEN. De oude toets kon voor dat account dus nooit slagen, en een verse relay
had daar niets aan veranderd.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import mail_identity_check  # noqa: E402
from mail_identity_check import _wachtwoord, envelope_fouten, received_fouten  # noqa: E402

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


def test_een_regel_die_niet_van_de_ontvanger_is_valt_op() -> None:
    """Precies een regel is niet genoeg: gaf de relay er een door en schreef de ontvanger
    er geen, dan is het er ook een. Die ene moet aantoonbaar van de ontvanger zijn."""
    fouten = received_fouten([INZENDER_REGEL])

    assert len(fouten) == 1
    assert "niet die van de ontvanger" in fouten[0]


# --- de envelope (RC-159) ---------------------------------------------------------

#: Wat de relay voor een PROJECTaccount doet: From: en envelope zijn hetzelfde adres.
PROJECTADRES = "noreply-rijksapp+ai1-uit@rijksoverheid.nl"

#: Wat de relay voor zad-keycloak doet: een eigen From:-adres uit het sieve-script, en een
#: envelope die uit de accountnaam is afgeleid. Zelfde domein, ander lokaal deel.
KEYCLOAK_FROM = "noreply-inloggen@rijksoverheid.nl"
KEYCLOAK_ENVELOPE = "noreply-rijksapp@rijksoverheid.nl"


def test_een_projectaccount_houdt_een_gelijke_envelope() -> None:
    """Het geval dat er altijd al was, en dat niet mag veranderen."""
    assert envelope_fouten(PROJECTADRES, PROJECTADRES, PROJECTADRES) == []


def test_de_keycloak_afzender_mag_uiteenlopen_in_het_lokale_deel() -> None:
    """Het geval waarvoor de oude toets nooit kon slagen.

    Dit is de reden dat het script een eigen --verwacht-envelope heeft: het verschil is
    bewust en het is DMARC-veilig, want het domein blijft gelijk.
    """
    assert envelope_fouten(KEYCLOAK_ENVELOPE, KEYCLOAK_FROM, KEYCLOAK_ENVELOPE) == []


def test_een_ander_domein_valt_op() -> None:
    """De eis die er wel toe doet. Loopt het domein uiteen, dan lijnt SPF niet meer uit en
    haalt geen enkel bericht nog DMARC - wij ondertekenen niets met DKIM."""
    fouten = envelope_fouten("noreply-rijksapp@elders.example", KEYCLOAK_FROM, "noreply-rijksapp@elders.example")

    assert len(fouten) == 1
    assert "DMARC" in fouten[0]


def test_een_onverwacht_adres_in_hetzelfde_domein_valt_ook_op() -> None:
    """Uitlijning alleen is niet genoeg: het plusdeel in de envelope draagt de bounce, dus
    een envelope die naar een ander account wijst maakt een bounce onherleidbaar."""
    fouten = envelope_fouten("noreply-rijksapp+iemand-anders@rijksoverheid.nl", KEYCLOAK_FROM, KEYCLOAK_ENVELOPE)

    assert len(fouten) == 1
    assert "verwacht" in fouten[0]


# --- het wachtwoord staat niet op de opdrachtregel ---------------------------------


def test_het_wachtwoord_komt_uit_de_omgeving(monkeypatch) -> None:
    """Er is met opzet geen --password. Een wachtwoord op de opdrachtregel staat in
    /proc/<pid>/cmdline voor iedereen op de machine en blijft in de shellgeschiedenis
    staan, en dit script wordt gedraaid met een GEDEELD platformwachtwoord in de hand."""
    monkeypatch.setenv("MAIL_RELAY_PASSWORD", "uit-de-omgeving")

    assert _wachtwoord() == "uit-de-omgeving"


def test_zonder_omgevingsvariabele_wordt_erom_gevraagd(monkeypatch) -> None:
    """De terugval is een prompt en niet een lege tekenreeks: stil doorgaan zonder
    wachtwoord levert een authenticatiefout op de relay op die niets uitlegt."""
    monkeypatch.delenv("MAIL_RELAY_PASSWORD", raising=False)
    monkeypatch.setattr(mail_identity_check.getpass, "getpass", lambda _prompt: "uit-de-prompt")

    assert _wachtwoord() == "uit-de-prompt"
