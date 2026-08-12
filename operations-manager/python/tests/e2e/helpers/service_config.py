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

from opi.web.lotc_switch import project_tab_url
from playwright.sync_api import Error as PlaywrightError

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page

_MODAL = "#edit-section-modal.is-open"
_INNER = "#edit-section-inner"


def open_detail(page: Page, base_url: str, project_name: str) -> None:
    """Open de pagina waar de dienstkaarten staan: het tabblad Services.

    Dat was de landingspagina van het project; sinds de opdeling in tabbladen staan de
    kaarten op hun eigen tabblad, en op de landingspagina staat er dus geen enkele.
    """
    page.goto(f"{base_url}{project_tab_url(project_name, 'services')}", wait_until="networkidle", timeout=30000)
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
    knop = service_card(page, service_display_name).locator(
        "nldd-button[text='Configureer'], button:has-text('Configureer')"
    )
    knop.first.click()
    page.wait_for_selector(_MODAL, timeout=10000)
    page.wait_for_selector(f"{_INNER} form", timeout=15000)


def open_services_modal(page: Page) -> None:
    """Click the 'Bewerken' button on the Services & Integraties section (services modal)."""
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
    return page.locator(f"button:has-text('{label}'), a:has-text('{label}')")


def click_deployment_action(page: Page, label: str) -> None:
    """Accept the confirm dialog and press a deployment action button.

    The button POSTs to the action endpoint, which commits to git and reprocesses -- several
    seconds -- before the page reloads. Callers should poll the source of truth (the project
    file) for the new state rather than trusting a fixed wait here.
    """
    page.once("dialog", lambda dialog: dialog.accept())
    deployment_action(page, label).first.click()
    page.wait_for_timeout(1000)
