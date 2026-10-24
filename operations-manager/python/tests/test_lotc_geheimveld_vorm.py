"""De vorm van een geheimveld, waar drie lezers buiten het sjabloon op selecteren.

``<c-secret-field>`` werd met NLDD 0.8.83 een custom element: de klaartekst stond eerst in
``data-value`` op een ``.lotc-secret__value`` BINNEN het veld, en staat nu in ``value`` op
het host-element zelf; de knoppen dragen ``data-action`` in plaats van ``data-act``.

Drie plekken buiten de sjablonen lezen die vorm, en geen ervan draait in CI: de e2e-suite
staat daar uitgezet (``-m "not e2e"``). Verschuift de vorm nog een keer, dan verandert er
niets aan een groene CI en breekt het pas bij degene die de sandbox gebruikt - een halve
dag later, met een foutmelding over een leeg token.

Daarom staat de meting hier: op de HTML die het sjabloon echt oplevert, met de selectors
die die drie lezers gebruiken.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from opi.core.templates_lotc import templates_lotc

REPO = Path(__file__).resolve().parent.parent

#: Per lezer: het bestand, en het stuk tekst waarmee hij het geheimveld aanwijst. Staat
#: het er niet meer in, dan meet deze test iets anders dan wat die lezer doet.
LEZERS = [
    ("tests/e2e/helpers/sandbox_api.py", r'value="(zad_[A-Za-z0-9_-]+)"'),
    ("tests/e2e/test_lotc_project_tab.py", "lotc-secret-field button[data-action='copy']"),
    ("tests/e2e/test_componentkaart_uitklap.py", 'locator("lotc-secret-field button")'),
]

#: Het patroon uit ``sandbox_api.geef_projecttoken``, dat het zojuist uitgegeven token van
#: het tabblad Team plukt. Het voorvoegsel wijst het aan: op die pagina staat geen tweede
#: geheimveld dat met ``zad_`` begint.
TOKEN_PATROON = re.compile(r'value="(zad_[A-Za-z0-9_-]+)"')


#: Een waarde met de vorm van een token, niet met de inhoud ervan: ``example`` erin houdt
#: de secret-scan van een bevinding af zonder het patroon hierboven te breken.
EEN_TOKEN = "zad_example_dEf6-hIj9kLm2nOp5qRs8tUv1"


def _render(*, revealed: bool = False, waarde: str = "Ab3_dEf6-hIj9kLm2nOp5qRs8tUv1") -> str:
    extra = " revealed" if revealed else ""
    bron = f'<c-secret-field value="{{{{ waarde }}}}" show-copy{extra} />'
    return templates_lotc.env.from_string(bron).render(waarde=waarde)


@pytest.mark.parametrize(("bestand", "selector"), LEZERS, ids=[pad for pad, _ in LEZERS])
def test_elke_lezer_gebruikt_nog_de_vorm_die_hier_gemeten_wordt(bestand: str, selector: str) -> None:
    """Zonder deze helft kan een lezer wegdrijven terwijl deze test groen blijft."""
    assert selector in (REPO / bestand).read_text(), (
        f"{bestand} wijst het geheimveld niet meer aan met {selector!r}. Werk die lezer en "
        "de metingen hieronder samen bij, anders meet dit bestand een vorm die niemand leest."
    )


def test_de_klaartekst_staat_op_het_host_element() -> None:
    """De waarde die de tokenuitgifte ophaalt.

    Niet de tekst van het element: dat is een rij bolletjes. De volle waarde staat in een
    attribuut, en welk attribuut dat is, is precies wat met 0.8.83 verschoof.
    """
    html = _render()
    veld = BeautifulSoup(html, "html.parser").select_one("lotc-secret-field")

    assert veld is not None, f"geen lotc-secret-field in de uitvoer: {html[:200]}"
    assert veld.get("value") == "Ab3_dEf6-hIj9kLm2nOp5qRs8tUv1"
    assert veld.get_text(strip=True) != "Ab3_dEf6-hIj9kLm2nOp5qRs8tUv1", (
        "de klaartekst staat in de TEKST van het veld; dan is hij afgeschermd noch te scrapen"
    )


def test_het_patroon_uit_de_tokenuitgifte_vindt_de_waarde() -> None:
    """De regex van ``sandbox_api.geef_projecttoken``, op echte uitvoer.

    Die weg draait alleen tegen een sandbox, dus een stille mismatch komt daar terug als
    "geen uitgegeven token op het tabblad Team".
    """
    waarden = TOKEN_PATROON.findall(_render(revealed=True, waarde=EEN_TOKEN))

    assert waarden == [EEN_TOKEN], f"het patroon plukt er dit uit: {waarden}"


def test_de_kopieerknop_is_op_zijn_actie_aan_te_wijzen() -> None:
    """``data-action``, niet ``data-act``: die naam veranderde mee met het custom element."""
    soup = BeautifulSoup(_render(), "html.parser")

    assert soup.select("lotc-secret-field button[data-action='copy']"), (
        f"geen kopieerknop op data-action: {[k.get('data-action') for k in soup.select('button')]}"
    )


def test_de_eerste_knop_van_een_afgeschermd_veld_is_het_oogje() -> None:
    """De knop waar test_componentkaart_uitklap op klikt, en de reden dat hij ``.first`` is.

    Bij een afgeschermd veld staat het oogje vooraan en de kopieerknop erachter. Keert die
    volgorde om, dan klikt die test op kopieren en meet hij niet meer wat hij zegt.
    """
    knoppen = BeautifulSoup(_render(), "html.parser").select("lotc-secret-field button")

    assert [knop.get("data-action") for knop in knoppen] == ["reveal", "copy"]


def test_een_veld_dat_al_open_staat_heeft_geen_oogje() -> None:
    """De keerzijde: ``revealed`` laat de eerste knop de kopieerknop zijn.

    Zonder deze helft leest de test hierboven als "er staan altijd twee knoppen", en dan
    zou ``.first`` op de projectpagina (waar velden met ``revealed`` staan) de verkeerde
    knop kunnen zijn zonder dat het opvalt.
    """
    knoppen = BeautifulSoup(_render(revealed=True), "html.parser").select("lotc-secret-field button")

    assert [knop.get("data-action") for knop in knoppen] == ["copy"]
