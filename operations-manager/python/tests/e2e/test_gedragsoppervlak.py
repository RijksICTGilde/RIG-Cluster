"""Wat elke pagina en elk dialoogfragment KAN, vastgelegd als lijst.

DE POORT DIE HIER STOND, EN WAAROM HIJ WEG IS

Tot RC-67 haalden ``test_lotc_parity.py`` en ``test_lotc_modal_pariteit.py`` dezelfde route
twee keer op - ``?layout=roos`` en ``?layout=nldd`` - en vergeleken het gedragsoppervlak:
waar je heen kunt, wat htmx ophaalt, welke JavaScript-functies aangeroepen worden, welke
invoervelden er zijn en welke id's er staan waar script of htmx aan hangt. Dat is de reden
dat de omzetting zo ver gekomen is: er is meer dan eens iets stilzwijgend verdwenen - een
keuzelijst voor deployments, een knop die een venster opende, invoervelden van een filter -
en geen van die dingen gaf een foutmelding. De pagina rendert, hij doet alleen minder.

Met de oude pagina weg meet die vergelijking de ene helft van niets tegen de andere.

WAT ERVOOR IN DE PLAATS KOMT

Dezelfde meting (``tests/oppervlak.py``), maar met een VASTGELEGDE LIJST als bron in plaats
van de oude pagina: ``tests/oppervlak_snapshot.json`` noemt per route en per fragment welke
bestemmingen, htmx-adressen, aanroepen, velden en id's er horen te zijn. De test faalt als
er iets van die lijst VERDWIJNT; iets erbij is gewoon nieuw werk.

Dat is minder elegant en het veroudert, maar het is eerlijk: eerst was de oude pagina de
norm, nu is de nieuwe het.

DE LIJST BIJWERKEN

    ZAD_SCHRIJF_OPPERVLAK=1 uv run pytest tests/e2e/test_gedragsoppervlak.py -m e2e

Dat herschrijft het bestand met wat er vandaag staat. Lees de diff dan ook echt: elke regel
die verdwijnt is gedrag dat verdwijnt, en dat is precies wat deze poort hoort te vangen.

WAT DEZE TEST NIET DEKT, en dat hoor je te weten voor je hem vertrouwt: hij meet wat er in
de HTML staat, en dus alleen gedrag dat bij de gegevens van de testserver ZICHTBAAR is. Die
server heeft geen Prometheus en geen ArgoCD, dus blokken die daarvan afhangen - de meters
op het dashboard, de statuskaarten - staan er niet in.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest
from opi.web.lotc_switch import project_tab_url
from tests.e2e.conftest import TEST_USER, _sign_session
from tests.oppervlak import als_lijsten, meet, ontbreekt

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.e2e

#: Het project dat de e2e-testserver meelevert.
PROJECT = "test-project-detail"

SNAPSHOT = Path(__file__).resolve().parents[1] / "oppervlak_snapshot.json"

#: De omgevingsvariabele die van deze poort een schrijver maakt. Zie de moduledocstring.
SCHRIJVEN = os.environ.get("ZAD_SCHRIJF_OPPERVLAK") == "1"

ROUTES = [
    "/dashboard",
    "/projects",
    "/services",
    "/admin/users",
    "/admin/approvals",
    "/admin/usage",
    "/metrics-explorer",
    "/about",
    "/forms/wizard/start",
]

#: De projectpagina geeft elk tabblad een eigen URL, dus elk tabblad is een eigen meting.
TABBLADEN = ("project", "componenten", "services", "deployments", "metrics", "taken")

#: De dialogen die de projectpagina opent. Elk is een eigen flow met eigen velden; ze delen
#: alleen de schil, en dat is precies waarom ze los gemeten worden. Aan die velden en dat
#: ene hx-post hangt het OPSLAAN; valt er een weg, dan bewaart de dialoog stilletjes minder
#: dan je invulde.
FLOWS = [
    "modal-edit-identity",
    "modal-edit-team",
    "modal-edit-services",
    "modal-edit-attachments",
    "modal-edit-component-0",
    "modal-edit-component-1",
    "modal-add-deployment-1",
    "modal-edit-deployment-0",
    "modal-edit-domain-0",
    "modal-edit-backup-schedule-0",
    # Backup en restore: geen bewerking maar een taak, met een eigen knoppentekst
    # ("Verder" in plaats van "Opslaan"). Juist daarom horen ze erbij.
    "modal-backup",
    "modal-restore",
    # De configuratie van elke dienst die er een heeft.
    "modal-edit-keycloak-config",
    "modal-edit-auth-wall-config",
    "modal-edit-postgresql-schemas",
    "modal-edit-postgresql-config",
    "modal-edit-minio-config",
    "modal-edit-redis-config",
    "modal-edit-sleep-mode-config",
    "modal-edit-invite-config",
    "modal-edit-cross-domain-config",
]

FRAGMENTEN = [
    f"/projects/details/{PROJECT}/backups",
    f"/projects/{PROJECT}/tasks",
    f"/projects/{PROJECT}/actions/refresh-project/confirm",
    *[f"/projects/{PROJECT}/modal-wizard/{flow}" for flow in FLOWS],
]

PADEN = [*ROUTES, *[project_tab_url(PROJECT, tab) for tab in TABBLADEN], *FRAGMENTEN]


@pytest.fixture(scope="module")
def client(app_server: str) -> Iterator[httpx.Client]:
    cookie = _sign_session({"user": TEST_USER})
    with httpx.Client(base_url=app_server, cookies={"session": cookie}, follow_redirects=True, timeout=60) as c:
        yield c


@pytest.fixture(scope="module")
def vastgelegd() -> dict[str, dict[str, list[str]]]:
    if not SNAPSHOT.exists():
        return {}
    return json.loads(SNAPSHOT.read_text())


@pytest.fixture(scope="module")
def geschreven() -> Iterator[dict[str, dict[str, list[str]]]]:
    """Verzamelt de metingen en schrijft ze weg als erom gevraagd is."""
    verzameld: dict[str, dict[str, list[str]]] = {}
    yield verzameld
    if SCHRIJVEN and verzameld:
        bestaand = json.loads(SNAPSHOT.read_text()) if SNAPSHOT.exists() else {}
        bestaand.update(verzameld)
        SNAPSHOT.write_text(json.dumps(bestaand, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def test_de_lijst_bestaat_en_dekt_elk_pad(vastgelegd: dict[str, dict[str, list[str]]]) -> None:
    """Bewaak de bewaker: een lege of onvolledige lijst maakt elke toets hieronder gratis groen."""
    if SCHRIJVEN:
        pytest.skip("de lijst wordt in deze run opnieuw geschreven")

    ontbrekend = [pad for pad in PADEN if pad not in vastgelegd]
    assert not ontbrekend, (
        "deze paden staan niet in oppervlak_snapshot.json en worden dus niet bewaakt:\n  "
        + "\n  ".join(ontbrekend)
        + "\nDraai ZAD_SCHRIJF_OPPERVLAK=1 pytest tests/e2e/test_gedragsoppervlak.py om ze vast te leggen."
    )


#: Wat er per pad NIET vastgelegd wordt, met de reden.
#:
#: De bewerkdialoog van een component toont per DIENST die dat component gebruikt een
#: configuratieblok, en welke diensten dat zijn staat in het projectbestand. Andere tests
#: in deze suite bewerken datzelfde project, dus die blokken verschillen tussen een run
#: van dit bestand alleen en een run van de hele suite. Een lijst die daarvan afhangt
#: piept willekeurig, en een meetlat die willekeurig piept ga je negeren.
#:
#: De rest van die dialogen - naam, image, poorten, het opslaan-adres - ligt gewoon vast.
NIET_VASTLEGGEN = {
    "_services-config{": "hangt af van de diensten die een ander testgeval op dit project zette",
}


def _vastlegbaar(lijsten: dict[str, list[str]]) -> dict[str, list[str]]:
    return {
        categorie: [item for item in items if not any(sleutel in item for sleutel in NIET_VASTLEGGEN)]
        for categorie, items in lijsten.items()
    }


@pytest.mark.parametrize("pad", PADEN)
def test_de_pagina_kan_nog_alles_wat_er_vastligt(
    client: httpx.Client,
    vastgelegd: dict[str, dict[str, list[str]]],
    geschreven: dict[str, dict[str, list[str]]],
    pad: str,
) -> None:
    response = client.get(pad)
    assert response.status_code == 200, f"{pad} gaf {response.status_code}"

    gemeten = meet(response.text)

    if SCHRIJVEN:
        geschreven[pad] = _vastlegbaar(als_lijsten(gemeten))
        return

    weg = ontbreekt(vastgelegd.get(pad, {}), gemeten)
    assert not weg, (
        f"verdwenen gedrag op {pad}:\n  "
        + "\n  ".join(weg)
        + "\nIs dat de bedoeling, werk dan oppervlak_snapshot.json bij MET de reden in de PR."
    )


@pytest.mark.parametrize("pad", FRAGMENTEN)
def test_het_fragment_toont_geen_markup_als_tekst(client: httpx.Client, pad: str) -> None:
    """Geen enkele tag komt als letterlijke tekst op het scherm.

    Dit is geen theoretische zorg. De omgeving heeft autoescape AAN (een eis van het
    componentensysteem), en een widget die de HTML van zijn kinderen invoegde zonder die
    als veilig te merken leverde daardoor de LETTERLIJKE tekst ``<nldd-button ...>`` op.
    Zichtbaar werd dat pas bij een reeks MET items, dus in de bewerkdialoog van een
    bestaand project eerder dan in de lege aanmaakwizard.
    """
    response = client.get(pad)
    assert response.status_code == 200

    for verdacht in ("&lt;nldd-", "&lt;div", "&lt;c-", "&lt;button", "&lt;input"):
        assert verdacht not in response.text, f"{pad}: {verdacht} staat als tekst in de uitvoer"
