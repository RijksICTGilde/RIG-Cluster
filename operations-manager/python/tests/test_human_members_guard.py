"""Wie ``users`` leest moet weten dat er service accounts in staan.

WAAROM DEZE TEST BESTAAT

``users`` draagt sinds schemaversie 2.12 twee soorten lid. Alles wat met een MENS gebeurt
(een Keycloak-gebruiker, een uitnodiging, een mail, OTP, de inloglijst) hoort alleen de
mensen te zien. Een lezer die dat vergeet geeft een service account een postvak dat niet
bestaat, of zet hem op de inloglijst, waar hij een tweede factor zou moeten hebben die hij
niet heeft.

Dat valt niet op in een test van die lezer zelf, want zonder service account in de fixture
werkt hij gewoon. Daarom deze lijst: elk bestand dat ``users`` leest staat erin, met wat
het ermee doet. Komt er een lezer bij, dan faalt deze test en moet de schrijver kiezen:
alle leden (een bewijsmiddel, een rol, een telling) of alleen de mensen
(:func:`opi.services.project_leden.human_members`).
"""

from __future__ import annotations

import re
from pathlib import Path

OPI = Path(__file__).resolve().parent.parent / "opi"

#: Hoe een lezer van de ledenlijst eruitziet in de bron. ``project.users``, ``.users or []``
#: en ``data["users"]`` in al hun vormen.
LEZER = re.compile(r"""\.users\b|\[["']users["']\]|\.get\(\s*["']users["']""")

#: De bestanden die de ledenlijst KAAL lezen, met waarom dat daar mag: het gaat om een
#: bewijsmiddel, een rol, een veld in een formulier of een telling, en dan telt een service
#: account gewoon mee. Een bestand dat hier niet in staat en de lijst wel kaal leest is een
#: nieuwe lezer die nog moet kiezen.
BEKENDE_LEZERS: dict[str, str] = {
    "api/principal.py": "zoekt een token bij een lid; beide soorten dragen tokens",
    "core/authz/actions.py": "de naam van de action platform.users, geen ledenlijst",
    "core/leden.py": "vergelijkt de leden en hun rollen voor en na een opslag; beide soorten",
    "bootstrap/keycloak_setup.py": "de users van een Keycloak-sjabloon, niet van een project",
    "forms/i18n.py": "het label van de sectie",
    "forms/models/project.py": "de ledenlijst als formulierveld",
    "forms/wizard/leden.py": "scheidt de mensen van de service accounts voor het teamformulier",
    "forms/models/project_file.py": "het model van de ledenlijst zelf",
    "handlers/keycloak_yaml_handler.py": "de users van een Keycloak-sjabloon",
    "services/api_token_service.py": "rollen en tokens, voor beide soorten",
    "services/persistence/__init__.py": "de ORM-tabel users, niet de ledenlijst",
    "services/project.py": "bouwt de leden uit het bestand; filtert ze niet",
    "services/project_leden.py": "de filter zelf",
    "services/project_service.py": "bouwt de leden, en filtert voor de inloglijst",
    "services/project_store.py": "leest het bestand, en filtert voor de inloglijst",
    "services/schema_migration.py": "zet de projectsleutel om in een lid",
    "services/user_admin_service.py": "de ORM-tabel users, niet de ledenlijst",
    "services/user_otp_service.py": "de ORM-tabel users; OTP hangt aan een mens, niet aan een project",
    "utils/project_utils.py": "schrijft de ledenlijst bij het aanmaken",
    "web/lotc_fixtures.py": "voorbeelddata voor de componentengalerij",
    "web/lotc_switch.py": "de ledenlijst op het scherm",
}

#: De plekken waar iets met een PERSOON gebeurt: mail, inlog, console. Die MOETEN de filter
#: gebruiken, en dit is de controle die faalt als iemand hem eruit haalt.
MENSENLEZERS = (
    # De inloglijst: alleen een mens logt in.
    "services/project_service.py",
    "services/project_store.py",
    # De contactpersoon van een uitnodiging krijgt mail.
    "services/catalog/invite/defaults.py",
    # De databaseconsole opent voor adressen die kunnen inloggen.
    "manager/db_console_manager.py",
    # "Mag deze aanmelding dit project openen": een service account meldt zich nergens aan.
    "services/project_authorization.py",
    # Het Team-blok en zijn bewerkmodaal nodigen mensen uit en sturen ze mail.
    "web/router.py",
)

#: Waaraan te zien is dat een lezer de filter gebruikt.
FILTERNAMEN = ("human_members", "human_emails", "menselijke_leden_uit_data", "LidSoort.SERVICE_ACCOUNT")


def _lezers() -> dict[str, str]:
    """Elk bestand onder ``opi/`` dat de ledenlijst leest, met zijn inhoud."""
    gevonden = {}
    for pad in sorted(OPI.rglob("*.py")):
        tekst = pad.read_text()
        if LEZER.search(tekst):
            gevonden[pad.relative_to(OPI).as_posix()] = tekst
    return gevonden


def test_elke_lezer_van_de_ledenlijst_staat_in_de_lijst() -> None:
    """Een nieuwe lezer moet kiezen: alle leden, of alleen de mensen."""
    nieuw = sorted(set(_lezers()) - set(BEKENDE_LEZERS))
    assert not nieuw, (
        f"deze bestanden lezen 'users' kaal maar staan niet in BEKENDE_LEZERS: {nieuw}. "
        "Kies per bestand of het ALLE leden bedoelt of alleen de mensen "
        "(opi/services/project_leden.py), en zet het erbij."
    )


def test_de_lijst_noemt_geen_bestand_dat_de_ledenlijst_niet_meer_leest() -> None:
    """Anders blijft een vermelding staan voor code die er niet meer is."""
    verdwenen = sorted(set(BEKENDE_LEZERS) - set(_lezers()))
    assert not verdwenen, f"deze bestanden lezen 'users' niet meer: {verdwenen}"


def test_een_mensenlezer_gebruikt_de_filter() -> None:
    """De plekken waar iets met een PERSOON gebeurt mogen de lijst niet kaal lezen.

    Faalt deze test, dan krijgt een service account een postvak of een plek op de
    inloglijst: een toegang zonder mens en zonder tweede factor.
    """
    for bestand in MENSENLEZERS:
        tekst = (OPI / bestand).read_text()
        assert any(naam in tekst for naam in FILTERNAMEN), (
            f"{bestand} doet iets met een persoon maar gebruikt de mensenfilter niet; "
            f"gebruik een van {FILTERNAMEN} uit opi/services/project_leden.py"
        )
