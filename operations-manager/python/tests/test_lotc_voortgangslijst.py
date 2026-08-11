"""De takenlijst van de voortgang: EEN plek, en geen eigen opmaak.

Vier sjablonen tonen dezelfde lijst - de taken van een lopende opdracht, met per regel
een status. Die lijst stond drie keer met de hand gebouwd: een keer in de macro met een
``div.edit-progress-task-item`` waarvan de ``display: flex`` in ``static/css/modal.css``
stond, en twee keer in de bg-fragmenten met ``c-cluster`` en ``c-paragraph``. Gemeten in
een browser viel het pictogram in allebei de eigen versies boven de tekst op elke pagina
die dat stijlblad niet laadt, en dat waren er drie van de vier.

De reparatie is niet "het stijlblad er ook bij laden" - dan is de vijfde plek weer stuk -
maar de lijst op de lijstcomponenten van het thema zetten, in de macro die alle vier de
sjablonen aanroepen. Componenten dragen hun eigen opmaak.

Deze poort bewaakt dat het EEN plek blijft. Dat de regel in een browser ook echt naast
elkaar staat, meet ``tests/e2e/test_lotc_voortgangslijst_beeld.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from opi.core.templates_lotc import templates_lotc

#: De sjablonen die de takenlijst tonen. Alle vier via dezelfde macro.
SJABLONEN = [
    "bg/_modal-wizard-progress-fragment.html.j2",
    "bg/_task-progress.html.j2",
    "partials/task_progress_fragment.html.j2",
    "wizard/modal_wizard_progress_fragment.html.j2",
]

#: De klassen waarmee de lijst met de hand werd opgemaakt. Ze horen nergens meer te staan.
EIGEN_KLASSEN = [
    "edit-progress-tasks",
    "edit-progress-task-item",
    "edit-progress-subtask",
    "edit-progress-task-error",
]

TAKEN: list[dict[str, Any]] = [
    {
        "name": "Project bijwerken",
        "status": "completed",
        "error": None,
        "subtasks": [{"name": "YAML wegschrijven", "status": "running", "subtasks": [], "error": None}],
    },
    {"name": "Uitrollen", "status": "failed", "error": "Kon niet verbinden", "subtasks": []},
]

CONTEXT: dict[str, Any] = {
    "task_id": "t1",
    "project_name": "proj",
    "progress": 40,
    "current_step": "Bezig",
    "tasks": TAKEN,
    "status": "running",
    "error": None,
    "progress_url": "/x",
    "container_id": "c1",
}

WORTEL = Path(__file__).resolve().parent.parent


def _render(sjabloon: str) -> str:
    return templates_lotc.env.get_template(sjabloon).render(**CONTEXT)


@pytest.mark.parametrize("sjabloon", SJABLONEN)
def test_lijst_komt_uit_de_gedeelde_macro(sjabloon: str) -> None:
    """Elk sjabloon toont de taken als lijstregels met cellen, niet als eigen divs."""
    html = _render(sjabloon)
    assert "<nldd-list " in html, f"{sjabloon} rendert geen lijst"
    # drie regels: een taak, zijn subtaak, en de tweede taak
    assert html.count("<nldd-list-item") == 3, f"{sjabloon} toont niet elke taak en subtaak"
    assert html.count("<nldd-icon-cell") == 3, f"{sjabloon} mist een statuspictogram"
    assert "Project bijwerken" in html
    assert "YAML wegschrijven" in html


@pytest.mark.parametrize("sjabloon", SJABLONEN)
def test_geen_eigen_opmaak_meer(sjabloon: str) -> None:
    """Geen klasse uit modal.css: de lijst mag van geen enkel stijlblad afhangen."""
    html = _render(sjabloon)
    for klasse in EIGEN_KLASSEN:
        assert klasse not in html, f"{sjabloon} hangt nog aan .{klasse}"


def test_subtaak_springt_in_met_een_cel() -> None:
    """De inspringing is een spacer-cel van het thema en geen padding uit een stijlblad."""
    html = _render("bg/_task-progress.html.j2")
    regels = [regel for regel in html.split("<nldd-list-item") if "nldd-text-cell" in regel]
    subtaak = next(regel for regel in regels if "YAML wegschrijven" in regel)
    hoofdtaak = next(regel for regel in regels if "Project bijwerken" in regel)
    assert 'size="24"' in subtaak, "de subtaak springt niet in"
    assert 'size="24"' not in hoofdtaak, "de hoofdtaak springt ten onrechte in"


def test_de_foutregel_hangt_onder_zijn_taak() -> None:
    """De fout is de supporting-text van de cel, en dus onlosmakelijk van die regel."""
    html = _render("bg/_task-progress.html.j2")
    assert 'supporting-text="Kon niet verbinden"' in html


def test_de_wizardmodal_toont_de_foutregels_nog_steeds_niet() -> None:
    """show_errors=False blijft: in de dialoog staat de fout al in de melding erboven."""
    html = _render("bg/_modal-wizard-progress-fragment.html.j2")
    assert "Kon niet verbinden" not in html


def test_de_klassen_staan_ook_niet_meer_in_het_stijlblad() -> None:
    """Regels die niemand meer gebruikt zijn de volgende die per ongeluk terugkomen."""
    css = (WORTEL / "static" / "css" / "modal.css").read_text()
    for klasse in EIGEN_KLASSEN:
        assert f".{klasse} " not in css, f".{klasse} staat nog in modal.css"
        assert f".{klasse}{{" not in css, f".{klasse} staat nog in modal.css"


def test_geen_sjabloon_laadt_modal_css_voor_deze_lijst() -> None:
    """De pleister die aanleiding was: modal.css bijladen op een pagina om de lijst.

    De pagina's met een BEWERKDIALOOG laden modal.css nog steeds, want die dialoog heeft
    zijn eigen opmaak. De wizardpagina had hem alleen voor deze lijst, en die is weg.
    """
    wizardpagina = (WORTEL / "opi" / "templates_lotc" / "wizard" / "wizard_page.html.j2").read_text()
    assert "static_url('css/modal.css')" not in wizardpagina
