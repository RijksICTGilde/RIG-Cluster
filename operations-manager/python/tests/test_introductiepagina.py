"""De introductiepagina: bereikbaar zonder rechten, en waar over de diensten.

Twee dingen kunnen aan deze pagina stil kapotgaan, en ze zijn allebei onzichtbaar voor wie
hem opent.

1. **De inlogmuur.** De pagina is er voor iemand die nog GEEN rechten heeft. Komt er ooit
   een ``@requires_sso`` op - per ongeluk, of omdat iemand de routes eromheen op een hoop
   beveiligt - dan is hij precies voor zijn eigen publiek onbereikbaar, en merkt niemand
   dat: wie de pagina test is zelf ingelogd.

2. **Veroudering.** De dienstenlijst hoort bij de catalogus. Wordt hij ooit overgetypt of
   raakt de bron los, dan belooft de eerste pagina die iemand van ZAD ziet diensten die er
   niet zijn, of verzwijgt hij wat er wel is. Daarom wordt hier niet getoetst DAT er
   kaarten staan, maar dat er precies die staan die de catalogus levert.
"""

from typing import TYPE_CHECKING

from opi.services.registry import SERVICE_DEFINITIONS
from opi.services.services_enums import ServiceKind
from opi.web.lotc_switch import build_lotc_introductie

if TYPE_CHECKING:
    from fastapi.testclient import TestClient


def test_introductie_is_bereikbaar_zonder_sessie(test_client: TestClient) -> None:
    """Zonder cookie, zonder API-sleutel: gewoon 200 met de pagina."""
    response = test_client.get("/introductie")

    assert response.status_code == 200, response.text
    assert "Zelfservice Applicatie Deployment" in response.text


def test_de_route_draagt_geen_sso_eis() -> None:
    """De middleware leest ``_requires_sso`` van het endpoint; dat attribuut hoort er niet.

    Rechtstreeks op de functie en niet via een verzoek, want een 200 hierboven bewijst dit
    niet: bij een ontbrekende testconfiguratie kan een route om een heel andere reden
    doorlaten.
    """
    from opi.web.router import introductie

    assert getattr(introductie, "_requires_sso", False) is False


def test_root_stuurt_een_anonieme_bezoeker_naar_de_introductie(test_client: TestClient) -> None:
    """``/`` was een doorverwijzing naar het dashboard, en dus naar het inlogscherm."""
    response = test_client.get("/", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/introductie"


def _catalogus() -> tuple[list[str], list[str]]:
    """De labels die de catalogus levert, gesplitst zoals de pagina ze toont."""
    zelf = [d.name for d in SERVICE_DEFINITIONS.values() if not d.hidden and d.kind is not ServiceKind.SYSTEM]
    achtergrond = [d.name for d in SERVICE_DEFINITIONS.values() if not d.hidden and d.kind is ServiceKind.SYSTEM]
    return zelf, achtergrond


def test_de_dienstenlijst_is_die_van_de_catalogus() -> None:
    """Elke kaart komt uit ``SERVICE_DEFINITIONS``, en geen enkele dienst ontbreekt."""
    zelf, achtergrond = _catalogus()
    context = build_lotc_introductie(None)

    assert [d["label"] for d in context["diensten_zelf"]] == zelf
    assert [d["label"] for d in context["diensten_achtergrond"]] == achtergrond
    assert zelf, "de catalogus levert geen kiesbare diensten meer"
    assert achtergrond, "de catalogus levert geen systeemdiensten meer"


def test_verborgen_diensten_staan_er_niet_op() -> None:
    """``hidden=True`` betekent: OPI kent hem zelf toe, jij kunt hem niet kiezen.

    Op een pagina die uitlegt wat je kunt kiezen is zo'n dienst ruis. Het dienstenoverzicht
    toont ze wel, en bewust - daar zoek je omgevingsvariabelen op.
    """
    verborgen = {d.name for d in SERVICE_DEFINITIONS.values() if d.hidden}
    context = build_lotc_introductie(None)

    getoond = {d["label"] for d in context["diensten_zelf"]} | {d["label"] for d in context["diensten_achtergrond"]}
    assert verborgen, "geen enkele dienst is meer verborgen; dan zegt deze test niets"
    assert not (verborgen & getoond)


def test_elke_dienst_op_de_pagina_staat_er_ook_echt_op(test_client: TestClient) -> None:
    """De brug tussen de context en de HTML: wat de catalogus levert, wordt gerenderd.

    Zonder deze stap kan de lijst kloppen terwijl het sjabloon hem niet doorloopt.
    """
    zelf, achtergrond = _catalogus()
    pagina = test_client.get("/introductie").text

    for label in zelf + achtergrond:
        assert label in pagina, f"dienst ontbreekt op de pagina: {label}"


def test_de_omschrijving_komt_letterlijk_van_de_dienst() -> None:
    """Niet hertaald. Wat de dienst over zichzelf zegt, is wat de pagina belooft."""
    context = build_lotc_introductie(None)
    per_naam = {d.name: d for d in SERVICE_DEFINITIONS.values()}

    for kaart in context["diensten_zelf"] + context["diensten_achtergrond"]:
        assert kaart["summary"] == per_naam[kaart["label"]].description
