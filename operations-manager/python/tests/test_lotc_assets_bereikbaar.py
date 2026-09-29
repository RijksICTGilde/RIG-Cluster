"""Elk bestand dat <c-page> in de <head> zet, is ook echt op te halen.

Een ontbrekend stijlblad of script is een STILLE storing: de pagina laadt, de HTML klopt,
en het scherm ziet er ongestileerd uit of een component doet niets. Er komt geen enkele
fout voorbij, want een 404 op een <link> of <script> stopt het laden niet.

Twee dingen kunnen dat veroorzaken, en dit bestand dekt ze allebei:

1. ``<c-page>`` noemt een bestand dat de route ``/static/lotc/{rel}`` niet kan vinden. Die
   route loopt een LIJST van filesystem-wortels af (een per geinstalleerd pakket), dus of
   een verwijzing uitkomt hangt af van welk pakket hem levert. NLDD 0.8.83 splitste zijn
   bundel op en zette ``lotc-nldd.css`` en ``lotc-nldd.js`` BUITEN ``dist/``; die komen
   dus langs een ander deel van de wortel dan de rest van het thema.
2. De opsomming in ``tests/e2e/test_sandbox_lotc.py`` raakt achterop. Die toetst dat de
   bestanden in de gebouwde IMAGE zitten, maar hij draait alleen met een sandbox. Komt er
   een bestand bij en wordt die lijst niet bijgewerkt, dan bewaakt hij het niet meer.
"""

from __future__ import annotations

import re

from lord_of_the_components.design_system import discover_design_systems
from opi.core.templates_lotc import DESIGN_SYSTEMS, resolve_lotc_static, templates_lotc
from tests.e2e.test_sandbox_lotc import STATIC_ASSETS

#: Wat een gerenderde pagina in haar <head> ophaalt, met het /static/lotc/-voorvoegsel.
_VERWIJZING = re.compile(r'(?:href|src)="/static/lotc/([^"?]+)')


def _verwijzingen_van_een_pagina() -> set[str]:
    html = templates_lotc.env.from_string('<c-page title="meting"><c-paragraph>x</c-paragraph></c-page>').render()
    return set(_VERWIJZING.findall(html))


def test_de_pagina_haalt_alles_op_wat_de_design_systems_opgeven() -> None:
    """De <head> komt uit css_urls/js_urls van elk actief design system.

    Levert een nieuwe versie daar een bestand bij, dan staat het zonder verder werk in de
    <head> - en dus ook zonder dat iemand naar de rest van deze bestanden kijkt.
    """
    verwacht: set[str] = set()
    beschikbaar = discover_design_systems()
    for naam in DESIGN_SYSTEMS:
        ds = beschikbaar[naam]
        for url in (*ds.css_urls, *ds.js_urls):
            if url.startswith("/static/lotc/"):
                verwacht.add(url.removeprefix("/static/lotc/"))

    assert verwacht, "geen enkel design system geeft een css- of js-bestand op"
    assert verwacht <= _verwijzingen_van_een_pagina(), "een design system noemt een bestand dat <c-page> niet opneemt"


def test_elke_verwijzing_uit_de_head_is_op_te_halen() -> None:
    """De route /static/lotc/{rel} moet er een bestand bij vinden, anders is het een 404."""
    ontbreekt = sorted(rel for rel in _verwijzingen_van_een_pagina() if resolve_lotc_static(rel) is None)

    assert ontbreekt == [], (
        "<c-page> verwijst naar bestanden die de route niet kan vinden. Ze komen uit de "
        "css_urls/js_urls van een design system; kijk of het pakket ze wel meelevert en of "
        "zijn staticwortel in LOTC_STATIC_ROOTS staat:\n  " + "\n  ".join(ontbreekt)
    )


def test_de_sandboxlijst_noemt_de_bestanden_die_deze_versie_levert() -> None:
    """De opsomming in de sandboxtest telt alleen mee als hij actueel is.

    Hij draait alleen met E2E_BASE_URL gezet en toetst de gebouwde image, dus een
    vergeten regel valt daar nergens op. Deze test hangt hem aan wat het thema NU levert.
    """
    uit_de_head = _verwijzingen_van_een_pagina()
    genoemd = {pad.removeprefix("/static/lotc/") for pad in STATIC_ASSETS}

    verdwenen = sorted(genoemd - uit_de_head)
    assert verdwenen == [], (
        f"test_sandbox_lotc.STATIC_ASSETS noemt bestanden die geen enkele pagina meer ophaalt: {verdwenen}"
    )

    # nldd/dist/nldd.js is de kernbundel: zonder dat bestand is elk <nldd-*> een leeg
    # element. Hij heeft de bundelherstructurering van 0.8.83 overleefd, en dat hoort
    # gepind te zijn en niet aangenomen.
    assert "nldd/dist/nldd.js" in uit_de_head
    assert resolve_lotc_static("nldd/dist/nldd.js") is not None
