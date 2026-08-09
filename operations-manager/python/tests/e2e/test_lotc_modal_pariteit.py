"""De bewerkdialoog kan in de nieuwe vormgeving alles wat hij in de oude kon.

De pagina's van ZAD zijn omgezet naar LOTC; de INHOUD van de dialogen was dat niet. Je
opende "Projectleden beheren" en keek naar de oude vormgeving in een nieuw venster. Deze
poort staat onder de omzetting daarvan.

Elk dialoogfragment wordt twee keer opgehaald bij dezelfde route - ?layout=roos en
?layout=nldd - en het gedragsoppervlak wordt vergeleken: waar je heen kunt, wat htmx
ophaalt, welke JavaScript-functies aangeroepen worden, welke velden er zijn en welke id's
er staan waar htmx of het script aan hangt. Aan die velden en dat ene hx-post hangt het
OPSLAAN; valt er een weg, dan bewaart de dialoog stilletjes minder dan je invulde.

Waarom dit een aparte poort is naast tests/e2e/test_lotc_parity.py: die meet PAGINA'S.
Een fragment is geen pagina - het wordt pas na een klik in de dialoog gezet, en juist daar
glipt een omzetting langs de aandacht.

WAT DEZE POORT NIET DEKT: de samenvatting, de voortgang en de melding na opslaan. Die
komen pas na een POST met een geldig token en, voor de voortgang, een takendienst die de
testserver niet heeft. Ze worden op sjabloonniveau gemeten in
tests/test_lotc_modal_fragmenten.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
from tests.e2e.conftest import TEST_USER, _sign_session
from tests.test_lotc_modal_fragmenten import AANVAARDE_IDS, Oppervlak, meet

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.e2e

PROJECT = "test-project-detail"

#: De dialogen die de projectpagina opent. Elk is een eigen flow met eigen velden; ze
#: delen alleen de schil, en dat is precies waarom ze los gemeten worden.
FLOWS = [
    # De vier vensters die de projectpagina zelf opent.
    "modal-edit-identity",
    "modal-edit-team",
    "modal-edit-services",
    "modal-edit-attachments",
    # De genummerde flows: een component, een deployment, een webadres, een schema.
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
    # De configuratie van elke dienst die er een heeft. Ze delen de dialoog en de
    # formulierlaag, maar elk heeft eigen velden - en aan die velden hangt het opslaan.
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

FRAGMENTEN = [f"/projects/{PROJECT}/modal-wizard/{flow}" for flow in FLOWS]


@pytest.fixture
def client(app_server: str) -> Iterator[httpx.Client]:
    cookie = _sign_session({"user": TEST_USER})
    with httpx.Client(base_url=app_server, cookies={"session": cookie}, follow_redirects=True, timeout=60) as c:
        yield c


def _oppervlak(client: httpx.Client, pad: str, layout: str) -> Oppervlak:
    scheider = "&" if "?" in pad else "?"
    response = client.get(f"{pad}{scheider}layout={layout}")
    assert response.status_code == 200, f"{pad} ({layout}) gaf {response.status_code}"
    return meet(response.text)


@pytest.mark.parametrize("pad", FRAGMENTEN)
def test_het_dialoogfragment_kan_alles_wat_het_oude_kon(client: httpx.Client, pad: str) -> None:
    oud = _oppervlak(client, pad, "roos")
    nieuw = _oppervlak(client, pad, "nldd")

    verdwenen: list[str] = []
    for label, a, b in (
        ("bestemming", oud.bestemmingen, nieuw.bestemmingen),
        ("htmx", oud.htmx, nieuw.htmx),
        ("js-functie", oud.functies, nieuw.functies),
        ("veld", oud.velden, nieuw.velden),
    ):
        verdwenen.extend(f"{label}: {weg}" for weg in sorted(a - b))

    for weg in sorted(oud.ids - nieuw.ids):
        if any(sleutel in weg for sleutel in AANVAARDE_IDS):
            continue
        # Een id die nog steeds een VELDNAAM is, hoort bij een besturing die er gewoon
        # staat: alleen het id-attribuut ontbreekt op het element. Dat is een eigenschap
        # van een LOTC-component (c-checkbox-field neemt zijn id niet over) en niet iets
        # dat deze omzetting doet - het geldt net zo op de al omgezette wizardpagina, en
        # geen enkel script zoekt een veld op zijn pad-id op (gemeten in static/js/).
        if weg in nieuw.velden:
            continue
        verdwenen.append(f"id: {weg}")

    assert not verdwenen, f"verdwenen gedrag in {pad}:\n  " + "\n  ".join(verdwenen)


@pytest.mark.parametrize("pad", FRAGMENTEN)
def test_het_fragment_toont_geen_markup_als_tekst(client: httpx.Client, pad: str) -> None:
    """Geen enkele tag komt als letterlijke tekst op het scherm.

    Dit is geen theoretische zorg. De LOTC-omgeving heeft autoescape AAN (een eis van het
    componentensysteem), en een widget die de HTML van zijn kinderen invoegde zonder die
    als veilig te merken leverde daardoor de LETTERLIJKE tekst ``<nldd-button ...>`` op.
    Zichtbaar werd dat pas bij een reeks MET items, dus in de bewerkdialoog van een
    bestaand project eerder dan in de lege aanmaakwizard.
    """
    response = client.get(f"{pad}?layout=nldd")
    assert response.status_code == 200

    for verdacht in ("&lt;nldd-", "&lt;div", "&lt;c-", "&lt;button", "&lt;input"):
        assert verdacht not in response.text, f"{pad}: {verdacht} staat als tekst in de uitvoer"
