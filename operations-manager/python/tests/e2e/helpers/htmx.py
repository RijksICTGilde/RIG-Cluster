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
