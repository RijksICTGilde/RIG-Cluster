"""Schermafbeeldingen van de bevestigingsdialoog, licht en donker.

Niet een poort maar een kijkje: een groene browsertoets bewijst niet dat een dialoog er
goed uitziet. De bestanden komen in dezelfde map als de andere visuele opnames.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

MAP = Path(__file__).parent / "screenshots"


@pytest.mark.parametrize("weergave", ["light", "dark"])
def test_leg_de_bevestiging_vast(app_server: str, auth_page: Page, weergave: str) -> None:
    auth_page.set_viewport_size({"width": 1280, "height": 900})
    auth_page.context.add_cookies([{"name": "zad_scheme", "value": weergave, "url": app_server}])
    auth_page.goto(f"{app_server}/admin/users")
    auth_page.wait_for_function("() => document.querySelectorAll('*:not(:defined)').length === 0", timeout=15000)

    formulier = auth_page.locator("form[data-bevestig]").first
    formulier.locator("button[type='submit']").first.click()
    auth_page.locator("#bevestiging-dialoog").wait_for(state="visible", timeout=5000)

    MAP.mkdir(parents=True, exist_ok=True)
    auth_page.screenshot(path=str(MAP / f"bevestiging-{weergave}.png"), animations="disabled")
