"""Wachten tot htmx klaar is, gedeeld door de wizard- en de dialooghelper.

Beide klikken door stappen die zichzelf server-side hertekenen, en beide liepen op
hetzelfde: een swap die landt terwijl je typt of klikt. Wie daar doorheen typt raakt zijn
invoer kwijt (het veld wordt vervangen), wie daar doorheen klikt raakt zijn klik kwijt
(de knop wordt vervangen, er vertrekt geen verzoek, en het wachten op de volgende stap
verloopt). Dezelfde oorzaak, twee gezichten, dus een plek.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import Page

#: Zo lang moet het stil zijn voordat we de stap als klaar beschouwen.
QUIET_MS = 400


def wait_for_htmx_quiet(page: Page, quiet_ms: int = QUIET_MS) -> None:
    """Wacht tot er *quiet_ms* lang geen htmx-swap meer is afgerond.

    ``networkidle`` volstaat niet: de swap hangt aan een timer en niet aan een openstaand
    verzoek, dus die keert meteen terug en dan loop je nog steeds voor de swap uit.
    """
    page.evaluate(
        """(quietMs) => new Promise(resolve => {
            let timer = setTimeout(done, quietMs);
            function done() {
                document.removeEventListener('htmx:afterSettle', bump);
                resolve(true);
            }
            function bump() {
                clearTimeout(timer);
                timer = setTimeout(done, quietMs);
            }
            document.addEventListener('htmx:afterSettle', bump);
        })""",
        quiet_ms,
    )


#: Het lui-ladende blok van de backups: een lege <div> die zijn snapshots pas ophaalt als
#: hij in beeld komt.
BACKUPBLOK = "[hx-trigger~='intersect'][hx-get$='/backups']"


def scroll_backupblok_in_beeld(page: Page) -> None:
    """Breng het lui-ladende backupblok in beeld en wacht tot htmx het heeft opgehaald.

    Waarom dit een helper is en geen ``scroll_into_view_if_needed()`` ter plekke: drie
    tests stonden hierop rood zonder dat er iets kapot was. Het blok hangt aan
    ``hx-trigger="intersect once"`` - bewust, want per deployment een verzoek opende
    evenzoveel Kopia-verbindingen - en in een headless venster staat het onder de vouw. Het
    verzoek vertrok dus nooit, de oob-swap landde nooit, en de test wachtte tien seconden
    op een element dat er nooit zou komen.

    Dat is precies het soort rood dat je leert negeren: het gaat niet over de wijziging die
    je maakt, het is elke keer hetzelfde, en de neiging is om het weg te halen. Vandaar hier
    een naam die zegt WAAROM er gescrold wordt, in plaats van een losse regel per test.
    """
    page.locator(BACKUPBLOK).first.scroll_into_view_if_needed()
    wait_for_htmx_quiet(page)
