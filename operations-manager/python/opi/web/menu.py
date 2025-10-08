"""
Menu utilities for the web interface.

This module provides functions for generating navigation menu items
that are used across different web routes.
"""

from typing import Any


def get_menu_items(user: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """
    Get the menu items for the navigation bar.

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
        {"label": "Nieuw Project", "link": "/projects/new", "icon": "plus"},
        {"label": "Services", "link": "/services", "icon": "server"},
        {"label": "Architecture", "link": "/architecture", "icon": "info"},
        {"label": "API Docs", "link": "/docs", "icon": "computercode"},
    ]

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
