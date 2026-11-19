"""
Menu utilities for the web interface.

This module provides functions for generating navigation menu items
that are used across different web routes.
"""

import logging
from typing import Any

from opi.core.config import settings
from opi.services.step_up import is_elevated_admin
from opi.services.user_service import get_user_service

logger = logging.getLogger(__name__)


def get_menu_items(user: dict[str, Any] | None = None, **_kwargs: Any) -> list[dict[str, str]]:  # noqa: ANN401  # extra context a caller may pass
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
        # In the main menu: whoever is logged in never passes "/" again (that goes to the
        # dashboard), and this is the way to the explanation about the platform.
        {"label": "Introductie", "link": "/introductie", "icon": "puzzel"},
        {"label": "Getting started", "link": "/getting-started", "icon": "uitvoering"},
        # The map of the platform: what runs and what ZAD talks to.
        {"label": "Platform", "link": "/platform", "icon": "wolk"},
        {"label": "CLI", "link": "/cli", "icon": "computercode"},
        {"label": "Actions", "link": "/actions", "icon": "applicatie"},
        {"label": "API Docs", "link": "/docs", "icon": "computercode"},
        # Under the API docs and not with Introductie: reference material for whoever
        # points their own domain here, the same kind of reader as the API documentation.
        {"label": "Eigen domein", "link": "/eigen-domein", "icon": "wereldbol"},
        # For everyone: whoever gets or hands out a role reads here what it opens.
        {"label": "Rollen en rechten", "link": "/rollen", "icon": "user"},
        # In the sidebar and not in a page footer: the NLDD application pattern has no footer.
        {"label": "Beveiligingsbeleid", "link": "/security", "icon": "schild-met-vinkje-erop"},
    ]

    # Auto-detect admin status from user email
    is_admin = False
    if user and user.get("email"):
        try:
            is_admin = is_elevated_admin(get_user_service(), user["email"].lower())
        except Exception:  # noqa: BLE001  # the menu must still render
            logger.debug("Could not check admin status for menu", exc_info=True)

    if is_admin:
        menu_items.append({"label": "Metrics", "link": "/metrics-explorer", "icon": "grafiek"})
        menu_items.append({"label": "Gebruikersbeheer", "link": "/admin/users", "icon": "user"})
        menu_items.append({"label": "Gebruik & Kosten", "link": "/admin/usage", "icon": "grafiek"})
        # "Aanvragen" and not "Domeinen": behind this link sits every approval a service
        # declares, and since send-email that is more than one.
        menu_items.append({"label": "Aanvragen", "link": "/admin/approvals", "icon": "vinkje"})
        menu_items.append({"label": "Services status", "link": "/admin/diensten", "icon": "weegschaal"})
        if settings.SSO_OUTAGE_ENABLED:
            menu_items.append({"label": "Platformacties", "link": "/admin/platform", "icon": "schild-met-vinkje-erop"})

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
