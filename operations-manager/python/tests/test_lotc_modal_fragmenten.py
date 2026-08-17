"""De fragmenten van de bewerkdialoog: dragen ze nog waar het opslaan aan hangt?

De dialoog op de projectpagina wordt gevuld door vijf fragmenten: de stap, de
samenvatting, het startbeeld van de voortgang, de voortgang zelf en de melding bij
"alleen opslaan".

Hier stond een VERGELIJKING met de oude vormgeving van hetzelfde fragment: elke
bestemming, elk htmx-adres, elke aanroep, elk veld en elk id, twee keer gerenderd met
dezelfde context. Die tweede vormgeving is er niet meer. Wat ervoor in de plaats komt is
de LIJST: ``tests/oppervlak_snapshot_fragmenten.json`` legt per geval vast wat het
fragment draagt, en deze poort faalt als er iets van AF gaat.

De lijst bijwerken::

    ZAD_SCHRIJF_OPPERVLAK=1 uv run pytest tests/test_lotc_modal_fragmenten.py

Lees de diff dan ook echt: aan die velden en dat ene hx-post hangt het OPSLAAN, dus elke
regel die verdwijnt is een dialoog die stilletjes minder bewaart dan je invulde.

Waarom hier en niet in de e2e-poort: de drie standen van de voortgang (loopt, klaar,
mislukt) en de samenvatting zijn met een draaiende testserver niet te bereiken - die heeft
geen takendienst. Ze RENDEREN wel, met een context die we hier zelf neerzetten.

De stapfragmenten op de echte route staan in tests/e2e/test_gedragsoppervlak.py.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from opi.core.templates_lotc import templates_lotc
from opi.web.navigation_lotc import to_nldd_icon
from tests.oppervlak import als_lijsten, meet, ontbreekt

SNAPSHOT = Path(__file__).resolve().parent / "oppervlak_snapshot_fragmenten.json"

#: De omgevingsvariabele die van deze poort een schrijver maakt. Zie de moduledocstring.
SCHRIJVEN = os.environ.get("ZAD_SCHRIJF_OPPERVLAK") == "1"


def _steps() -> Any:
    """Een stappenstand met een stap, zoals een eenstapsflow hem heeft."""
    return SimpleNamespace(
        count=1,
        current="team-edit",
        all=["team-edit"],
        titles={"team-edit": "Projectleden"},
        completed=[],
        first="team-edit",
        last="team-edit",
        prev=None,
        next=None,
        is_first=True,
        is_last=True,
        index=0,
        progress_pct=100,
    )


def _request() -> Any:
    return SimpleNamespace(state=SimpleNamespace(csrf_token="csrf-voor-de-test"))


REVIEW_CONTEXT: dict[str, Any] = {
    "request": _request(),
    "steps": _steps(),
    "flow_id": "modal-edit-team",
    "project_name": "een-project",
    "wizard_token": "token-van-de-test",
    "section_summaries": [
        {
            "section_id": "team-edit",
            "title": "Projectleden",
            "icon": "groep-3-personen",
            "fields": [
                {"label": "Naam", "value": "waarde", "is_list": False},
                {"label": "Leden", "value": ["een", "twee"], "is_list": True},
                {"html": "<div class='wizard-review__seq-item'><strong>een rij</strong></div>"},
            ],
        }
    ],
    "action_label": "Bevestigen en verwerken",
    "warnings": ["dit kan dataverlies geven"],
    "global_errors": ["er ging iets mis"],
    "nldd_icon": to_nldd_icon,
}

PROGRESS_CONTEXT: dict[str, Any] = {"task_id": "taak-1", "project_name": "een-project"}


def _fragment_context(status: str) -> dict[str, Any]:
    return {
        "task_id": "taak-1",
        "project_name": "een-project",
        "progress": 42,
        "current_step": "Bezig met uitrollen",
        "tasks": [
            {
                "name": "Manifesten schrijven",
                "status": "completed",
                "error": None,
                "subtasks": [{"name": "component web", "status": "running", "error": None}],
            }
        ],
        "status": status,
        "error": "de taak is mislukt",
        "component_failures": (
            [
                {
                    "title": "Component start niet",
                    "suggestion": "controleer het image",
                    "failure_type": "crash_loop",
                    "component": "web",
                    "deployment": "default",
                }
            ]
            if status == "failed"
            else []
        ),
    }


#: Per geval: de naam, het sjabloon en de context.
GEVALLEN: list[tuple[str, str, dict[str, Any]]] = [
    ("samenvatting", "bg/_modal-wizard-review.html.j2", REVIEW_CONTEXT),
    ("voortgang-start", "bg/_modal-wizard-progress.html.j2", PROGRESS_CONTEXT),
    ("opgeslagen", "bg/_modal-wizard-success.html.j2", {}),
    *[
        (f"voortgang-{status}", "bg/_modal-wizard-progress-fragment.html.j2", _fragment_context(status))
        for status in ("running", "completed", "failed")
    ],
]


def _stap_context(*, eerste: bool, laatste: bool, basis_url: str, flow_id: str = "modal-edit-team") -> dict[str, Any]:
    """De context van EEN stap, met de stand van de knoppenbalk als knop.

    De HTTP-poort (tests/e2e/test_lotc_modal_pariteit.py) ziet altijd de EERSTE stap van
    een flow; verder komen kost een POST met een geldig token. De knoppen die daarna pas
    verschijnen - "Vorige" en "Volgende" in plaats van "Annuleren" en "Opslaan" - zijn
    juist waar een omzetting een adres kwijtraakt. Vandaar hier, op sjabloonniveau.
    """
    alle = ["een", "twee", "drie"]
    huidig = alle[0] if eerste else (alle[-1] if laatste else alle[1])
    index = alle.index(huidig)
    steps = SimpleNamespace(
        count=len(alle),
        current=huidig,
        all=alle,
        titles={naam: naam.title() for naam in alle},
        completed=alle[:index],
        first=alle[0],
        last=alle[-1],
        prev=(alle[index - 1] if index else None),
        next=(alle[index + 1] if index + 1 < len(alle) else None),
        is_first=eerste,
        is_last=laatste,
        index=index,
        progress_pct=50,
    )
    section = SimpleNamespace(
        section_id=huidig,
        title="Een stap",
        description="Wat je hier invult",
        icon="groep-3-personen",
        help_template="container-image.html.j2",
    )
    return {
        "request": _request(),
        "steps": steps,
        "flow_id": flow_id,
        "section": section,
        "step_html": "<p>de velden</p>",
        "project_name": "een-project",
        "wizard_token": "token-van-de-test",
        "errors": {},
        "global_errors": ["er ging iets mis"],
        "warnings": ["let op"],
        "step_base_url": basis_url,
        "step_target": "#edit-section-inner",
        "step_push_url": False,
        "step_query_params": "",
        "nldd_icon": to_nldd_icon,
    }


STAP_GEVALLEN: list[tuple[str, str, dict[str, Any]]] = [
    (naam, "bg/_modal-wizard-step.html.j2", context)
    for naam, context in (
        (
            "stap-eerste",
            _stap_context(eerste=True, laatste=False, basis_url="/projects/een-project/modal-wizard/f/step/"),
        ),
        (
            "stap-midden",
            _stap_context(eerste=False, laatste=False, basis_url="/projects/een-project/modal-wizard/f/step/"),
        ),
        (
            "stap-laatste",
            _stap_context(eerste=False, laatste=True, basis_url="/projects/een-project/modal-wizard/f/step/"),
        ),
        (
            # De dialoog van de goedkeurder draait dezelfde stap op een ANDER adres. Dat
            # adres komt uit de context, en het is het enige verschil - dus de kans dat
            # het bij een omzetting vastgeroest raakt op /projects/ is precies de reden
            # dat het hier gemeten wordt. Er is geen testproject met een openstaande
            # aanvraag, dus dit is de enige plek waar die dialoog gemeten wordt.
            "stap-goedkeuring",
            _stap_context(
                eerste=True,
                laatste=True,
                basis_url="/admin/approvals/een-project/modal-wizard/admin-approval/step/",
                flow_id="admin-approval",
            ),
        ),
        (
            # Backup en restore zeggen "Verder" in plaats van "Opslaan" en tonen de
            # melding "Bezig met opslaan" niet: ze slaan niets op maar starten een taak.
            "stap-backup",
            _stap_context(
                eerste=True,
                laatste=True,
                basis_url="/projects/een-project/modal-wizard/modal-backup/step/",
                flow_id="modal-backup",
            ),
        ),
    )
]


ALLE_GEVALLEN = [*GEVALLEN, *STAP_GEVALLEN]


@pytest.fixture(scope="module")
def vastgelegd() -> dict[str, dict[str, list[str]]]:
    if not SNAPSHOT.exists():
        return {}
    return json.loads(SNAPSHOT.read_text())


@pytest.fixture(scope="module")
def geschreven():
    """Verzamelt de metingen en schrijft ze weg als erom gevraagd is."""
    verzameld: dict[str, dict[str, list[str]]] = {}
    yield verzameld
    if SCHRIJVEN and verzameld:
        bestaand = json.loads(SNAPSHOT.read_text()) if SNAPSHOT.exists() else {}
        bestaand.update(verzameld)
        SNAPSHOT.write_text(json.dumps(bestaand, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def test_de_lijst_dekt_elk_geval(vastgelegd: dict[str, dict[str, list[str]]]) -> None:
    """Bewaak de bewaker: een geval zonder vastgelegde lijst is een geval zonder poort."""
    if SCHRIJVEN:
        pytest.skip("de lijst wordt in deze run opnieuw geschreven")

    ontbrekend = [naam for naam, _, _ in ALLE_GEVALLEN if naam not in vastgelegd]
    assert not ontbrekend, (
        f"deze gevallen staan niet in {SNAPSHOT.name}: {ontbrekend}. "
        f"Draai ZAD_SCHRIJF_OPPERVLAK=1 pytest {Path(__file__).name} om ze vast te leggen."
    )


@pytest.mark.parametrize(("naam", "sjabloon", "context"), ALLE_GEVALLEN, ids=[g[0] for g in ALLE_GEVALLEN])
def test_het_fragment_draagt_nog_alles_wat_er_vastligt(
    vastgelegd: dict[str, dict[str, list[str]]],
    geschreven: dict[str, dict[str, list[str]]],
    naam: str,
    sjabloon: str,
    context: dict[str, Any],
) -> None:
    """Geen bestemming, geen htmx-adres, geen aanroep, geen veld en geen id mag wegvallen."""
    gemeten = meet(templates_lotc.env.get_template(sjabloon).render(context))

    if SCHRIJVEN:
        geschreven[naam] = als_lijsten(gemeten)
        return

    weg = ontbreekt(vastgelegd.get(naam, {}), gemeten)
    assert not weg, (
        f"verdwenen gedrag in {naam} ({sjabloon}):\n  "
        + "\n  ".join(weg)
        + f"\nIs dat de bedoeling, werk dan {SNAPSHOT.name} bij MET de reden in de PR."
    )


def test_de_samenvatting_toont_alle_drie_de_soorten_veld() -> None:
    """Paar, lijst en voorgerenderde HTML komen alle drie in beeld.

    Zonder deze toets zou het vorige geval ook slagen met een samenvatting die de velden
    helemaal niet toont: er staat geen knop of adres in, dus in het gedragsoppervlak valt
    hun verdwijnen niet op. En juist dit scherm is waar iemand controleert wat hij zo
    gaat opslaan.
    """
    nieuw = templates_lotc.env.get_template("bg/_modal-wizard-review.html.j2").render(REVIEW_CONTEXT)

    assert "waarde" in nieuw
    assert "een" in nieuw
    assert "twee" in nieuw
    assert "een rij" in nieuw
    assert "dit kan dataverlies geven" in nieuw
    assert "er ging iets mis" in nieuw


def test_de_voortgang_voert_een_stapnaam_niet_uit_als_sjabloon() -> None:
    """``{{ ... }}`` in een stapnaam is tekst, geen expressie.

    Het voortgangsfragment rendert EEN keer, met opzet. Ging er alsnog een tweede
    sjabloonslag overheen, dan zou de naam van een stap of subtaak uitgevoerd worden in
    de OPI-pod. Dat is in deze codebase eerder een lek geweest, dus het is een toets en
    geen afspraak.
    """
    context = _fragment_context("running")
    context["current_step"] = "{{ 6 * 9 }}"
    context["tasks"][0]["name"] = "{{ 6 * 9 }}"

    nieuw = templates_lotc.env.get_template("bg/_modal-wizard-progress-fragment.html.j2").render(context)

    assert "54" not in nieuw
    assert "6 * 9" in nieuw


def test_de_stap_houdt_zijn_knoppentekst_per_flow() -> None:
    """Backup zegt "Verder", een bewerking zegt "Opslaan" - dat verschil blijft staan.

    Het is de enige plek waar de tekst van de verzendknop van de flow afhangt, en zulke
    voorwaarden zijn precies wat een omzetting kwijtraakt: de knop rendert, alleen met
    het verkeerde woord erop.
    """
    env = templates_lotc.env.get_template("bg/_modal-wizard-step.html.j2")

    backup = env.render(_stap_context(eerste=True, laatste=True, basis_url="/x/", flow_id="modal-backup"))
    bewerken = env.render(_stap_context(eerste=True, laatste=True, basis_url="/x/", flow_id="modal-edit-team"))

    assert "Verder" in backup
    assert "Opslaan" not in backup
    assert "Opslaan" in bewerken
    assert "Verder" not in bewerken
    # De melding "Bezig met opslaan" hoort bij opslaan, niet bij het starten van een taak.
    assert "Bezig met opslaan" in bewerken
    assert "Bezig met opslaan" not in backup
