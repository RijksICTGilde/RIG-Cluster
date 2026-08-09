"""Markup in een attribuutwaarde: het gat waar de knop "Logs bekijken" in viel.

WAT ER MISGING

De knop rendeerde als:

    <nldd-button onclick="openLogViewer("amt-odc-prd", "productie", ...)">

Het attribuut sluit bij het eerste binnenste aanhalingsteken. De browser leest de rest
als losse attributen, de knop doet niets, en hij ziet er vreemd uit. Er kwam geen
foutmelding: de HTML is geldig, alleen niet wat er bedoeld was.

DE OORZAAK, EN WAAROM HIJ ZO GOED VERSTOPT ZIT

``tojson`` levert ``Markup`` op - Jinja's manier om te zeggen "dit is al veilig, niet
meer escapen". LOTC escapet ``:attrs``-waarden netjes met ``| e``, maar dat filter laat
markup met opzet ongemoeid. Het resultaat is dat juist de waarden die aanhalingstekens
BEVATTEN als enige niet geescaped worden.

Het origineel had er geen last van: daar staat het onclick-attribuut tussen ENKELE
aanhalingstekens, dus de dubbele uit de JSON passen erbinnen. Bij de omzetting naar een
component is die keuze weggevallen, want het component schrijft zijn eigen attributen.

``| forceescape`` escapet ook markup en is daarmee de juiste afsluiting van elke waarde
die in een attribuut belandt.

WAAROM DIT EEN EIGEN TEST HEEFT

Geen van de bestaande lagen ving dit. De compilatiecontrole ziet een geldig component
met geldige attributen. De gedragsvergelijking vergelijkt de NAAM van de aangeroepen
functie, en die stond aan beide kanten. Een screenshot toont een knop. Alleen wie op de
knop drukt of de HTML LEEST merkt het - en dat is precies wat hieronder gebeurt.
"""

import re

import pytest
from opi.core.templates_lotc import templates_lotc

# Elk sjabloon dat een JSON-waarde in een attribuut zet, met de context die het nodig
# heeft. Komt er een bij, dan hoort hij hier.
GEVALLEN = [
    pytest.param(
        '{% set js = ("openLogViewer(" ~ (naam | tojson) ~ ", " ~ (comps | tojson) ~ ")") | forceescape %}'
        '<c-button label="Logs bekijken" :attrs="{\'onclick\': js}" />',
        {"naam": 'pro"ject', "comps": [{"reference": "web"}]},
        id="js-aanroep-via-attrs",
    ),
    pytest.param(
        '<c-secret-field value="{{ waarden | tojson(indent=2) | forceescape }}" />',
        {"waarden": {"replicaCount": 2, "image": {"tag": "v1"}}},
        id="json-in-een-value",
    ),
]


@pytest.mark.parametrize(("bron", "context"), GEVALLEN)
def test_een_json_waarde_sluit_zijn_attribuut_niet_voortijdig(bron: str, context: dict) -> None:
    """Na het escapen staat er geen enkel kaal aanhalingsteken meer IN een attribuut."""
    uitvoer = templates_lotc.env.from_string(bron).render(**context)

    # Elk attribuut van elke tag uitknippen en tellen hoeveel er zijn. Klopt het aantal
    # openings- en sluittekens niet, dan is een attribuut voortijdig gesloten.
    for tag in re.findall(r"<[a-zA-Z][^>]*>", uitvoer):
        assert tag.count('"') % 2 == 0, f"oneven aantal aanhalingstekens, attribuut sluit voortijdig: {tag[:200]}"

    assert "&#34;" in uitvoer or "&quot;" in uitvoer, (
        "er is niets geescaped; dan heeft forceescape zijn werk niet gedaan"
    )


def test_zonder_forceescape_gaat_het_wel_mis() -> None:
    """De keerzijde: zonder het filter breekt het attribuut aantoonbaar.

    Zonder deze helft zou de test hierboven ook slagen op een sjabloon dat toevallig
    geen aanhalingstekens bevat, en dan bewaakt hij niets.
    """
    kapot = templates_lotc.env.from_string(
        '{% set js = "f(" ~ (naam | tojson) ~ ")" %}<c-button label="x" :attrs="{\'onclick\': js}" />'
    ).render(naam="project")

    knop = re.search(r"<nldd-button[^>]*>", kapot)
    assert knop, kapot
    tag = knop.group(0)

    # Het bewijs is niet het AANTAL aanhalingstekens - dat blijft even zolang er een
    # paar rauw doorheen glipt - maar de vraag of ze geescaped zijn. Staat er geen
    # enkele entiteit, dan is de waarde ongewijzigd in het attribuut beland en sluit
    # dat attribuut bij het eerste teken uit de JSON.
    verouderd = "de waarde wordt inmiddels wel geescaped - escapet LOTC markup nu zelf? Dan kan forceescape uit de sjablonen, en deze test weg."
    assert "&#34;" not in tag, verouderd
    assert "&quot;" not in tag, verouderd
    assert 'onclick="f("project")"' in tag, f"onverwachte vorm: {tag[:200]}"


def test_de_knop_logs_bekijken_draagt_een_hele_aanroep() -> None:
    """Het echte sjabloon, niet een nagebouwd fragment.

    De vorige twee toetsen een patroon. Deze toetst het BESTAND waar de fout in zat, want
    een patroon dat elders klopt zegt niets over de plek die stuk was.
    """
    context = {
        "project": {"name": "amt-odc-prd"},
        "deployment": {
            "name": "productie",
            "cluster": "local",
            "components": [{"reference": "component-1", "image": "nginx:latest"}],
        },
        "argocd_status": {"productie": {"health": "Healthy", "sync": "Synced", "errors": []}},
        "deployment_states": {},
        "current_cluster": "local",
    }
    uitvoer = templates_lotc.env.get_template("bg/_argocd-deployment-card.html.j2").render(**context)

    knop = re.search(r"<nldd-button[^>]*Logs bekijken[^>]*>", uitvoer)
    assert knop, "de knop 'Logs bekijken' staat niet meer op de kaart"
    tag = knop.group(0)
    assert tag.count('"') % 2 == 0, f"het onclick-attribuut sluit voortijdig: {tag[:200]}"
    assert "openLogViewer(&#34;amt-odc-prd&#34;" in tag, f"de aanroep is niet compleet: {tag[:200]}"
