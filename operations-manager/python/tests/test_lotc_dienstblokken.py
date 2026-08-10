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

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from opi.core.templates import get_templates
from opi.core.templates_lotc import lotc_counterpart, templates_lotc

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

#: Fragmenten die geen ``section`` krijgen maar wel in een LOTC-pagina terechtkomen. De
#: OTP-code wordt met htmx in het Keycloak-blok gezet, dus hij hoort bij dezelfde poort.
FRAGMENTEN: dict[str, dict[str, Any]] = {
    "keycloak/otp-code.html.j2": {"code": "123456", "project_name": "voorbeeld", "realm": "voorbeeld-realm"},
}


def _detail_sjablonen() -> list[str]:
    """Elke ``section-detail.html.j2`` in de catalogus, als sjabloonnaam."""
    return sorted(f"{pad.parent.name}/{pad.name}" for pad in CATALOG_DIR.glob("*/section-detail.html.j2"))


def test_elk_dienstblok_heeft_een_lotc_tegenhanger() -> None:
    """De poort uit de kop: een nieuwe dienst kan zijn tegenhanger niet vergeten.

    Zonder deze test is de terugval in bg/project-tabs.html.j2 een uitnodiging: het blok
    komt er dan met render_roos() alsnog in, ongestileerd, en niemand ziet het tot iemand
    de pagina opent.
    """
    zonder = [naam for naam in _detail_sjablonen() if lotc_counterpart(naam) is None]

    assert zonder == [], (
        f"deze dienstblokken hebben geen LOTC-tegenhanger: {zonder}. "
        f"Leg er een <naam>-lotc.html.j2 naast in dezelfde dienstmap; zonder die tegenhanger "
        f"rendert de projectpagina het blok met render_roos() en staat het er onopgemaakt op."
    )


def test_de_meetlijst_dekt_elk_dienstblok() -> None:
    """Een dienstblok dat hier niet in BLOKKEN staat wordt hieronder niet vergeleken."""
    assert sorted(BLOKKEN) == _detail_sjablonen()


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
