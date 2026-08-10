"""Het gedeelde voortgangsfragment volgt de vormgeving van de pagina eromheen (RC-64).

Waarom dit een eigen poort verdient: ``render_progress_fragment`` rendert het antwoord op
elke bevestigde actie op de projectpagina (verwerken, slapen, verwijderen). Het rendert
NIET de pagina, dus het valt niet op als het scheef staat - het komt na een klik binnen,
midden in een dialoog die er verder goed uitziet. Tot RC-64 koos het altijd de
roos-omgeving, ook in een LOTC-pagina, en dat leverde een knop met rvo-klassen op die daar
door geen enkel stijlblad opgemaakt wordt: ``lotc_rvo`` staat niet in ``DESIGN_SYSTEMS``.

Twee dingen worden gemeten, en het tweede is de reden dat de eerste alleen niet genoeg is:

1. De keuze valt goed - het LOTC-verzoek krijgt geen roos-HTML.
2. De knop in het LOTC-fragment WERKT. De LOTC-tegenhanger zette ``on_complete`` in een
   variabele en hing hem nergens aan, dus de knop kwam zonder klikafhandeling op het
   scherm. Overschakelen zonder die reparatie ruilt een lelijke werkende knop in voor een
   mooie dode knop, en dat is geen vooruitgang.
"""

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from opi.web.task_progress import render_progress_fragment

#: Wat de roos-omgeving in elke gerenderde component achterlaat. Op een LOTC-pagina is dit
#: het bewijs dat er HTML uit de andere omgeving is binnengekomen; LOTC zet
#: ``data-lotc-component``.
ROOS_MARKER = "data-roos-component"


def _request(layout: str) -> SimpleNamespace:
    """Het kleinste dat de schakelaar van een verzoek nodig heeft."""
    return SimpleNamespace(query_params={"layout": layout}, cookies={})


def _context(**overrides) -> dict:
    context = {
        "task_id": "t-1",
        "project_name": "demo",
        "progress_url": "/projects/demo/task-progress/t-1",
        "progress": 40,
        "current_step": "Bezig met verwerken",
        "tasks": [{"name": "stap", "status": "completed", "subtasks": [{"name": "substap", "status": "running"}]}],
        "status": "running",
        "success_message": "Wijzigingen succesvol verwerkt!",
        "on_complete": "location.reload()",
    }
    context.update(overrides)
    return context


@pytest.mark.parametrize("status", ["running", "completed", "failed"])
def test_het_lotc_verzoek_krijgt_geen_roos_html(status: str) -> None:
    rendered = render_progress_fragment(_request("nldd"), _context(status=status))

    assert ROOS_MARKER not in rendered, f"roos-HTML in het LOTC-fragment bij status={status}"
    assert "rvo-" not in rendered, f"rvo-klasse in het LOTC-fragment bij status={status}"


@pytest.mark.parametrize("status", ["running", "completed", "failed"])
def test_het_roos_verzoek_krijgt_nog_steeds_roos_html(status: str) -> None:
    """De oude weg blijft intact; de schakelaar kiest, hij vervangt niet."""
    rendered = render_progress_fragment(_request("roos"), _context(status=status))

    assert ROOS_MARKER in rendered


@pytest.mark.parametrize("status", ["completed", "failed"])
def test_de_afsluitknop_voert_on_complete_uit(status: str) -> None:
    """Zonder dit is de knop mooi en dood: de dialoog gaat niet dicht."""
    rendered = render_progress_fragment(_request("nldd"), _context(status=status, on_complete="location.reload()"))

    assert 'onclick="location.reload()"' in rendered, f"knop zonder klikafhandeling bij status={status}"


def test_de_iconen_van_de_takenlijst_bestaan_in_de_nldd_woordenschat() -> None:
    """Een ROOS-iconnaam rendert in NLDD leeg, zonder fout. Zie test_lotc_icon_mapping."""
    lotc = pytest.importorskip("lord_of_the_components")
    icons = json.loads((Path(lotc.__file__).parent / "icons.json").read_text())
    woordenschat = set(icons["sets"]["nldd"]) | set(icons["aliases"])
    rendered = render_progress_fragment(
        _request("nldd"),
        _context(tasks=[{"name": "a", "status": s, "subtasks": []} for s in ("completed", "failed", "running", "")]),
    )

    namen = set(re.findall(r'<nldd-icon[^>]*name="([^"]+)"', rendered))
    assert namen, "geen enkel icoon in de takenlijst"
    assert namen <= woordenschat, f"onbekende iconnamen: {sorted(namen - woordenschat)}"


def test_de_foutmelding_toont_de_suggestie_en_een_werkende_logboeklink() -> None:
    """De partial die het fragment eerst insloot liet de suggestie weg en gaf een lege,
    dode link. Aanzetten zonder dat te repareren zou een gebruiker met een mislukte taak
    een melding zonder inhoud opleveren."""
    rendered = render_progress_fragment(
        _request("nldd"),
        _context(
            status="failed",
            component_failures=[
                {
                    "component": "web",
                    "deployment": "prod",
                    "failure_type": "crash",
                    "title": "Component start niet",
                    "suggestion": "Controleer het geheugenlimiet",
                }
            ],
        ),
    )

    assert "Controleer het geheugenlimiet" in rendered
    assert "openLogViewer(" in rendered
    assert "Bekijk logs voor 'web'" in rendered
