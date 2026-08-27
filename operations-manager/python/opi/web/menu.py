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
        {"label": "Mijn projecten", "link": "/projects", "icon": "applicatie"},
        {"label": "Nieuw Project", "link": "/forms/wizard/restart", "icon": "plus"},
        {"label": "Services overzicht", "link": "/services", "icon": "server"},
        # Ook in het hoofdmenu en niet alleen in de voettekst: wie ingelogd is komt
        # nooit meer langs "/" (dat gaat dan naar het dashboard), en dan was de
        # voettekst de enige weg naar de uitleg over het platform.
        {"label": "Introductie", "link": "/introductie", "icon": "puzzel"},
        {"label": "CLI", "link": "/cli", "icon": "computercode"},
        {"label": "Actions", "link": "/actions", "icon": "applicatie"},
        {"label": "API Docs", "link": "/docs", "icon": "computercode"},
        # Onder de API-docs en niet bij Introductie: dit is naslag voor wie zijn eigen
        # domein hierheen laat wijzen, dezelfde soort lezer als de API-documentatie.
        {"label": "Eigen domein", "link": "/eigen-domein", "icon": "wereldbol"},
    ]

    # Auto-detect admin status from user email
    is_admin = False
    if user and user.get("email"):
        try:
            is_admin = get_user_service().is_platform_admin(user["email"].lower())
        except Exception:
            logger.debug("Could not check admin status for menu", exc_info=True)

    if is_admin:
        menu_items.append({"label": "Metrics", "link": "/metrics-explorer", "icon": "grafiek"})
        menu_items.append({"label": "Gebruikersbeheer", "link": "/admin/users", "icon": "user"})
        menu_items.append({"label": "Gebruik & Kosten", "link": "/admin/usage", "icon": "grafiek"})
        # "Aanvragen" en niet "Domeinen": achter deze link staat elke goedkeuring die een
        # dienst declareert, en dat is er sinds send-email meer dan een.
        menu_items.append({"label": "Aanvragen", "link": "/admin/approvals", "icon": "vinkje"})
        menu_items.append({"label": "Services status", "link": "/admin/diensten", "icon": "weegschaal"})
        # Onderaan het beheerblok, want dit is naslag en geen dagelijks scherm. De
        # pagina zelf zit achter require_platform_admin; dit menu-item verbergt hem
        # alleen, en verbergen is geen grendel.
        menu_items.append({"label": "Toegang", "link": "/admin/toegang", "icon": "sleutel"})

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
