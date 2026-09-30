"""De selectors in onze eigen JavaScript passen op de markup die het thema oplevert.

Een querySelector die niets vindt is geen fout. Het script draait, de tak wordt
overgeslagen, en het gedrag dat eraan hing is er gewoon niet meer: de rij klapt om terwijl
je op de bediening klikte, of de cursor blijft op ``<body>`` staan na een afkeuring. Er
komt geen melding voorbij, niet in de console en niet in de log.

De omslag naar NLDD 0.8.92 deed dat twee keer:

- ``nldd-list-item-action`` heet sinds 0.8.83 ``nldd-list-item-segment``;
- een themaveld zet zijn ``aria-invalid`` sinds 0.8.84 op het invoerelement BINNEN zijn
  schaduwboom, waar een ``querySelector`` niet komt, en draagt in de light DOM ``invalid``.

De browsermetingen hiervoor staan in ``tests/e2e/test_componentkaart_uitklap.py`` en
``tests/e2e/test_focus_op_de_eerste_fout.py``. Die draaien niet in CI (de pytest-stap
daar staat op ``-m "not e2e"``), dus zonder dit bestand is er geen poort die een volgende
naamswijziging tegenhoudt voordat iemand het op een scherm ziet.

De selector staat hieronder als constante en wordt in dezelfde toets naast het JS-bestand
gelegd. Zonder die helft bewaakt dit bestand een tekst die in de JavaScript allang anders
luidt, en dan is het groen zonder iets te zeggen.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opi.core.templates_lotc import templates_lotc
from opi.web.lotc_router import router as lotc_router
from starlette.middleware.sessions import SessionMiddleware

JS_DIR = Path(__file__).resolve().parent.parent / "static" / "js"

#: De selector waarmee uitklap.js de bediening van een rij overslaat.
BEDIENING_VAN_EEN_RIJ = "nldd-list-item-segment"

#: De twee selectors uit naarDeEersteFout in htmx-formgedrag.js: het veld dat de cursor
#: krijgt, en waar alleen naartoe gescrold wordt als er geen veld te vinden is.
FOUT_VELD = '[invalid], [aria-invalid="true"]'
FOUT_TEKST = "nldd-validation-item, .rvo-form-field__error-text, .lotc-form-field__error-text"


@pytest.fixture(scope="module")
def componententabblad() -> str:
    """Het tabblad met de dienstenlijst, gerenderd zoals de bg-route hem oplevert."""
    app = FastAPI()
    app.include_router(lotc_router)
    app.add_middleware(SessionMiddleware, secret_key="test-only")
    return TestClient(app).get("/lotc/bg/project-tabs?tab=componenten").text


def _js(naam: str) -> str:
    return (JS_DIR / naam).read_text()


def _soep(bron: str, **context: object) -> BeautifulSoup:
    return BeautifulSoup(templates_lotc.env.from_string(bron).render(**context), "html.parser")


def test_uitklap_slaat_de_bediening_van_een_rij_over() -> None:
    """De tagnaam in uitklap.js is de tag die ``<c-list-item-segment>`` oplevert."""
    assert f"closest('{BEDIENING_VAN_EEN_RIJ}')" in _js("uitklap.js"), (
        "uitklap.js wijst de bediening van een rij anders aan; werk deze meting bij"
    )

    soep = _soep('<c-list-item-segment button accessible-label="Uitleg">x</c-list-item-segment>')

    assert soep.select(BEDIENING_VAN_EEN_RIJ), (
        f"c-list-item-segment levert geen {BEDIENING_VAN_EEN_RIJ} op, dus uitklap.js slaat niets "
        f"meer over en de rij klapt om bij een klik op het vraagteken: {soep}"
    )


def test_de_bediening_zit_ook_echt_in_de_pagina_die_uitklap_bedient(componententabblad: str) -> None:
    """Niet alleen het losse component: de dienstenlijst op het componententabblad.

    Daar staat het vraagteken, en daar is de rij uitklapbaar. Rendert die lijst de tag
    niet, dan bewaakt de test hierboven een component dat niemand op die pagina gebruikt.
    """
    soep = BeautifulSoup(componententabblad, "html.parser")

    assert soep.select("nldd-list-item[data-uitklap]"), "geen uitklapbare rij op het componententabblad"
    assert soep.select(BEDIENING_VAN_EEN_RIJ), (
        "het vraagteken bij een dienst is geen list-item-segment meer, dus uitklap.js slaat "
        "de klik erop niet over en de rij klapt eronder weg"
    )


def test_een_fout_veld_is_te_vinden_met_de_selector_uit_htmx_formgedrag() -> None:
    """Het veld dat de cursor hoort te krijgen.

    Drie veldsoorten, want ze dragen hun merkteken elk op een andere tag: een tekstveld op
    ``nldd-text-field``, een keuzelijst op ``nldd-combo-box``, en een los aankruisvakje op
    ``nldd-checkbox-field`` zonder ``nldd-form-field`` eromheen.
    """
    bron = _js("htmx-formgedrag.js")
    assert f"querySelector('{FOUT_VELD}')" in bron, "naarDeEersteFout wijst het foute veld anders aan"

    for tag in ("c-text-input-field", "c-select-field", "c-checkbox-field"):
        soep = _soep(f'<{tag} id="veld" name="veld" label="Naam" error="Dit veld is verplicht"/>')

        assert soep.select(FOUT_VELD), f"{tag}: een foutief veld is niet te vinden, dus de cursor blijft op <body>"


def test_alleen_aria_invalid_vindt_het_themaveld_niet() -> None:
    """De keerzijde, en de reden dat er twee merktekens in die selector staan.

    Hier stond alleen ``[aria-invalid="true"]``. Een themaveld zet dat attribuut binnen
    zijn schaduwboom, dus in de light DOM staat het er niet en vond de selector niets.
    Zonder deze helft leest de test hierboven als "een van de twee is genoeg".
    """
    soep = _soep('<c-text-input-field id="veld" name="veld" label="Naam" error="Dit veld is verplicht"/>')

    assert not soep.select('[aria-invalid="true"]'), (
        "het themaveld draagt aria-invalid nu wel in de light DOM; dan kan het eerste deel "
        "van de selector in htmx-formgedrag.js eruit, en deze test weg"
    )
    assert soep.select("[invalid]")


def test_de_terugval_op_de_foutregel_vindt_de_foutregel() -> None:
    """Zonder fout veld wordt er alleen naartoe gescrold; dan moet de tekst wel te vinden zijn."""
    assert f'querySelector("{FOUT_TEKST}")' in _js("htmx-formgedrag.js"), (
        "de terugval in naarDeEersteFout wijst de foutregel anders aan"
    )

    soep = _soep('<c-text-input-field id="veld" name="veld" label="Naam" error="Dit veld is verplicht"/>')

    assert soep.select(FOUT_TEKST), "de foutregel is niet te vinden, dus er wordt nergens naartoe gescrold"
