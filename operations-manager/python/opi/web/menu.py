"""
Menu utilities for the web interface.

This module provides functions for generating navigation menu items
that are used across different web routes.
"""

import logging
from typing import Any

from opi.services.user_service import get_user_service

logger = logging.getLogger(__name__)


def get_menu_items(user: dict[str, Any] | None = None, **_kwargs: Any) -> list[dict[str, str]]:
    """
    Get the menu items for the navigation bar.

    Admin status is determined automatically from the user's email.

    Args:
        user: User information dictionary from session (optional)

    Returns:
        List of menu item dictionaries with label, link, icon, and optional align
    """
    # Determine the user label for the account menu item
    if user and user.get("name"):
        user_label = user["name"]
    elif user and user.get("email"):
        # Fallback to email if no name available
        user_label = user["email"]
    else:
        # Fallback to default text if no user info
        user_label = "Mijn Account"

    menu_items = [
        {"label": "Dashboard", "link": "/dashboard", "icon": "home"},
        {"label": "Projecten", "link": "/projects", "icon": "applicatie"},
        {"label": "Nieuw Project", "link": "/forms/wizard/restart", "icon": "plus"},
        {"label": "Services", "link": "/services", "icon": "server"},
        # De architectuurpagina staat NIET in de navigatie. Hij is 2000 regels lang en
        # leunde op RVO-opmaakklassen die het nieuwe thema niet kent: de inhoud staat er
        # wel - koppen, tekst en alle acht diagrammen - maar het leest als een lange platte
        # lap. Op verzoek uit het menu tot hij iets toevoegt. De pagina zelf blijft
        # bereikbaar via /architecture en via de voettekst, dus er gaat niets verloren.
        {"label": "CLI", "link": "/cli", "icon": "computercode"},
        {"label": "Actions", "link": "/actions", "icon": "applicatie"},
        {"label": "API Docs", "link": "/docs", "icon": "computercode"},
    ]

    # Auto-detect admin status from user email
    is_admin = False
    if user and user.get("email"):
        try:
            is_admin = get_user_service().is_platform_admin(user["email"].lower())
        except Exception:
            logger.debug("Could not check admin status for menu", exc_info=True)

    if is_admin:
        # Metrics stond in de algemene lijst, maar de verkenner kijkt over projecten heen
        # en hoort daarmee bij het beheerdersgereedschap, niet bij wat elke gebruiker ziet.
        menu_items.append({"label": "Metrics", "link": "/metrics-explorer", "icon": "grafiek"})
        menu_items.append({"label": "Gebruikersbeheer", "link": "/admin/users", "icon": "user"})
        menu_items.append({"label": "Gebruik & Kosten", "link": "/admin/usage", "icon": "grafiek"})
        menu_items.append({"label": "Domeinen", "link": "/admin/approvals", "icon": "wereldbol"})

    # Add user-specific menu items
    if user:
        menu_items.extend(
            [
                {"label": user_label, "link": "/account", "icon": "user", "align": "right"},
                {"label": "Uitloggen", "link": "/auth/logout", "icon": "uitgang", "align": "right"},
            ]
        )
    else:
        menu_items.append({"label": "Inloggen", "link": "/auth/login", "icon": "sleutel", "align": "right"})

    return menu_items
