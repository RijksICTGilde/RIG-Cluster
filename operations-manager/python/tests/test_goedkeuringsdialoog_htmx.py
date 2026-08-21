"""De goedkeuringsdialoog haalt zijn formulier met htmx op, en niet met de hand.

WAAROM DIT EEN STATISCHE POORT IS EN NIET ALLEEN EEN BROWSERTEST

Het gedrag hieronder wordt in de browser gemeten door
``tests/e2e/test_lotc_aanvragenbeheer.py`` en ``tests/e2e/test_shared_modal_blockade.py``.
Die suite draait alleen apart (``-m e2e``); de gewone testronde slaat hem over. Toen de
htmx-omzetting van RC-115 bij een merge wegviel terwijl de TESTS ervan wel meekwamen,
stond er dus zeven keer rood in een suite die niemand in die ronde draaide, en groen in
de suite die wel draaide.

Deze test kost geen browser en loopt dus altijd mee. Hij meet de drie dingen die bij het
terugvallen op de handbouw meteen weer verdwenen:

  - de knop "Beheren" haalt het formulier op met ``hx-get`` (een GEWOON attribuut, dus
    door Jinja gerenderd - een URL in JavaScript samenstellen gaf de %7B%7B-fout);
  - er staat geen vooraf neergezette foutbak meer in de dialoog: een fout komt als
    fragment terug, of hij komt niet;
  - ``#approval-loading`` bestaat, want ``static/css/modal.css`` stuurt dat element aan.
    Dat is het paar dat stilletjes uit elkaar viel: de CSS landde wel, het sjabloon niet.
"""

from __future__ import annotations

import pathlib
import re

from opi.core.template_helpers import TEMPLATES_DIR

SJABLOON = pathlib.Path(TEMPLATES_DIR) / "bg" / "admin-approvals.html.j2"
MODAL_CSS = pathlib.Path(__file__).resolve().parents[1] / "static" / "css" / "modal.css"

#: De knop "Beheren", met alles wat htmx nodig heeft om de dialoog te vullen.
BEHEREN = re.compile(
    r"label=\"Beheren\"[^>]*"
    r"hx-get=\"/admin/approvals/\{\{ project\.project_name \| urlencode \}\}/modal-wizard/admin-approval\"[^>]*"
    r"hx-target=\"#edit-section-inner\"[^>]*"
    r"hx-indicator=\"#approval-loading\""
)


def _bron() -> str:
    return SJABLOON.read_text()


def test_beheren_haalt_het_formulier_op_met_htmx() -> None:
    """De URL staat in een hx-get, met de projectnaam uit Jinja."""
    assert BEHEREN.search(_bron()), (
        "De knop 'Beheren' haalt het formulier niet (meer) met htmx op. De URL hoort in "
        "hx-get te staan - een gewoon attribuut, dus door Jinja gerenderd. Zie "
        "tests/e2e/test_lotc_aanvragenbeheer.py voor wat er misgaat als hij in JavaScript "
        "wordt samengesteld."
    )


def test_de_dialoog_haalt_niets_met_de_hand_op() -> None:
    """Geen fetch en geen innerHTML: dat is precies de handbouw die eruit ging."""
    bron = _bron()

    assert "fetch(" not in bron, "de handgeschreven fetch staat er weer; htmx doet het ophalen"
    assert ".innerHTML" not in bron, "de handgeschreven innerHTML staat er weer; hx-target doet het plaatsen"


def test_er_staat_geen_vooraf_neergezette_foutbak() -> None:
    """Een fout komt als fragment terug (bg/_modal-fout.html.j2), niet in een lege bak."""
    assert "approval-error" not in _bron(), (
        "de lege foutbak staat er weer. Die werd op elke opening als leeg rood vak "
        "getekend, want de klasse waarmee hij verborgen werd doet in deze schil niets."
    )


def test_de_laadtoestand_van_htmx_staat_in_het_sjabloon_en_in_de_css() -> None:
    """``#approval-loading`` hoort aan beide kanten te bestaan, anders stuurt de CSS niets aan."""
    assert 'id="approval-loading"' in _bron(), "het element dat hx-indicator aanwijst bestaat niet"
    assert "#approval-loading" in MODAL_CSS.read_text(), "css/modal.css stuurt de laadtoestand niet aan"


def test_htmx_toont_ook_een_antwoord_met_een_foutcode() -> None:
    """Zonder deze haak wisselt htmx bij een 4xx of 5xx niets in: venster open, en leeg."""
    bron = _bron()

    assert "htmx:beforeSwap" in bron, "de haak die een foutantwoord alsnog toont is weg"
    assert "shouldSwap = true" in bron, "de haak zet de wissel niet aan"
