"""The navigation structure of the LOTC build line, after the example of bg.rijks.app.

This file is deliberately the only place where the LAYOUT of the navigation sits: a utility
bar in the header (account, search) plus a sidebar with grouped main navigation. Whoever
wants to change the layout changes GROUPS and touches no template.

The source of the items stays opi/http/render/menu.py, which holds the links and the
role dependency; that logic may not be duplicated. This file only rearranges what comes
out of it.
"""

import logging
from typing import Any

from opi.http.render.menu import get_menu_items
from opi.http.render.nldd_iconen import nldd_icon_names

logger = logging.getLogger(__name__)

# Our icons carry Dutch ROOS names; NLDD has its own English vocabulary. LOTC translates a
# handful itself (home -> house) and passes the rest through unchanged, after which NLDD
# does not recognise them and nothing appears.
#
# Only names that EXIST ON THE NLDD SIDE belong on the right: a guess would render the same
# empty icon as no translation, but invisibly instead of measurably. What is missing here is
# a real gap and is reported as such by tests/test_lotc_icon_mapping.py.
ROOS_TO_NLDD_ICONS = {
    "applicatie": "rectangle-stack",
    "beveiligingsscan": "shield-check-mark",
    "bewerken": "pencil-on-square",
    "envelop": "envelope",
    "groep-3-personen": "person-2",
    "kalender": "calendar",
    "computercode": "chevron-left-forward-slash-chevron-right",
    "database": "database",
    # The deltas are little triangles; caret is the NLDD triangle.
    "delta-naar-links": "caret-left",
    "delta-naar-rechts": "caret-right",
    "delta-omlaag": "caret-down",
    # square-arrow-down, not square-and-arrow-down: which of the two names the bundle DRAWS
    # changes per version, and the other renders an empty svg element. Measured in the
    # bundle, not read from the icon list.
    "downloaden": "square-arrow-down",
    # Logging out.
    "uitgang": "arrow-right-out-bucket",
    "document-blanco": "file-text",
    "externe-link": "link",
    "foutmelding": "exclamation-triangle",
    "grafiek": "chart-x-y-axis-line",
    "informatie-op-internet": "globe",
    "instellingen": "gear",
    # netwerk has no icon of its own; link is the closest that carries the idea of a
    # connection.
    "netwerk": "link",
    "klok": "timer",
    "kruis": "dismiss",
    # NLDD has no laptop, but it does have a desk with a screen.
    "laptop": "desk-with-screen",
    # folder-stack MAY NOT APPEAR HERE, however well it fits: LOTC rewrites that name in
    # icons.json to ``folder-on-folder``, which is not in the bundle, so the icon renders
    # empty. The gate measures the rendered ``name=`` (tests/test_lotc_icon_mapping.py).
    "map": "folder",
    # NLDD has no rocket; cylinder-split is the icon a deployment carries everywhere here
    # (see "server" below), so the wizard step gets the same image as its subject.
    "raket": "cylinder-split",
    # "Job uitvoeren" (postgres_pages.py): setting something in motion.
    "uitvoering": "play",
    "wolk": "cloud",
    "pijl-naar-rechts": "chevron-right",
    "publicatie": "file-text",
    "puzzel": "puzzle-piece",
    "refresh": "arrow-2-counter-clockwise",
    "schild-met-vinkje-erop": "shield-check-mark",
    "server": "cylinder-split",
    "sleutel": "lock-closed",
    # NLDD has no stethoscope. The heart carries the same meaning here: health-check and
    # deployment-health are both about the health of a deployment.
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
    # No literal counterpart in NLDD, but one that SAYS the same: sleep mode (a moon) and
    # the help page about resources (a meter).
    "uit-aanknop": "moon",
    "weegschaal": "score-meter",
}

# The layout of the sidebar: a heading with the links that belong under it. Links not named
# here land ungrouped at the top, so a new menu item never silently falls away.
GROUPS: list[tuple[str, list[str]]] = [
    ("Bouwen & draaien", ["/forms/wizard/restart", "/services"]),
    # /introductie first under Platform: it is the explanation ABOUT the platform.
    (
        "Platform",
        [
            "/introductie",
            "/getting-started",
            "/platform",
            "/cli",
            "/actions",
            "/docs",
            "/eigen-domein",
            "/rollen",
            "/security",
        ],
    ),
    # /metrics-explorer belongs here: an admin page about the whole platform. The menu item
    # itself is already visible only to a platform admin (menu.py, behind is_admin).
    (
        "Beheer",
        ["/metrics-explorer", "/admin/users", "/admin/usage", "/admin/approvals", "/admin/diensten"],
    ),
]


def to_nldd_icon(roos_icon: str) -> str:
    """Translate a ROOS icon name into the NLDD vocabulary.

    Unknown names pass through unchanged, because LOTC knows a number of them itself and a
    name NLDD does know has to be able to get through, but they no longer pass SILENTLY: an
    icon that never arrives still holds its space in the button and nobody hears about it.

    The hard gate is tests/test_lotc_icon_mapping.py, which walks every icon name in every
    template and every service definition. This log line is the safety net under it, for
    names that only come into being at runtime (a service that takes its icon from data).
    """
    translated = ROOS_TO_NLDD_ICONS.get(roos_icon, roos_icon)
    known = nldd_icon_names()
    if roos_icon and known and translated not in known:
        logger.warning(
            "Iconnaam %r levert geen icoon op (na vertaling: %r). Hij rendeert als een lege "
            "plek zonder foutmelding. Kies een naam die NLDD levert of leg de afbeelding in "
            "ROOS_TO_NLDD_ICONS.",
            roos_icon,
            translated,
        )
    return translated


def get_navigation(user: dict[str, Any] | None, current_path: str = "") -> dict[str, Any]:
    """Split the menu items into a utility bar and a grouped sidebar.

    Args:
        user: the user from the session, as opi/http/render/menu.py expects it.
        current_path: the path of the current page, for the active state.

    Returns:
        ``utility``: the items that belong in the header (account, logging in and out).
        ``sidebar``: a list of (heading-or-None, items) in display order.
    """
    items: list[dict[str, Any]] = [
        {
            **item,
            "icon": to_nldd_icon(item.get("icon", "")),
            "active": bool(current_path) and item["link"] == current_path,
        }
        for item in get_menu_items(user)
    ]

    # bg puts account matters top right in the header, and our menu items already carry a
    # marker for that ("align": "right").
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

    return {"utility": utility, "sidebar": sidebar}
