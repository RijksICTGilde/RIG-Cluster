"""De navigatiestructuur van de LOTC-bouwlijn, naar het voorbeeld van bg.rijks.app.

Dit bestand is met opzet het enige plek waar de INDELING van de navigatie staat.
De omzetting naar LOTC verandert namelijk niet alleen hoe componenten heten, maar
ook hoe de navigatie is opgebouwd:

    nu (roos)          een platte balk van elf items, alles op een niveau
    bg.rijks.app       een hulpbalk in de header (account, zoeken) plus een
                       zijkolom met gegroepeerde hoofdnavigatie

Dat is een ontwerpkeuze, geen vertaling, en hij zal nog schuiven. Daarom staat hij
hier los van de templates: wie de indeling wil veranderen, verandert GROUPS en raakt
geen enkel template aan.

De bron van de items blijft opi/web/menu.py. Dat is bewust: daar zitten de links en
de rolafhankelijkheid (welke items een beheerder wel ziet en een gebruiker niet), en
die logica mag niet verdubbelen. Dit bestand herschikt alleen wat daar uit komt.
"""

import logging
from typing import Any

from opi.web.menu import get_menu_items
from opi.web.nldd_iconen import nldd_icon_names

logger = logging.getLogger(__name__)

# Onze iconen dragen Nederlandse ROOS-namen; NLDD heeft een eigen, Engelse
# woordenschat van ongeveer vijftig iconen. LOTC vertaalt een handvol namen zelf
# (home -> house, info -> info-circle) en geeft de rest ongewijzigd door, waarna
# NLDD ze niet herkent en er niets verschijnt.
#
# Deze tabel dekt het gat. Alleen namen die AAN DE NLDD-KANT BESTAAN staan rechts;
# een gok zou hetzelfde lege icoon opleveren als geen vertaling, maar dan onzichtbaar
# in plaats van meetbaar. Wat hier ontbreekt is een echt gat en wordt als zodanig
# gerapporteerd door tests/test_lotc_icon_mapping.py.
ROOS_TO_NLDD_ICONS = {
    "applicatie": "rectangle-stack",
    "beveiligingsscan": "shield-check-mark",
    "bewerken": "pencil-on-square",
    "envelop": "envelope",
    "groep-3-personen": "person-2",
    "kalender": "calendar",
    "computercode": "chevron-left-forward-slash-chevron-right",
    "database": "database",
    # De delta's zijn driehoekjes; caret is de NLDD-driehoek.
    "delta-naar-links": "caret-left",
    "delta-naar-rechts": "caret-right",
    "delta-omlaag": "caret-down",
    # square-and-arrow-down, niet square-arrow-down. Die tweede naam staat in de
    # iconenlijst van LOTC maar zit NIET in de bundel die de browser laadt, dus hij
    # rendeerde leeg. Gemeten in een browser, niet uit de lijst gelezen.
    "downloaden": "square-and-arrow-down",
    # Uitloggen. Stond hier niet, dus het menu-item droeg een lege plek; zichtbaar werd
    # dat pas toen de icoontoets ook het MENU ging meten (RC-67).
    "uitgang": "arrow-right-out-bucket",
    "document-blanco": "file-text",
    "externe-link": "link",
    "foutmelding": "exclamation-triangle",
    "grafiek": "chart-x-y-axis-line",
    "informatie-op-internet": "globe",
    "instellingen": "gear",
    # netwerk heeft geen eigen icoon; link is het dichtstbijzijnde dat het idee van
    # verbinding draagt.
    "netwerk": "link",
    "klok": "timer",
    "kruis": "dismiss",
    # De preset "Lokale ontwikkeling" (configs/presets/keycloak-config.yaml) droeg
    # "laptop"; NLDD heeft geen laptop, wel een werkplek met een scherm.
    "laptop": "desk-with-screen",
    # folder-stack MAG HIER NIET STAAN, hoe goed hij ook past. Die naam bestaat in de
    # NLDD-bundel, maar LOTC heeft er in icons.json een alias van gemaakt die hem naar
    # ``folder-on-folder`` herschrijft, en DIE zit niet in de bundel. Het icoon rendeerde
    # dus leeg terwijl elke poort groen stond: de poort mat de naam die wij MEEGEVEN en
    # niet de naam die er na de aliaslaag van LOTC uit komt. De toets meet nu het
    # gerenderde ``name=`` (tests/test_lotc_icon_mapping.py).
    "map": "folder",
    # De raket stond op de wizardstap "Deployments" en rendeerde leeg. NLDD heeft geen
    # raket; cylinder-split is het icoon dat een deployment hier overal draagt (zie
    # "server" hieronder), dus de stap krijgt hetzelfde beeld als waar hij over gaat.
    "raket": "cylinder-split",
    # "Job uitvoeren" (postgres_pages.py): iets in gang zetten.
    "uitvoering": "play",
    "wolk": "cloud",
    "pijl-naar-rechts": "chevron-right",
    "publicatie": "file-text",
    "puzzel": "puzzle-piece",
    "refresh": "arrow-2-counter-clockwise",
    "schild-met-vinkje-erop": "shield-check-mark",
    "server": "cylinder-split",
    "sleutel": "lock-closed",
    # NLDD heeft geen stethoscoop. Het hart is hier geen versiering maar dezelfde
    # betekenis: deze twee diensten (health-check, deployment-health) gaan over de
    # gezondheid van een deployment. Zonder afbeelding bleef de kaart leeg, en dat is
    # zichtbaar minder dan wat het origineel toont.
    "stethoscoop": "heart",
    "terug": "arrow-u-turn-backward",
    "user": "person",
    "verwijderen": "trash",
    "vinkje": "check-mark-circle",
    "vraagteken": "question-mark-circle",
    "wachtend-persoon": "person",
    "waarschuwing": "exclamation-triangle",
    "wereldbol": "globe",
    "zandloper": "timer",
    "zoek": "search",
    # Geen letterlijke tegenhanger in NLDD, wel een die hetzelfde ZEGT:
    # de slaapstand van een deployment (een maan), en de hulppagina over resources
    # (een meter). Ze stonden hiervoor in KNOWN_GAPS en renderden dus als niets.
    "uit-aanknop": "moon",
    "weegschaal": "score-meter",
}

# De indeling van de zijkolom: een kopje met de links die eronder horen. Links die
# hier niet genoemd worden, komen ongegroepeerd bovenaan - zo valt een nieuw menu-item
# niet stilzwijgend weg maar staat het meteen ergens.
GROUPS: list[tuple[str, list[str]]] = [
    ("Bouwen & draaien", ["/forms/wizard/restart", "/services"]),
    # /introductie als eerste van Platform: het is de uitleg OVER het platform, en zonder
    # groep viel hij bovenaan bij Dashboard en Mijn projecten, waar hij niet hoort.
    ("Platform", ["/introductie", "/cli", "/actions", "/docs"]),
    # /metrics-explorer stond onder "Bouwen & draaien" en hoort hier: het is een
    # beheerpagina over het hele platform, niet iets dat je gebruikt om je eigen project
    # te bouwen. Het menu-item zelf is al alleen voor een platformbeheerder zichtbaar
    # (menu.py, achter is_admin), dus deze groep zegt hetzelfde als wie hem ziet.
    ("Beheer", ["/metrics-explorer", "/admin/users", "/admin/usage", "/admin/approvals"]),
]


def to_nldd_icon(roos_icon: str) -> str:
    """Vertaal een ROOS-iconnaam naar de NLDD-woordenschat.

    Onbekende namen gaan ongewijzigd door - LOTC kent er zelf een aantal, en een naam die
    NLDD wel kent moet hier gewoon doorheen kunnen - maar ze gaan niet langer STIL door.

    Dat stille doorlaten was de fout. De redenering erachter klopte half: een verkeerd
    icoon tonen is inderdaad erger dan een lege plek. Maar niets zeggen is de slechtste
    van de drie, want dan houdt de knop ruimte vrij voor een icoon dat nooit komt en hoort
    niemand er iets over. Zo stonden er maandenlang 37 lege plekken in de interface,
    waaronder de bewerk- en verwijderknop, tot iemand toevallig goed keek.

    De harde poort staat in tests/test_lotc_icon_mapping.py: die loopt elke iconnaam in
    elk sjabloon en in elke dienstdefinitie langs en faalt op een naam die de geleverde
    NLDD-bundel niet kent. Daar hoort hij, want daar breekt hij niets in productie en
    valt het op voordat het uitgerold is. Deze logregel is de vangnet eronder, voor namen
    die pas tijdens het draaien ontstaan (een dienst die zijn icoon uit gegevens haalt).
    """
    vertaald = ROOS_TO_NLDD_ICONS.get(roos_icon, roos_icon)
    bekend = nldd_icon_names()
    if roos_icon and bekend and vertaald not in bekend:
        logger.warning(
            "Iconnaam %r levert geen icoon op (na vertaling: %r). Hij rendeert als een lege "
            "plek zonder foutmelding. Kies een naam die NLDD levert of leg de afbeelding in "
            "ROOS_TO_NLDD_ICONS.",
            roos_icon,
            vertaald,
        )
    return vertaald


def get_navigation(user: dict[str, Any] | None, current_path: str = "") -> dict[str, Any]:
    """Deel de menu-items op in een hulpbalk en een gegroepeerde zijkolom.

    Args:
        user: de gebruiker uit de sessie, zoals opi/web/menu.py hem verwacht.
        current_path: het pad van de huidige pagina, voor de actieve staat.

    Returns:
        ``utility``: de items die in de header horen (account, in- en uitloggen).
        ``sidebar``: een lijst van (kopje-of-None, items) in weergavevolgorde.
    """
    items: list[dict[str, Any]] = [
        {
            **item,
            "icon": to_nldd_icon(item.get("icon", "")),
            "active": bool(current_path) and item["link"] == current_path,
        }
        for item in get_menu_items(user)
    ]

    # bg zet accountzaken rechtsboven in de header. Onze menu-items dragen daar al
    # een markering voor ("align": "right"), dus die hoeft niet opnieuw bedacht.
    utility = [item for item in items if item.get("align") == "right"]
    main = [item for item in items if item.get("align") != "right"]

    grouped_links = {link for _, links in GROUPS for link in links}
    sidebar: list[tuple[str | None, list[dict[str, Any]]]] = []

    ungrouped = [item for item in main if item["link"] not in grouped_links]
    if ungrouped:
        sidebar.append((None, ungrouped))

    for label, links in GROUPS:
        in_group = [item for item in main if item["link"] in links]
        if in_group:
            sidebar.append((label, in_group))

    # De beheeritems ook los, voor het gebruikersmenu rechtsboven. Ze blijven in de
    # zijkolom staan: dit is een tweede WEG naar dezelfde pagina's, niet een verhuizing.
    # Wie ze uit de zijkolom wil halen moet dat apart besluiten.
    beheer_links = dict(GROUPS)["Beheer"]
    beheer = [item for item in main if item["link"] in beheer_links]

    return {"utility": utility, "sidebar": sidebar, "beheer": beheer}
