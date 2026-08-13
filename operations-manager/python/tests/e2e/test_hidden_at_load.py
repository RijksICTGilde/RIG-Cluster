"""Wat verborgen hoort te zijn, is verborgen op de gerenderde pagina.

Het verbergen van een blok gaat sinds RC-48 via de klasse ``.is-hidden`` in plaats van
``style="display: none"``. Dat is een cascade-verschil en geen markup-verschil, en daar
zegt de markup dus niets over: ``base.css`` staat in de ``<head>`` en de stylesheet van de
pagina wordt in de body gelinkt, dus later. Zet die stylesheet een ``display`` op hetzelfde
element, dan wint hij bij gelijke specificiteit en verbergt de klasse niets meer -- terwijl
zowel de markup als de CSS los gelezen klopt.

Deze tests lezen daarom ``getComputedStyle(...).display`` uit op de echte pagina. Dat is de
enige plek waar de cascade zichtbaar is.
"""

from typing import TYPE_CHECKING

import pytest
from tests.e2e.helpers.tabs import open_tab

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

DETAIL_URL = "/projects/details/test-project-detail"


def _display(page: Page, selector: str) -> str:
    """De doorgerekende display-waarde van het eerste element dat past."""
    return page.evaluate(
        "sel => { const e = document.querySelector(sel);return e ? getComputedStyle(e).display : 'ONTBREEKT'; }",
        selector,
    )


@pytest.mark.parametrize(
    "selector",
    [
        "#filter-container",  # verschijnt pas bij meer dan tien metrics
        "#loading",
        "#error-msg",
        "#prometheus-frame-container",
    ],
)
def test_metrics_explorer_starts_with_its_blocks_hidden(app_server: str, auth_page: Page, selector: str) -> None:
    """De metrics explorer toont bij het laden alleen zijn bediening."""
    auth_page.goto(f"{app_server}/metrics-explorer")
    auth_page.wait_for_load_state("networkidle")

    assert _display(auth_page, selector) == "none", (
        f"{selector} hoort verborgen te zijn bij het laden; het script zet hem aan. "
        "Staat er een display op dit element in de stylesheet van de pagina?"
    )


def test_the_deployments_tab_renders_one_deployment(app_server: str, auth_page: Page) -> None:
    """Het tabblad Deployments toont EEN deployment, en verbergt de rest niet - die staat
    er niet.

    Hier stond de tegenovergestelde toets: alles behalve de eerste hoorde ``.is-hidden`` te
    zijn. Sinds RC-92 staat de naam in het pad en rendert de server er een, dus wat toen
    verborgen moest zijn, hoort er nu helemaal niet te staan. De fixture heeft er twee
    ('default' en 'tweede'); met een enkele bewijst dit niets.
    """
    auth_page.goto(f"{app_server}{DETAIL_URL}")
    auth_page.wait_for_load_state("networkidle")
    open_tab(auth_page, "deployments")
    auth_page.locator("#tab-deployments").wait_for(state="visible", timeout=5000)

    aanwezig = auth_page.evaluate(
        '() => [...document.querySelectorAll(\'#tab-deployments [id^="deployment-"], '
        "#tab-deployments [data-deployment]')].map(e => e.id || e.dataset.deployment)"
    )
    assert aanwezig, "er staat geen enkel deploymentblok op het tabblad"
    assert not [naam for naam in aanwezig if "tweede" in naam], (
        f"de andere deployment staat er nog steeds bij: {aanwezig}"
    )
