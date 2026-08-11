"""De fragmenten van de bewerkdialoog doen in beide weergaven hetzelfde.

De dialoog op de projectpagina wordt gevuld door vijf fragmenten: de stap, de
samenvatting, het startbeeld van de voortgang, de voortgang zelf en de meldingen bij
"alleen opslaan". Ze zijn omgezet naar LOTC, en de eis bij die omzetting is dat ze er
anders UITZIEN en hetzelfde DOEN.

Wat hier gemeten wordt is dus niet de vormgeving maar het gedrag: waar een knop heen
gaat, welk adres htmx ophaalt, welke JavaScript-functie er aangeroepen wordt, welke
velden er zijn en welke id's er staan waar htmx of het script aan hangt. Twee keer
dezelfde context, twee sjablonen, een vergelijking van verzamelingen.

Waarom hier en niet in de e2e-poort: deze drie standen (voortgang loopt, klaar, mislukt)
en de samenvatting zijn met een draaiende testserver niet te bereiken - die heeft geen
takendienst. Ze RENDEREN wel, met een context die we hier zelf neerzetten, en dat is
precies genoeg om te zien of er een knop of een adres is weggevallen.

De stapfragmenten zelf gaan wel over de echte route; die staan in
tests/e2e/test_lotc_modal_pariteit.py.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from types import SimpleNamespace
from typing import Any

import pytest
from opi.core.templates_lotc import templates_lotc
from opi.web.navigation_lotc import to_nldd_icon

HX_ATTRS = ("hx-get", "hx-post", "hx-delete", "hx-put", "hx-patch")
JS_ATTRS = ("onclick", "@click", "onchange", "oninput", "onsubmit")

#: Het wizardtoken en het CSRF-token gaan HELEMAAL uit een gemeten adres.
#:
#: Niet alleen omdat ze per sessie verschillen. De twee componentsystemen zetten ze op een
#: andere plek: onder roos verdwijnt de querystring van een knop met een voorwaardelijk
#: attribuut ernaast uit het adres en reist het token alleen nog in ``hx-vals`` mee, onder
#: LOTC blijft hij in het adres EN in ``hx-vals`` staan. Beide kanten sturen het token dus
#: mee; alleen de weg verschilt, en dat is vormgeving van het componentsysteem.
#:
#: Dat het token ECHT aankomt is daarmee niet gemeten, en daar is deze toets ook niet de
#: plek voor: dat doet tests/e2e/test_lotc_modal_dialoog.py, die de dialoog opent en
#: opslaat tegen een draaiende server.
VLUCHTIG = re.compile(r"[?&](?:_wizard_token|csrf|csrf_token)=[^&\s\"']*", re.IGNORECASE)


def stabiel(waarde: str) -> str:
    """Haal de tokens uit een adres, zodat er een PAD overblijft om te vergelijken."""
    zonder = VLUCHTIG.sub("", waarde)
    return zonder.replace("&", "?", 1) if "?" not in zonder and "&" in zonder else zonder


#: Verschillen in de id-verzameling die we AANVAARDEN, elk met de reden erbij. Een regel
#: hier is een besluit, geen dekking.
AANVAARDE_IDS = {
    # Het roos-veld zet zijn label in een eigen <label id="...-label"> en zijn uitleg in
    # een <span id="...-helper">. Het LOTC-veld draagt allebei als attribuut op
    # <nldd-form-field>; er is dus geen apart element om een id op te zetten. De uitleg
    # zelf is er wel, met een id op -help. Eigenschap van de formulierlaag, niet van deze
    # omzetting - het geldt net zo op de al omgezette wizardpagina.
    "-label": "label is een attribuut geworden in plaats van een eigen element",
    "-helper": "de uitleg heet -help in de LOTC-formulierlaag",
    # De stappenbalk van roos is een <nav id="wizard-steps">; die van LOTC is een
    # <c-step-indicator> zonder eigen id. In de DIALOOG hangt er niets aan: de hele
    # #edit-section-inner wordt bij elke stap vervangen, dus er is niets om apart aan te
    # wijzen. (Op de wizardPAGINA wel - daar zet het fragment zelf de wikkel met die id
    # eromheen, en dat is ongewijzigd.)
    "wizard-steps": "de stappenbalk heeft in de dialoog geen id nodig; niets wijst hem aan",
}


class Oppervlak(HTMLParser):
    """Verzamelt wat een fragment DOET, los van hoe het eruitziet.

    De tegenhanger van ``Oppervlak`` in scripts/lotc_compare_behaviour.py, met een
    verschil dat hier nodig is: een LOTC-formulierveld is geen ``<input>`` maar een
    ``<nldd-text-field name=...>``. Alleen op de tagnaam ``input`` meten zou elk veld in
    de nieuwe weergave als verdwenen melden terwijl het er gewoon staat - en een meetlat
    die altijd piept, houdt niemand in de gaten.

    ``<nldd-icon name=...>`` valt er expres buiten: dat ``name`` is de naam van het
    plaatje, geen formulierveld.
    """

    BESTURING_TAGS = ("input", "select", "textarea")
    GEEN_BESTURING = ("nldd-icon",)

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.bestemmingen: set[str] = set()
        self.htmx: set[str] = set()
        self.functies: set[str] = set()
        self.velden: set[str] = set()
        self.ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        d = {k: (v or "") for k, v in attrs}

        for sleutel, waarde in d.items():
            if sleutel not in ("href", "action") and not sleutel.endswith("-href"):
                continue
            if waarde and not waarde.startswith(("#", "javascript:", "/static/")):
                self.bestemmingen.add(stabiel(waarde))

        for sleutel in HX_ATTRS:
            if d.get(sleutel):
                self.htmx.add(f"{sleutel}={stabiel(d[sleutel])}")

        for sleutel in JS_ATTRS:
            for naam in re.findall(r"([A-Za-z_$][\w$]*)\s*\(", d.get(sleutel, "")):
                self.functies.add(naam)

        is_besturing = tag in self.BESTURING_TAGS or (
            tag.startswith(("nldd-", "c-")) and tag not in self.GEEN_BESTURING
        )
        if is_besturing and d.get("name"):
            self.velden.add(d["name"])

        # input-id telt mee: het LOTC-veld geeft de id door aan zijn eigen invoerveld.
        for sleutel in ("id", "input-id"):
            if d.get(sleutel):
                self.ids.add(d[sleutel])


def meet(html: str) -> Oppervlak:
    oppervlak = Oppervlak()
    oppervlak.feed(html)
    return oppervlak


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


#: Per geval: het roos-sjabloon, het LOTC-sjabloon, de context, en of de roos-uitvoer nog
#: door ``process_components`` moet. Die laatste vraag volgt precies wat de route doet:
#: de voortgangsfragmenten renderen met opzet EEN keer.
GEVALLEN: list[tuple[str, str, str, dict[str, Any], bool]] = [
    (
        "samenvatting",
        "wizard/modal_wizard_review.html.j2",
        "bg/_modal-wizard-review.html.j2",
        REVIEW_CONTEXT,
        True,
    ),
    (
        "voortgang-start",
        "wizard/modal_wizard_progress.html.j2",
        "bg/_modal-wizard-progress.html.j2",
        PROGRESS_CONTEXT,
        False,
    ),
    (
        "opgeslagen",
        "wizard/modal_wizard_success.html.j2",
        "bg/_modal-wizard-success.html.j2",
        {},
        True,
    ),
    *[
        (
            f"voortgang-{status}",
            "wizard/modal_wizard_progress_fragment.html.j2",
            "bg/_modal-wizard-progress-fragment.html.j2",
            _fragment_context(status),
            False,
        )
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


STAP_GEVALLEN: list[tuple[str, str, str, dict[str, Any], bool]] = [
    (
        naam,
        "wizard/modal_wizard_step.html.j2",
        "bg/_modal-wizard-step.html.j2",
        context,
        True,
    )
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


def _render(roos: str, lotc: str, context: dict[str, Any], verwerk_roos: bool) -> tuple[Oppervlak, Oppervlak]:
    env = templates_lotc.env
    oud = env.get_template(roos).render(context)
    if verwerk_roos:
        oud = str(env.filters["process_components"](oud))
    nieuw = templates_lotc.env.get_template(lotc).render(context)
    return meet(oud), meet(nieuw)


@pytest.mark.parametrize(
    ("naam", "roos", "lotc", "context", "verwerk_roos"),
    [*GEVALLEN, *STAP_GEVALLEN],
    ids=[g[0] for g in (*GEVALLEN, *STAP_GEVALLEN)],
)
def test_het_fragment_kan_alles_wat_het_oude_kon(
    naam: str, roos: str, lotc: str, context: dict[str, Any], verwerk_roos: bool
) -> None:
    """Geen bestemming, geen htmx-adres, geen aanroep en geen veld mag wegvallen."""
    oud, nieuw = _render(roos, lotc, context, verwerk_roos)

    verdwenen: list[str] = []
    for label, a, b in (
        ("bestemming", oud.bestemmingen, nieuw.bestemmingen),
        ("htmx", oud.htmx, nieuw.htmx),
        ("js-functie", oud.functies, nieuw.functies),
        ("veld", oud.velden, nieuw.velden),
    ):
        verdwenen.extend(f"{label}: {weg}" for weg in sorted(a - b))

    verdwenen.extend(
        f"id: {weg}" for weg in sorted(oud.ids - nieuw.ids) if not any(sleutel in weg for sleutel in AANVAARDE_IDS)
    )

    assert not verdwenen, f"verdwenen gedrag in {naam}:\n  " + "\n  ".join(verdwenen)


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
