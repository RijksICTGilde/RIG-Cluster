"""Every icon named in the main menu must exist in the ROOS icon set.

A ROOS icon name that does not exist renders as a broken icon: nothing raises,
nothing logs, the menu item just looks wrong. "Domeinen" carried ``link`` for an
unknown length of time before someone noticed by eye.

The icons ship inside the installed ``jinja_roos_components`` package (1163 of
them, in category subdirectories), so this checks against the same files the
renderer uses rather than against a hand-copied list that would drift.
"""

from __future__ import annotations

import pathlib
from unittest.mock import MagicMock, patch

import jinja_roos_components
import pytest
from opi.web.menu import get_menu_items

_ICON_ROOT = pathlib.Path(jinja_roos_components.__file__).parent

_USER = {"email": "someone@example.com"}


def _available_icons() -> set[str]:
    return {path.stem for path in _ICON_ROOT.rglob("*/icons/**/*.svg")}


def _menu_icons() -> set[str]:
    """Icon names across every menu variant.

    Admin status is derived from the user's email via ``get_user_service()``, not
    passed in, so reaching the admin entries (Domeinen among them, the one that
    was broken) means stubbing that lookup. An anonymous render alone would miss
    exactly the item this test exists for.
    """
    icons: set[str] = set()
    for is_admin in (False, True):
        service = MagicMock()
        service.is_platform_admin.return_value = is_admin
        with patch("opi.web.menu.get_user_service", return_value=service):
            for user in (None, _USER):
                icons.update(item["icon"] for item in get_menu_items(user=user) if item.get("icon"))
    return icons


def test_the_icon_set_is_actually_found() -> None:
    """Guard the guard: an empty set would make every assertion below vacuous."""
    assert len(_available_icons()) > 500


@pytest.mark.parametrize("icon", sorted(_menu_icons()))
def test_menu_icon_exists_in_the_roos_icon_set(icon: str) -> None:
    assert icon in _available_icons(), (
        f"menu icon {icon!r} does not exist in the ROOS icon set; "
        f"it renders broken without any error. Pick an existing name."
    )


def test_the_replaced_icon_stays_gone() -> None:
    """``link`` was the broken one. It has no file, so it must never come back."""
    assert "link" not in _available_icons()
