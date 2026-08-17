"""Het zoekveld op /projects houdt zijn breedte als htmx het zoekgebied vervangt.

Gemeld door de eigenaar: na zoeken of sorteren wordt het invoerveld ineens smaller. Dat
is een browserverschijnsel en geen HTML-verschil - de server geeft na een swap exact
dezelfde markup terug - dus het wordt hier in de browser gemeten en niet op de HTTP-laag.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

NA_DE_SWAP_MS = 1500


def _breedtes(page: Page) -> dict:
    """De breedte van de huls en van het echte invoerveld in de schaduwboom."""
    return page.evaluate("""() => {
        const host = document.getElementById('projects-zoekveld');
        if (!host) return {host: null, invoer: null};
        const invoer = host.shadowRoot ? host.shadowRoot.querySelector('input') : null;
        const s = getComputedStyle(host);
        const item = host.closest('nldd-toolbar-item');
        const balk = host.closest('nldd-toolbar');
        return {
            host: Math.round(host.getBoundingClientRect().width),
            invoer: invoer ? Math.round(invoer.getBoundingClientRect().width) : null,
            width_attribuut: host.getAttribute('width'),
            host_stijl: s.width,
            host_inline: host.getAttribute('style'),
            host_flex: `${s.flexGrow}/${s.flexShrink}/${s.flexBasis}`,
            host_display: s.display,
            item_breedte: item ? Math.round(item.getBoundingClientRect().width) : null,
            item_stijl: item ? getComputedStyle(item).width : null,
            balk_breedte: balk ? Math.round(balk.getBoundingClientRect().width) : null,
            balk_klassen: balk ? balk.className : null,
            balk_attrs: balk ? [...balk.attributes].map(a => `${a.name}=${a.value}`).join(' ') : null,
        };
    }""")


def _open_projecten(page: Page, app_server: str) -> None:
    page.goto(f"{app_server}/projects")
    page.wait_for_selector("#projects-zoekveld")
    page.wait_for_function("() => !document.querySelector('nldd-search-field:not(:defined)')")


def test_het_zoekveld_blijft_even_breed_na_de_swap(auth_page: Page, app_server: str) -> None:
    """Even breed voor en na, want de markup is voor en na identiek."""
    _open_projecten(auth_page, app_server)
    voor = _breedtes(auth_page)

    auth_page.click("#projects-zoekveld")
    auth_page.keyboard.type("te")
    auth_page.wait_for_timeout(NA_DE_SWAP_MS)
    auth_page.wait_for_function("() => !document.querySelector('nldd-search-field:not(:defined)')")
    na = _breedtes(auth_page)

    assert voor["host"] is not None, "het zoekveld stond er voor de swap al niet"
    assert na["host"] is not None, "het zoekveld is na de swap weg"
    assert na["host"] == voor["host"], (
        f"de huls versmalde van {voor['host']}px naar {na['host']}px "
        f"(attribuut voor={voor['width_attribuut']!r} na={na['width_attribuut']!r}, "
        f"berekend voor={voor['host_stijl']} na={na['host_stijl']})"
    )

    # En de OORZAAK, want de breedte hierboven kan ook per ongeluk goed uitkomen: het
    # component leidt --_width af uit zijn width-attribuut, en die afleiding is wat na een
    # swap wegviel. Staat hij er weer, dan heeft het component zijn eigen werk overgedaan.
    assert "--_width" in (na["host_inline"] or ""), (
        f"het component heeft --_width niet opnieuw afgeleid: {na['host_inline']!r}"
    )

    # Het invoerveld BINNEN de huls mag wel smaller worden: zodra er tekst staat verschijnt
    # het wisknopje ernaast. Dat is geen versmalling van het veld maar inhoud erbij.
    assert (na["invoer"] or 0) > 0, "het invoerveld is na de swap weg"
