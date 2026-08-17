"""Toetst de LOTC-bouwlijn op een DRAAIENDE sandbox.

Los van de tests die tegen een testserver in het geheugen draaien: die bewijzen dat de
code klopt, niet dat de gebouwde image klopt. Juist daar zit hier het risico, want Lord
of the Components komt uit een git-dependency op een eigen host - als die bij het bouwen
niet bereikbaar is, of de statische bestanden komen niet mee in de image, dan merk je dat
pas op een draaiend cluster.

Draait alleen met E2E_BASE_URL gezet; zonder slaan deze tests over.
"""

import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.sandbox]

# De bestanden die <c-page> in de <head> zet. Zitten ze niet in de image, dan laadt de
# pagina wel maar ziet hij er ongestileerd uit - en dat gebeurt stil.
STATIC_ASSETS = [
    "/static/lotc/nldd/dist/nldd.js",
    "/static/lotc/layout/layout.css",
    "/static/lotc/app-components.css",
]


def test_lotc_shell_is_served(sandbox_page, sandbox_url: str) -> None:
    """De schil rendert op de sandbox, met echte NLDD-componenten."""
    response = sandbox_page.goto(f"{sandbox_url}/lotc/")
    assert response is not None
    assert response.ok

    sandbox_page.wait_for_load_state("networkidle")
    sandbox_page.wait_for_function(
        "() => document.querySelectorAll('*:not(:defined)').length === 0",
        timeout=15000,
    )

    assert sandbox_page.locator("nldd-top-navigation-bar").count() > 0, "de schil rendert geen NLDD"
    assert sandbox_page.locator(".lotc-unimplemented").count() == 0


def test_lotc_static_assets_are_in_the_image(sandbox_page, sandbox_url: str) -> None:
    """De CSS en JS van de design systems zitten in de gebouwde image.

    Dit is de toets die een testserver niet kan doen: die leest ze van de schijf van de
    ontwikkelmachine.
    """
    for asset in STATIC_ASSETS:
        response = sandbox_page.request.get(f"{sandbox_url}{asset}")
        assert response.ok, f"asset ontbreekt in de image: {asset} -> {response.status}"
