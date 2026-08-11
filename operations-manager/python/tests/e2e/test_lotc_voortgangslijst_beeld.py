"""Staat het statuspictogram NAAST de taaknaam, in een browser?

Dit is de meting waar de reparatie vandaan komt, en de reden dat hij niet met een
assertie op de HTML af te doen was: de markup zag er in alle vier de sjablonen prima uit
en op drie ervan viel het pictogram boven de tekst. Dat hing aan een stijlblad
(``modal.css``) dat die drie pagina's niet laden.

De drie standen van de voortgang zijn zonder takendienst niet via een route te bereiken -
zie de kop van ``tests/test_lotc_modal_fragmenten.py``. Daarom wordt het fragment hier
server-side gerenderd en in een ECHTE pagina van de testserver gezet: de thema-CSS en de
componenten zijn dan precies die van de applicatie, en de meting gaat over de geometrie
die de gebruiker ziet.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from opi.core.templates_lotc import templates_lotc

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

SJABLONEN = [
    "bg/_modal-wizard-progress-fragment.html.j2",
    "bg/_task-progress.html.j2",
    "partials/task_progress_fragment.html.j2",
    "wizard/modal_wizard_progress_fragment.html.j2",
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

#: De meting per lijstregel: staat de tekstcel rechts van de pictogramcel, op dezelfde
#: hoogte, en levert het pictogram ook echt beeld op.
METING = """(fragment) => {
    const bak = document.createElement('div');
    bak.id = 'meetbak';
    bak.innerHTML = fragment;
    document.body.appendChild(bak);
    return customElements.whenDefined('nldd-list-item').then(() => new Promise(klaar => {
        requestAnimationFrame(() => requestAnimationFrame(() => {
            const uit = [];
            for (const regel of bak.querySelectorAll('nldd-list-item')) {
                const pictogram = regel.querySelector('nldd-icon-cell');
                const tekst = regel.querySelector('nldd-text-cell');
                if (!pictogram || !tekst) { uit.push({fout: 'cel ontbreekt'}); continue; }
                const p = pictogram.getBoundingClientRect();
                const t = tekst.getBoundingClientRect();
                const icoon = pictogram.shadowRoot && pictogram.shadowRoot.querySelector('nldd-icon');
                uit.push({
                    tekst: tekst.textContent.trim(),
                    naastElkaar: t.left >= p.right - 1,
                    zelfdeHoogte: Math.abs(t.top - p.top) < 12,
                    tussenruimte: Math.round(t.left - p.right),
                    pictogramRendert: !!(icoon && icoon.shadowRoot && icoon.shadowRoot.querySelector('svg')),
                    inspringing: Math.round(p.left - regel.getBoundingClientRect().left),
                });
            }
            bak.remove();
            klaar(uit);
        }));
    }));
}"""


@pytest.mark.parametrize("sjabloon", SJABLONEN)
def test_pictogram_staat_naast_de_taaknaam(app_server: str, auth_page: Page, sjabloon: str) -> None:
    """Op elke pagina, met of zonder onze eigen stijlbladen."""
    auth_page.goto(f"{app_server}/projects/")
    auth_page.wait_for_load_state("networkidle")

    fragment = templates_lotc.env.get_template(sjabloon).render(**CONTEXT)
    regels = auth_page.evaluate(METING, fragment)

    assert len(regels) == 3, f"{sjabloon} toont {len(regels)} regels in plaats van 3"
    for regel in regels:
        assert "fout" not in regel, f"{sjabloon}: {regel['fout']}"
        assert regel["naastElkaar"], f"{sjabloon}: pictogram staat boven '{regel['tekst']}'"
        assert regel["zelfdeHoogte"], f"{sjabloon}: pictogram en '{regel['tekst']}' liggen niet op een lijn"
        assert regel["tussenruimte"] > 0, f"{sjabloon}: '{regel['tekst']}' plakt tegen het pictogram"
        assert regel["pictogramRendert"], f"{sjabloon}: leeg pictogram bij '{regel['tekst']}'"

    inspringend = [regel for regel in regels if regel["inspringing"] > 0]
    assert [regel["tekst"] for regel in inspringend] == ["YAML wegschrijven"], (
        f"{sjabloon}: alleen de subtaak hoort in te springen"
    )
