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

from typing import Any

from opi.web.menu import get_menu_items

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
    "downloaden": "square-and-arrow-down",
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
    "map": "folder-stack",
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
}

# De indeling van de zijkolom: een kopje met de links die eronder horen. Links die
# hier niet genoemd worden, komen ongegroepeerd bovenaan - zo valt een nieuw menu-item
# niet stilzwijgend weg maar staat het meteen ergens.
GROUPS: list[tuple[str, list[str]]] = [
    ("Bouwen & draaien", ["/forms/wizard/restart", "/services", "/metrics-explorer"]),
    ("Platform", ["/architecture", "/docs"]),
    ("Beheer", ["/admin/users", "/admin/usage", "/admin/approvals"]),
]


def to_nldd_icon(roos_icon: str) -> str:
    """Vertaal een ROOS-iconnaam naar de NLDD-woordenschat.

    Onbekende namen gaan ongewijzigd door: LOTC kent er zelf een paar, en voor de
    rest is een lege plek eerlijker dan een willekeurig ander icoon.
    """
    return ROOS_TO_NLDD_ICONS.get(roos_icon, roos_icon)


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
