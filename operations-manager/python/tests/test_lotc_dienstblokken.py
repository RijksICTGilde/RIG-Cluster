"""De blokken die de DIENSTEN op de projectpagina en in hun dialogen leveren.

Een dienst levert zijn eigen leesblok voor de projectdetailpagina
(``UIEvent.PROJECT_SECTIONS``, zie opi/services/registry.py) plus soms een dialoog. Die
sjablonen staan bij hun dienst, en de pagina rendert ze zonder te weten welke dienst het
is - dus een dienst die zijn sjabloon niet levert of niet rendert, valt hier op en niet
pas op het scherm.

Hier stond de tegenhanger-poort. Zolang er twee bouwlijnen waren, lag naast elk
dienstsjabloon een ``-lotc``-versie, en deze tests legden die twee naast elkaar: zelfde
bestemmingen, zelfde htmx, zelfde JavaScript-aanroepen, zelfde id's. Dat was de meetlat
die de omzetting mogelijk maakte, en hij is nu leeg: er is nog EEN sjabloon per dienst.

Wat ervoor in de plaats komt meet de sjablonen die er zijn:

1. **Elk dienstsjabloon rendert**, met gegevens in de vorm die de dienst zelf oplevert.
   Een componenttag met een attribuut dat niet bestaat breekt hier, niet in de browser.
2. **Er komt geen markup van het oude systeem uit** - geen ``rvo-``, geen
   ``data-roos-component``, en geen onvervangen ``<c-``.
3. **Elke dialoog kan zijn gegevens nog wegsturen.** Dat is de les die twee keer geld
   kostte: een dode knop ziet er precies zo uit als een levende.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from opi.core.templates_lotc import templates_lotc

CATALOG_DIR = Path(__file__).resolve().parents[1] / "opi" / "services" / "catalog"

#: Wat het OUDE componentensysteem in elke component achterliet. Het staat hier nog omdat
#: het het enige spoor is dat overblijft als er ooit weer HTML uit een tweede
#: renderomgeving binnenkomt; dit systeem zet ``data-lotc-component``.
ROOS_MARKER = "data-roos-component"


def _section(context: dict[str, Any]) -> SimpleNamespace:
    """Wat de pagina aan het sjabloon geeft: een ``DetailPageSection`` met zijn context."""
    return SimpleNamespace(context=context)


#: Per dienstblok de gegevens die de dienst zelf aanlevert. Verzonnen, maar in de VORM die
#: de dienst oplevert - een andere vorm zou een vergelijking opleveren die niets bewijst.
BLOKKEN: dict[str, dict[str, Any]] = {
    "attachments/section-detail.html.j2": {
        "section": _section(
            {
                "attachments": [{"id": "a1", "filename": "certificaat.pem"}],
                "can_edit": True,
                "project_name": "voorbeeld",
            }
        ),
    },
    "invite/section-detail.html.j2": {
        "section": _section(
            {"invites": [{"key": "sleutel-1", "realm_roles": ["beheerder"], "contact_email": "a@b.nl"}]}
        ),
        # De echte pagina heeft ``url_for`` van Starlette; hier is alleen de UITKOMST
        # interessant, en die moet in beide vormgevingen dezelfde bestemming opleveren.
        "url_for": lambda naam, **kw: f"https://zad.example/invite/{kw['key']}",
    },
    "keycloak/section-detail.html.j2": {
        "section": _section(
            {
                "realms": [
                    {
                        "host": "https://kc.example",
                        "realm": "voorbeeld-realm",
                        "username": "voorbeeld_admin",
                        "password": "VOORBEELDWAARDE-geen-echt-geheim",
                        "has_totp": True,
                    }
                ]
            }
        ),
        "project": {"name": "voorbeeld"},
    },
}

#: Een verzoek zoals de dialoogsjablonen het lezen: alleen ``request.state.csrf_token``,
#: dat in de hx-headers van elke muterende knop terechtkomt.
_VERZOEK = SimpleNamespace(state=SimpleNamespace(csrf_token="VOORBEELD-CSRF-TOKEN"))

#: Fragmenten die geen ``section`` krijgen maar wel in een LOTC-pagina terechtkomen. De
#: OTP-code wordt met htmx in het Keycloak-blok gezet, dus hij hoort bij dezelfde poort.
#:
#: De twee dialogen staan hier per TOESTAND. Ze renderen een andere tak per toestand, en
#: alleen de takken die je rendert worden vergeleken: met enkel de standaardtoestand blijft
#: de knop "Nu stoppen" - de enige die iets afbreekt - ongemeten.
FRAGMENTEN: dict[str, dict[str, Any]] = {
    "keycloak/otp-code.html.j2": {"code": "123456", "project_name": "voorbeeld", "realm": "voorbeeld-realm"},
}

#: De dialogen, per toestand. Aparte lijst omdat een naam hier meer dan een keer voorkomt
#: en een dict dat niet kan; de sjabloonnaam blijft de sleutel voor de tegenhanger.
_JOB = SimpleNamespace(
    name="job-voorbeeld",
    image="ghcr.io/voorbeeld/migratie:1",
    command="alembic upgrade head",
    opened_by="a@b.nl",
    session_id="sessie-1",
    state="running",
    expires_at=datetime(2026, 8, 10, 14, 30, tzinfo=UTC),
)
_SESSIE = SimpleNamespace(
    tool="pgweb",
    opened_by="a@b.nl",
    session_id="sessie-2",
    url="https://console.voorbeeld/",
    expires_at=datetime(2026, 8, 10, 14, 30, tzinfo=UTC),
)
_DIALOOG_BASIS: dict[str, Any] = {
    "request": _VERZOEK,
    "project_name": "voorbeeld",
    "deployment_name": "dep1",
    "error": None,
    "ttl_seconds": 1800,
    "enabled": True,
}
DIALOGEN: list[tuple[str, str, dict[str, Any]]] = [
    ("job/nieuw", "shared/_job-modal.html.j2", {**_DIALOOG_BASIS, "job": None, "state": "none"}),
    ("job/draait", "shared/_job-modal.html.j2", {**_DIALOOG_BASIS, "job": _JOB, "state": "running"}),
    (
        "job/mislukt",
        "shared/_job-modal.html.j2",
        {**_DIALOOG_BASIS, "job": _JOB, "state": "failed"},
    ),
    (
        "job/fout",
        "shared/_job-modal.html.j2",
        {**_DIALOOG_BASIS, "job": None, "state": "none", "error": "Het ging mis"},
    ),
    ("job/uit", "shared/_job-modal.html.j2", {**_DIALOOG_BASIS, "job": None, "state": "none", "enabled": False}),
    ("console/nieuw", "shared/_db-console-modal.html.j2", {**_DIALOOG_BASIS, "session": None, "state": "none"}),
    (
        "console/actief",
        "shared/_db-console-modal.html.j2",
        {**_DIALOOG_BASIS, "session": _SESSIE, "state": "running"},
    ),
    (
        "console/start",
        "shared/_db-console-modal.html.j2",
        {**_DIALOOG_BASIS, "session": None, "state": "starting"},
    ),
    (
        "console/uit",
        "shared/_db-console-modal.html.j2",
        {**_DIALOOG_BASIS, "session": None, "state": "none", "enabled": False},
    ),
]

#: Dienstsjablonen die niet hier maar in een andere test gerenderd worden, met de test
#: erbij. Zo blijft zichtbaar dat ze GEMETEN zijn en niet vergeten.
ELDERS_GEMETEN = {
    # tests/test_lotc_fragmenten.py::test_het_backupblok_rendert_met_zijn_snapshots
    "shared/_backup-snapshots.html.j2",
    # Wordt door _backup-snapshots.html.j2 ingesloten en gaat in dezelfde meting mee.
    "shared/_backup-snapshots-one.html.j2",
    # tests/e2e/test_lotc_pariteit.py meet dit blok op de pagina zelf.
    "shared/section-backups.html.j2",
    # tests/test_lotc_fragmenten.py::test_de_tijdvakknoppen_wijzen_naar_een_id_dat_bestaat
    "metrics_scraper/section-deployment.html.j2",
}


def _detail_sjablonen() -> list[str]:
    """Elke ``section-detail.html.j2`` in de catalogus, als sjabloonnaam."""
    return sorted(f"{pad.parent.name}/{pad.name}" for pad in CATALOG_DIR.glob("*/section-detail.html.j2"))


def _alle_dienstsjablonen() -> list[str]:
    """ELK sjabloon in de catalogus.

    De poort keek hiervoor alleen naar ``*/section-detail.html.j2``, en dat was te smal:
    een dienst levert meer dan zijn projectblok. Het deploymentblok van metrics_scraper,
    het backupblok en de twee dialogen (job, databaseconsole) vielen er allemaal buiten, en
    precies daar zaten de gaten.
    """
    return sorted(f"{pad.parent.name}/{pad.name}" for pad in CATALOG_DIR.glob("*/*.html.j2"))


def test_de_meetlijst_dekt_elk_dienstblok() -> None:
    """Een dienstblok dat hier niet in BLOKKEN staat wordt hieronder niet gerenderd."""
    assert sorted(BLOKKEN) == _detail_sjablonen()


def test_elk_dienstsjabloon_wordt_ergens_gerenderd() -> None:
    """Geen dienstsjabloon glipt langs ALLE metingen.

    Dat een sjabloon bestaat zegt niets; of het rendert wel. Deze test houdt de lijsten
    sluitend, zodat een nieuw dienstsjabloon dat in geen enkele meting staat opvalt.
    """
    gemeten = set(BLOKKEN) | set(FRAGMENTEN) | {naam for _, naam, _ in DIALOGEN} | ELDERS_GEMETEN
    ongemeten = [naam for naam in _alle_dienstsjablonen() if naam not in gemeten]

    assert ongemeten == [], (
        f"deze dienstsjablonen worden nergens gerenderd: {ongemeten}. "
        f"Zet ze in BLOKKEN/FRAGMENTEN/DIALOGEN hierboven, of - als ze elders gemeten "
        f"worden - in ELDERS_GEMETEN met de test erbij."
    )


def _render(naam: str, context: dict[str, Any]) -> str:
    return templates_lotc.env.get_template(naam).render(**context)


@pytest.mark.parametrize("naam", sorted(BLOKKEN) + sorted(FRAGMENTEN))
def test_het_dienstblok_rendert_zonder_markup_van_het_oude_systeem(naam: str) -> None:
    """De meting die de bron niet kan geven: wat de gebruiker krijgt."""
    html = _render(naam, {**BLOKKEN, **FRAGMENTEN}[naam])

    assert html.strip(), f"{naam} levert een leeg blok op"
    assert ROOS_MARKER not in html
    assert "rvo-" not in html
    assert "<c-" not in html, "onvervangen componenttag: dit sjabloon rendert in de verkeerde omgeving"


@pytest.mark.parametrize(("geval", "naam", "context"), DIALOGEN, ids=[g for g, _, _ in DIALOGEN])
def test_de_dialoog_rendert_zonder_markup_van_het_oude_systeem(geval: str, naam: str, context: dict[str, Any]) -> None:
    """Per toestand gemeten, want elke tak heeft zijn eigen knoppen."""
    html = _render(naam, context)

    assert html.strip(), f"{geval}: {naam} levert een lege dialoog op"
    assert ROOS_MARKER not in html
    assert "rvo-" not in html
    assert "<c-" not in html, "onvervangen componenttag: dit sjabloon rendert in de verkeerde omgeving"


def test_de_dialoogroutes_gaan_langs_de_gedeelde_render() -> None:
    """Twee sjablonen zijn niets waard als de route er langs rendert.

    Precies dat was het geval: beide modules riepen ``templates_lotc`` rechtstreeks aan.
    Gemeten op de BRON van de module en niet op een gerenderd antwoord, want de route
    erachter heeft een cluster nodig.
    """
    for module in ("jobs", "db_console"):
        bron = (CATALOG_DIR / "shared" / f"{module}.py").read_text()
        assert "templates_lotc" not in bron, f"{module}.py rendert rechtstreeks; ga via opi.web.lotc_switch.render()"
        assert "template=_MODAL_TEMPLATE" in bron, f"{module}.py geeft geen sjabloon mee aan render()"


#: Welke dialoogtoestanden hun gegevens moeten kunnen WEGSTUREN, en welke niet. Als lijst
#: en niet afgeleid uit een vergelijking met een tweede sjabloon: die tweede is er niet
#: meer, en een verwachting die uit de meting zelf komt bewijst niets.
KAN_VERSTUREN = {
    "job/uit": False,
    "console/uit": False,
    "console/actief": True,
    # De console is aan het STARTEN: er is nog niets om heen te sturen, alleen een wachtbeeld.
    "console/start": False,
}


@pytest.mark.parametrize(("geval", "naam", "context"), DIALOGEN, ids=[g for g, _, _ in DIALOGEN])
def test_de_dialoog_kan_nog_versturen(geval: str, naam: str, context: dict[str, Any]) -> None:
    """Kan deze dialoog zijn gegevens nog wegsturen? Een dode knop ziet er precies zo uit als een levende.

    Dit gat kostte de jobdialoog zijn knop, twee keer achter elkaar. Eerst omdat op
    ``c-button`` het attribuut ``type`` de VORMGEVING is en het HTML-attribuut
    ``html-type`` heet, dus de component schreef zelf ``type="button"``. En daarna omdat
    een ``<nldd-button>`` met ``html-type="submit"`` het submit-event van de omliggende
    ``<form>`` niet bereikt - zelfs ``form.requestSubmit()`` leverde geen verzoek op.

    Beide keren meldde de gedragsvergelijking niets: het ``hx-post`` stond op het
    FORMULIER en dat was in beide vormgevingen gelijk. De knop stond er, hij zag er goed
    uit, en er vertrok niets.

    Daarom wordt hier niet geteld hoe er verstuurd wordt maar OF dat kan: een element met
    ``type="submit"``, of een element dat zelf een ``hx-post`` draagt.
    """
    html = _render(naam, context)
    kan = bool(re.search(r'type="submit"', html) or re.search(r"hx-post=", html))

    verwacht = KAN_VERSTUREN.get(geval, True)
    assert kan is verwacht, (
        f"{geval}: deze dialoog kan {'wel' if kan else 'niet'} versturen en dat hoort "
        f"{'wel' if verwacht else 'niet'} te kunnen. Let op: op c-button is 'type' de "
        f'vormgeving, en een <nldd-button> met html-type="submit" dient een omliggende '
        f"<form> NIET in - geef de knop zelf een hx-post met hx-include, zoals de "
        f"consoledialoog doet."
    )
