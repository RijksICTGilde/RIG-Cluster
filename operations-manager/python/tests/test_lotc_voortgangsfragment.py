"""Het gedeelde voortgangsfragment (RC-64).

Waarom dit een eigen poort verdient: ``render_progress_fragment`` rendert het antwoord op
elke bevestigde actie op de projectpagina (verwerken, slapen, verwijderen). Het rendert
NIET de pagina, dus het valt niet op als het scheef staat - het komt na een klik binnen,
midden in een dialoog die er verder goed uitziet.

Twee dingen worden gemeten, en het tweede is de reden dat de eerste alleen niet genoeg is:

1. Er komt geen markup van het oude componentensysteem uit. Tot RC-64 rendeerde dit
   fragment altijd in de roos-omgeving, ook in een LOTC-pagina, en dat leverde een knop
   met rvo-klassen op die daar door geen enkel stijlblad opgemaakt werd.
2. De knop WERKT. Het omgezette sjabloon zette ``on_complete`` in een variabele en hing
   hem nergens aan, dus de knop kwam zonder klikafhandeling op het scherm. Een mooie dode
   knop is geen vooruitgang op een lelijke werkende.
"""

import re
from types import SimpleNamespace

import pytest
from opi.web.nldd_iconen import nldd_icon_names
from opi.web.task_progress import render_progress_fragment

#: Wat het OUDE componentensysteem in elke gerenderde component achterliet. Een enkel
#: voorkomen bewijst dat er HTML uit een tweede renderomgeving is binnengekomen; dit
#: systeem zet ``data-lotc-component``.
ROOS_MARKER = "data-roos-component"


def _request() -> SimpleNamespace:
    """Het kleinste dat het fragment van een verzoek nodig heeft."""
    return SimpleNamespace(query_params={}, cookies={})


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
def test_het_fragment_bevat_geen_markup_van_het_oude_systeem(status: str) -> None:
    rendered = render_progress_fragment(_request(), _context(status=status))

    assert ROOS_MARKER not in rendered, f"markup uit een tweede omgeving bij status={status}"
    assert "rvo-" not in rendered, f"rvo-klasse in het fragment bij status={status}"


@pytest.mark.parametrize("status", ["completed", "failed"])
def test_de_afsluitknop_voert_on_complete_uit(status: str) -> None:
    """Zonder dit is de knop mooi en dood: de dialoog gaat niet dicht."""
    rendered = render_progress_fragment(_request(), _context(status=status, on_complete="location.reload()"))

    assert 'onclick="location.reload()"' in rendered, f"knop zonder klikafhandeling bij status={status}"


def test_de_iconen_van_de_takenlijst_bestaan_in_de_nldd_woordenschat() -> None:
    """Een ROOS-iconnaam rendert in NLDD leeg, zonder fout. Zie test_lotc_icon_mapping.

    Gemeten tegen de BUNDEL die de browser laadt en niet tegen ``icons.json``: die twee
    lopen uiteen (327 namen tegen 271), en deze poort stond hier eerder tegen de lijst.

    De namen staan sinds de takenlijst op lijstcomponenten staat als ``icon=`` op een
    ``nldd-icon-cell``; het pictogram zelf zit in de schaduwboom van dat component.
    """
    rendered = render_progress_fragment(
        _request(),
        _context(tasks=[{"name": "a", "status": s, "subtasks": []} for s in ("completed", "failed", "running", "")]),
    )

    namen = set(re.findall(r'<nldd-icon-cell[^>]*\bicon="([^"]+)"', rendered))
    assert namen, "geen enkel icoon in de takenlijst"
    woordenschat = nldd_icon_names()
    assert namen <= woordenschat, f"onbekende iconnamen: {sorted(namen - woordenschat)}"


def test_de_foutmelding_toont_de_suggestie_en_een_werkende_logboeklink() -> None:
    """De partial die het fragment eerst insloot liet de suggestie weg en gaf een lege,
    dode link. Aanzetten zonder dat te repareren zou een gebruiker met een mislukte taak
    een melding zonder inhoud opleveren."""
    rendered = render_progress_fragment(
        _request(),
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
