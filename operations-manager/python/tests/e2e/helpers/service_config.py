"""User-based helpers for a service's detail-page config UI.

These press the real buttons and fill the real fields -- no ``page.evaluate`` shortcuts,
no direct modal-fragment URLs -- so a test exercises exactly what a user does. They are the
reusable pattern every config-owning service should follow:

- the service card on the detail page carries a "Configureer" button that opens the
  service's own config modal (``modal_flow_id``);
- the "Services & Integraties" section's "Bewerken" button opens the services modal, which
  chains to each selected service's config step;
- both modals are the server-driven modal wizard: a step is a form under
  ``#edit-section-inner`` with a submit button ("Volgende"/"Opslaan").

Give a service a config section (``config_form_section``) + ``config_section_id`` +
``modal_flow_id`` and register its ``MODAL_EDIT_<X>_FLOW``, and these helpers work for it
unchanged.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from playwright.sync_api import Error as PlaywrightError

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page

_MODAL = "#edit-section-modal.is-open"
_INNER = "#edit-section-inner"


def open_detail(page: Page, base_url: str, project_name: str) -> None:
    """Open de landingspagina van het project (het tabblad Overzicht).

    Daar staan de blokken die de DIENSTEN zelf leveren (de Keycloak-realm, de
    uitnodigingen). De configuratiekaarten staan op het tabblad Services; de helpers
    hieronder gaan daar zelf naartoe, zodat een test die alleen het overzicht nodig heeft
    niet ergens anders uitkomt.
    """
    page.goto(f"{base_url}/projects/details/{project_name}", wait_until="networkidle", timeout=30000)
    page.wait_for_load_state("networkidle")


def open_services_tab(page: Page) -> None:
    """Ga naar het tabblad Services, waar de dienstkaarten staan.

    Die kaarten stonden op de landingspagina en hebben sinds de opdeling een eigen
    tabblad. De tabbladen zijn gewone links (``<c-tab href=...>`` rendert een ``<a>``),
    dus dit is dezelfde navigatie als een gebruiker doet.
    """
    if "/projects/services/" in page.url:
        return
    page.locator("a[href*='/projects/services/']").first.click()
    page.wait_for_url("**/projects/services/**", timeout=15000)
    page.wait_for_load_state("networkidle")


def service_card(page: Page, service_display_name: str) -> Locator:
    """De kaart van EEN dienst.

    Op ``data-lotc-component='card'`` en niet op de klasse ``.service-detail-card``: die
    hoorde bij de oude sectie en staat niet meer op de pagina die geserveerd wordt.
    ``.last`` omdat het paneel zelf ook een kaart is en de naam van elke dienst bevat -
    de buitenste match is dus het paneel en de binnenste de kaart die we zoeken.
    """
    return page.locator("[data-lotc-component='card']").filter(has_text=service_display_name).last


def open_service_config_modal(page: Page, service_display_name: str) -> None:
    """Click the 'Configureer' button on a service's card, wait for its config modal."""
    open_services_tab(page)
    knop = service_card(page, service_display_name).locator(
        "nldd-button[text='Configureer'], button:has-text('Configureer')"
    )
    knop.first.click()
    page.wait_for_selector(_MODAL, timeout=10000)
    page.wait_for_selector(f"{_INNER} form", timeout=15000)


def open_services_modal(page: Page) -> None:
    """Click the 'Bewerken' button on the Services & Integraties section (services modal)."""
    open_services_tab(page)
    # Zonder tagnaam: de knop is een <nldd-button> en geen <button>, dus "button[onclick]"
    # vond hem niet meer.
    page.locator("[onclick*='modal-edit-services']").first.click()
    page.wait_for_selector(_MODAL, timeout=10000)
    page.wait_for_selector(f"{_INNER} input[name='services[]']", timeout=15000)


def modal_field(page: Page, name_contains: str) -> Locator:
    """A field inside the open modal, matched by a substring of its name."""
    return page.locator(f"{_INNER} [name*='{name_contains}']")


def modal_heading(page: Page) -> str:
    heading = page.locator(f"{_INNER} h1, {_INNER} h2, {_INNER} h3").first
    return (heading.text_content() or "").strip() if heading.count() else ""


def modal_advance_to_field(page: Page, name_contains: str, *, max_steps: int = 8) -> bool:
    """Press 'Volgende' until a field whose name contains ``name_contains`` is on screen.

    After each click it waits for the step to actually swap (the target field appears, or
    the heading changes) before re-checking, so a slow HTMX swap cannot make the loop click
    'Volgende' twice and skip the target step.
    """
    for _ in range(max_steps):
        if modal_field(page, name_contains).count() > 0:
            return True
        submit = page.locator(f"{_INNER} button[type='submit']")
        if submit.count() == 0:
            return False
        before_heading = modal_heading(page)
        submit.last.scroll_into_view_if_needed()
        submit.last.click()
        with contextlib.suppress(PlaywrightError):
            page.wait_for_function(
                """([inner, prev, target]) => {
                    const root = document.querySelector(inner);
                    if (!root) return false;
                    if (root.querySelector(`[name*='${target}']`)) return true;
                    const h = root.querySelector('h1, h2, h3');
                    return !!(h && h.textContent.trim() && h.textContent.trim() !== prev);
                }""",
                arg=[_INNER, before_heading, name_contains],
                timeout=15000,
            )
    return modal_field(page, name_contains).count() > 0


def modal_add_sequence_item(page: Page, add_label: str = "Item toevoegen") -> None:
    """Press a sequence 'add' button inside the open modal and wait for the new row.

    In the detail-edit modal the add button fetches a fresh fragment into ``#edit-section-inner``,
    so we wait for the field count under it to grow rather than for a fixed timeout.
    """
    before = page.locator(f"{_INNER} .rvo-sequence__items > *").count()
    page.locator(f"{_INNER} button:has-text('{add_label}'), {_INNER} a:has-text('{add_label}')").last.click()
    with contextlib.suppress(PlaywrightError):
        page.wait_for_function(
            """([inner, prev]) => {
                const root = document.querySelector(inner);
                if (!root) return false;
                return root.querySelectorAll('.rvo-sequence__items > *').length > prev;
            }""",
            arg=[_INNER, before],
            timeout=15000,
        )


def modal_submit(page: Page, label: str = "Opslaan") -> None:
    """Press the modal's save/submit button and wait for the modal to close."""
    submit = page.locator(f"{_INNER} button[type='submit']:has-text('{label}')")
    target = submit if submit.count() else page.locator(f"{_INNER} button[type='submit']")
    target.last.scroll_into_view_if_needed()
    target.last.click()


def open_deployments_tab(page: Page) -> None:
    """Click the 'Deployments' tab, where the per-deployment action buttons live."""
    page.get_by_text("Deployments", exact=True).first.click()
    page.wait_for_timeout(800)


def deployment_action(page: Page, label: str) -> Locator:
    """A deployment action button by its label (e.g. 'Deployment slapen', 'Applicatie wekken')."""
    return page.locator(f"nldd-button[text='{label}'], button:has-text('{label}'), a:has-text('{label}')")


def click_deployment_action(page: Page, label: str) -> None:
    """Press a deployment action and confirm it in the dialog that opens.

    De bevestiging is een MODAL en geen ``window.confirm``. Hier stond
    ``page.once("dialog", accept)``, en dat wachtte op een native dialoog die nooit komt:
    de actieknop opent de gedeelde dialoog met bg/_action-confirm.html.j2, en pas de knop
    daarin (``.confirm-action-submit``) doet de POST. Gevolg was dat de klik niets deed en
    de test meldde dat de deployment niet ging slapen - terwijl er nooit iets verstuurd is.

    De POST commit naar git en herverwerkt (enkele seconden), dus de aanroeper polt daarna
    de bron van waarheid (het projectbestand) en niet de pagina.

    Blijft de modal weg, dan faalt dit hard. Eerder werd dat weggeslikt en ging de test
    stil verder; de poll op het projectbestand ving het uiteindelijk wel, maar meldde dan
    een minuut later dat de deployment niet ging slapen in plaats van dat er nooit iets
    verstuurd is.
    """
    deployment_action(page, label).first.click()
    bevestig = page.locator(f"{_MODAL} .confirm-action-submit, {_INNER} .confirm-action-submit")
    try:
        bevestig.first.wait_for(state="visible", timeout=10000)
    except PlaywrightError as fout:
        raise AssertionError(
            f"De bevestigingsmodal van '{label}' kwam niet op; de actie is dus nooit verstuurd."
        ) from fout
    bevestig.first.click()
    page.wait_for_timeout(1000)
