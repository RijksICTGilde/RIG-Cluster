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
from opi.utils.nldd_iconen import to_nldd_icon

logger = logging.getLogger(__name__)

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
