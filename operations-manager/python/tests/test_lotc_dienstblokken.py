"""De blokken die de DIENSTEN op de projectpagina leveren, in beide vormgevingen (RC-64).

Een dienst levert zijn eigen leesblok voor de projectdetailpagina
(``UIEvent.PROJECT_SECTIONS``, zie opi/services/registry.py). Die sjablonen staan bij hun
dienst en zijn in roos-componenten geschreven; de LOTC-pagina rendeerde ze daarom met
``render_roos()`` en zette het resultaat als HTML neer.

Dat was verantwoord met "zo'n blok ziet er dan anders uit, en dat is zichtbaar onaf". Die
redenering veronderstelt dat de rvo-klassen nog iets DOEN. Ze doen niets: de LOTC-omgeving
laadt ``["lotc-layout", "nldd", "lotc-forms"]`` en ``lotc_rvo`` staat daar niet bij. Het
resultaat was dus niet zichtbaar anders maar volledig onopgemaakt - kale HTML midden op de
projectpagina.

Elke dienst levert nu naast zijn ``section-detail.html.j2`` een ``-lotc``-tegenhanger. Het
bezwaar daartegen is echt: een tweede kopie loopt uit de pas zodra een dienst zijn sjabloon
wijzigt, en diensten zijn juist het deel van dit platform dat blijft groeien. Deze test is
het antwoord daarop en meet drie dingen:

1. **Geen dienst vergeet zijn tegenhanger.** Een nieuw ``section-detail.html.j2`` zonder
   ``-lotc``-buur faalt hier, zodat de kopie zichtbaar is in plaats van stil.
2. **De twee doen hetzelfde.** Gemeten met dezelfde meetlat als
   ``scripts/lotc_compare_behaviour.py``: elke bestemming, elk htmx-adres, elke aangeroepen
   JavaScript-functie en elk id. Vormgeving telt niet mee. Zo valt een knop die zijn
   aanroep kwijtraakt op voordat een gebruiker erop klikt.
3. **Er komt geen roos-HTML meer uit.** Gemeten op het gerenderde blok, want dat is waar de
   fout zat: in de bron van de LOTC-pagina was geen enkele rvo-klasse te vinden.
"""

from __future__ import annotations

import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from opi.core.templates import get_templates
from opi.core.templates_lotc import LOTC_TEMPLATE_SUFFIX, lotc_counterpart, templates_lotc

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from lotc_compare_behaviour import meet, vergelijk

CATALOG_DIR = Path(__file__).resolve().parents[1] / "opi" / "services" / "catalog"

#: Wat de roos-omgeving in elke component achterlaat. Op een LOTC-pagina is dit het bewijs
#: dat er HTML uit de andere omgeving is binnengekomen; LOTC zet ``data-lotc-component``.
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

#: Dienstsjablonen die niet hier maar in een andere test met hun tegenhanger vergeleken
#: worden, met de test erbij. Zo blijft zichtbaar dat ze GEMETEN zijn en niet vergeten.
ELDERS_GEMETEN = {
    # tests/test_lotc_fragmenten.py::test_het_backupblok_doet_in_beide_vormgevingen_hetzelfde
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
    """ELK sjabloon in de catalogus dat zelf geen LOTC-tegenhanger is.

    De poort keek hiervoor alleen naar ``*/section-detail.html.j2``, en dat was te smal:
    een dienst levert meer dan zijn projectblok. Het deploymentblok van metrics_scraper,
    het backupblok en de twee dialogen (job, databaseconsole) vielen er allemaal buiten, en
    precies daar zaten de gaten - de jobdialoog en de consoledialoog rendeerden nog met de
    ROOS-omgeving, dus ze kwamen op een NLDD-pagina onopgemaakt binnen.

    Nu telt elk ``.html.j2`` in de catalogus mee. Een dienst die er een bijlegt zonder
    tegenhanger faalt hier, ongeacht hoe het bestand heet.
    """
    return sorted(
        f"{pad.parent.name}/{pad.name}"
        for pad in CATALOG_DIR.glob("*/*.html.j2")
        if not pad.name.endswith(LOTC_TEMPLATE_SUFFIX)
    )


def test_elk_dienstblok_heeft_een_lotc_tegenhanger() -> None:
    """De poort uit de kop: een nieuwe dienst kan zijn tegenhanger niet vergeten.

    Zonder deze test is het lege blok in bg/_deployment-service-sections.html.j2 een
    uitnodiging: het blok verdwijnt dan stilletjes van de pagina, en niemand ziet het tot
    iemand de pagina opent.
    """
    zonder = [naam for naam in _alle_dienstsjablonen() if lotc_counterpart(naam) is None]

    assert zonder == [], (
        f"deze dienstsjablonen hebben geen LOTC-tegenhanger: {zonder}. "
        f"Leg er een <naam>-lotc.html.j2 naast in dezelfde dienstmap; zonder die tegenhanger "
        f"staat het blok niet op de projectpagina."
    )


def test_de_meetlijst_dekt_elk_dienstblok() -> None:
    """Een dienstblok dat hier niet in BLOKKEN staat wordt hieronder niet vergeleken."""
    assert sorted(BLOKKEN) == _detail_sjablonen()


def test_elk_dienstsjabloon_wordt_ergens_vergeleken() -> None:
    """Geen dienstsjabloon glipt langs BEIDE vergelijkingen.

    De tegenhanger-poort hierboven zegt alleen dat het BESTAND er is. Of het hetzelfde
    doet, meten ``test_het_lotc_blok_doet_hetzelfde_als_het_roos_blok`` hier en de
    backup- en metricstests in ``tests/test_lotc_fragmenten.py``. Deze test houdt die twee
    lijsten sluitend: een nieuw dienstsjabloon dat in geen van beide staat valt op.
    """
    gemeten = set(BLOKKEN) | set(FRAGMENTEN) | {naam for _, naam, _ in DIALOGEN} | ELDERS_GEMETEN
    ongemeten = [naam for naam in _alle_dienstsjablonen() if naam not in gemeten]

    assert ongemeten == [], (
        f"deze dienstsjablonen worden nergens met hun tegenhanger vergeleken: {ongemeten}. "
        f"Zet ze in BLOKKEN/FRAGMENTEN hierboven, of - als ze elders gemeten worden - in "
        f"ELDERS_GEMETEN met de test erbij."
    )


@pytest.mark.parametrize("naam", sorted(BLOKKEN) + sorted(FRAGMENTEN))
def test_het_lotc_blok_doet_hetzelfde_als_het_roos_blok(naam: str) -> None:
    """Zelfde bestemmingen, zelfde htmx, zelfde JavaScript-aanroepen, zelfde id's."""
    context = {**BLOKKEN, **FRAGMENTEN}[naam]
    lotc_naam = lotc_counterpart(naam)
    assert lotc_naam is not None, f"{naam} heeft geen LOTC-tegenhanger"

    roos_html = get_templates().env.get_template(naam).render(**context)
    lotc_html = templates_lotc.env.get_template(lotc_naam).render(**context)

    verschillen = vergelijk(meet(roos_html), meet(lotc_html))

    assert verschillen == [], f"{naam} en {lotc_naam} doen niet hetzelfde:\n" + "\n".join(verschillen)


@pytest.mark.parametrize("naam", sorted(BLOKKEN) + sorted(FRAGMENTEN))
def test_het_lotc_blok_bevat_geen_roos_html(naam: str) -> None:
    """De meting die de bron niet kan geven: wat de gebruiker krijgt."""
    context = {**BLOKKEN, **FRAGMENTEN}[naam]
    lotc_naam = lotc_counterpart(naam)
    assert lotc_naam is not None

    lotc_html = templates_lotc.env.get_template(lotc_naam).render(**context)

    assert ROOS_MARKER not in lotc_html
    assert "rvo-" not in lotc_html
    assert "<c-" not in lotc_html, "onvervangen componenttag: dit sjabloon rendert in de verkeerde omgeving"


def test_een_blok_zonder_tegenhanger_valt_terug_in_plaats_van_om() -> None:
    """De ondergrens blijft staan: lelijk is beter dan weg.

    Een dienst die morgen een blok toevoegt en de tegenhanger nog niet heeft, mag de
    projectpagina niet meenemen in zijn val.
    """
    assert lotc_counterpart("keycloak/bestaat-niet.html.j2") is None
    assert lotc_counterpart("keycloak/section-detail.txt") is None


@pytest.mark.parametrize(("geval", "naam", "context"), DIALOGEN, ids=[g for g, _, _ in DIALOGEN])
def test_de_dialoog_doet_in_beide_vormgevingen_hetzelfde(geval: str, naam: str, context: dict[str, Any]) -> None:
    """De job- en consoledialoog, per toestand: zelfde adressen, aanroepen en id's.

    Deze twee zijn tot nu toe alleen in roos gerenderd - jobs.py en db_console.py deden een
    kale ``TemplateResponse`` op het roos-sjabloon, zonder schakelaar. Op een NLDD-pagina
    kwam de dialoog daardoor in de oude vormgeving binnen, en die wordt daar door niets
    opgemaakt. Per toestand gemeten, want elke tak heeft zijn eigen knoppen.
    """
    lotc_naam = lotc_counterpart(naam)
    assert lotc_naam is not None, f"{naam} heeft geen LOTC-tegenhanger"

    roos_html = get_templates().env.get_template(naam).render(**context)
    lotc_html = templates_lotc.env.get_template(lotc_naam).render(**context)

    verschillen = vergelijk(meet(roos_html), meet(lotc_html))

    assert verschillen == [], f"{geval}: {naam} en {lotc_naam} doen niet hetzelfde:\n" + "\n".join(verschillen)


@pytest.mark.parametrize(("geval", "naam", "context"), DIALOGEN, ids=[g for g, _, _ in DIALOGEN])
def test_de_dialoog_bevat_geen_roos_html(geval: str, naam: str, context: dict[str, Any]) -> None:
    """Wat de gebruiker krijgt, niet wat er in de bron staat."""
    lotc_naam = lotc_counterpart(naam)
    assert lotc_naam is not None

    lotc_html = templates_lotc.env.get_template(lotc_naam).render(**context)

    assert ROOS_MARKER not in lotc_html
    assert "rvo-" not in lotc_html
    assert "<c-" not in lotc_html, "onvervangen componenttag: dit sjabloon rendert in de verkeerde omgeving"


def test_de_dialoogroutes_kiezen_de_vormgeving_van_het_verzoek() -> None:
    """De poort onder de twee tests hierboven: het sjabloon moet ook GEKOZEN worden.

    Twee gelijkwaardige sjablonen zijn niets waard als de route er altijd maar een van
    rendert. Precies dat was het geval: beide modules riepen ``get_templates()`` aan en
    noemden het roos-sjabloon, dus de tegenhanger werd nooit gebruikt. Gemeten op de
    BRON van de module en niet op een gerenderd antwoord, want de route erachter heeft een
    cluster nodig.
    """
    for module in ("jobs", "db_console"):
        bron = (CATALOG_DIR / "shared" / f"{module}.py").read_text()
        assert "get_templates()" not in bron, (
            f"{module}.py rendert nog rechtstreeks in de roos-omgeving; ga via "
            f"opi.web.lotc_switch.render() zodat het verzoek de vormgeving kiest"
        )
        assert "lotc=_MODAL_TEMPLATE_LOTC" in bron, f"{module}.py geeft geen LOTC-sjabloon mee aan render()"


def test_de_lotc_omgeving_kan_niet_meer_in_de_roos_omgeving_renderen() -> None:
    """``render_roos`` is weg en mag niet terugkomen.

    Die functie rendeerde een dienstblok in de ANDERE componentomgeving en zette het
    resultaat als HTML op een NLDD-pagina. Zolang hij bestaat is hij een uitnodiging: een
    dienst die zijn tegenhanger niet schrijft komt er dan alsnog in, ongestileerd, en de
    poort hierboven wordt vrijblijvend. Er is nu ook niets meer om op terug te vallen - de
    roos-omgeving zelf gaat weg.
    """
    assert "render_roos" not in templates_lotc.env.globals

    # De Jinja-COMMENTAAR eruit voordat we zoeken: verschillende sjablonen leggen in hun kop
    # uit dat hier render_roos() stond en waarom het weg is. Die uitleg is de bedoeling, en
    # een test die erop afgaat dwingt je hem te schrappen - dan verliest de volgende lezer
    # precies de reden waarom het zo werkt.
    sjablonen = Path(__file__).resolve().parents[1] / "opi" / "templates_lotc"
    roepen_aan = [
        pad.name
        for pad in sjablonen.rglob("*.j2")
        if "render_roos(" in re.sub(r"\{#.*?#\}", "", pad.read_text(), flags=re.DOTALL)
    ]
    assert roepen_aan == [], f"deze LOTC-sjablonen roepen render_roos() nog aan: {roepen_aan}"


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
    ``type="submit"``, of een element dat zelf een ``hx-post`` draagt. De vorm mag per
    vormgeving verschillen - de jobdialoog gebruikt nu een knop met hx-post plus
    hx-include, net als de consoledialoog ernaast - maar de mogelijkheid niet.
    """
    lotc_naam = lotc_counterpart(naam)
    assert lotc_naam is not None

    roos_html = get_templates().env.get_template(naam).render(**context)
    lotc_html = templates_lotc.env.get_template(lotc_naam).render(**context)

    def kan_versturen(html: str) -> bool:
        return bool(re.search(r'type="submit"', html) or re.search(r"hx-post=", html))

    assert kan_versturen(lotc_html) == kan_versturen(roos_html), (
        f"{geval}: het roos-blok kan {'wel' if kan_versturen(roos_html) else 'niet'} versturen "
        f"en het LOTC-blok {'wel' if kan_versturen(lotc_html) else 'niet'}. "
        f"Let op: op c-button is 'type' de vormgeving, en een <nldd-button> met "
        f'html-type="submit" dient een omliggende <form> NIET in - geef de knop zelf een '
        f"hx-post met hx-include, zoals de consoledialoog doet."
    )
