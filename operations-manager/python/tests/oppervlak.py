"""Wat een pagina of fragment DOET, los van hoe het eruitziet.

Deze meetlat is overgebleven uit ``scripts/lotc_compare_behaviour.py``, dat dezelfde route
twee keer ophaalde - een keer in de oude vormgeving, een keer in de nieuwe - en de twee
gedragsoppervlakken naast elkaar legde. Zo is de omzetting naar het nieuwe
componentensysteem gedaan: een keuzelijst, een knop en een filterveld zijn onderweg
stilzwijgend verdwenen, en geen van die drie gaf een foutmelding.

Die vergelijking kan niet meer. De oude vormgeving is weg, dus de ene helft van niets
meten tegen de andere zegt niets. Wat overblijft is de meting zelf, en die is nog steeds
het waard: gecombineerd met een VASTGELEGDE lijst (zie ``tests/oppervlak_snapshot.json``)
legt hij vast wat een pagina kan, met de pagina zelf als onderwerp in plaats van zijn
voorganger.

Gemeten wordt:

- elke bestemming: href, form action, en de ``*-href``-varianten die componenten schrijven
- elk htmx-adres: hx-get, hx-post, hx-delete, hx-put, hx-patch
- elke aangeroepen JavaScript-functie: uit onclick, @click, onchange, oninput, onsubmit
- elk besturingselement met een naam: input, select, textarea, en de webcomponenten die
  een echt formulierveld ZIJN
- elk id waar JavaScript of htmx aan kan hangen

Vormgeving telt NIET mee: tagnamen, klassen, teksten en stylesheets.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

HX_ATTRS = ("hx-get", "hx-post", "hx-delete", "hx-put", "hx-patch")
JS_ATTRS = ("onclick", "@click", "onchange", "oninput", "onsubmit")

#: Waarden die per verzoek verschillen en dus niets zeggen over gedrag: het wizard-token,
#: CSRF, cache-brekers. Zonder dit meldt elke vergelijking een verschil dat er niet is - en
#: een meetlat die altijd piept, houdt niemand in de gaten.
VLUCHTIG = re.compile(r"((?:_wizard_token|csrf|csrf_token|nonce|_ts|v)=)[^&\s\"']+", re.IGNORECASE)


def stabiel(waarde: str) -> str:
    """Vervang wat per verzoek verschilt door een vaste tekst."""
    return VLUCHTIG.sub(r"\1<wisselend>", waarde)


#: Achtervoegsels van id's die het design system ZELF bijmaakt om een veld aan zijn label
#: en zijn hulptekst te knopen (aria-describedby, label for). Ze zijn interne bedrading en
#: geen gedrag: niemand mikt er een link, een htmx-doel of een JavaScript-aanroep op.
AFGELEIDE_ID_ACHTERVOEGSELS = ("-label", "-help", "-error", "-helper", "-description")


def is_afgeleid_id(waarde: str) -> bool:
    return waarde.endswith(AFGELEIDE_ID_ACHTERVOEGSELS)


#: Webcomponenten die een echt formulierveld ZIJN, naast de ``*-field``-familie. Op "elke
#: nldd-tag met een name" gaat het mis: ``<nldd-icon name="envelope">`` draagt daar de
#: ICOONNAAM, en dan telt elk icoon als een veld.
BESTURING = {"nldd-select", "nldd-textarea", "nldd-checkbox", "nldd-radio", "nldd-switch", "nldd-combo-box"}


def is_besturing(tag: str) -> bool:
    return tag in BESTURING or (tag.startswith("nldd-") and tag.endswith("-field"))


def is_ruis(bestemming: str) -> bool:
    return bestemming.startswith(("/static/lotc/", "/static/css/", "/static/js/")) or "unpkg.com" in bestemming


class Oppervlak(HTMLParser):
    """Verzamelt wat een pagina DOET, los van hoe hij eruitziet."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.bestemmingen: set[str] = set()
        self.htmx: set[str] = set()
        self.functies: set[str] = set()
        self.velden: set[str] = set()
        self.ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        d = {k: (v or "") for k, v in attrs}

        # Elk thema noemt zijn link anders: het ene schrijft href, het andere op sommige
        # componenten website-href. Op de naam "href" alleen zou de thuislink hier als
        # ontbrekend gemeld worden terwijl hij er gewoon staat.
        for sleutel, waarde in d.items():
            if sleutel not in ("href", "action") and not sleutel.endswith("-href"):
                continue
            # Een anker of een javascript:-link is navigatie binnen de pagina.
            if waarde and not waarde.startswith(("#", "javascript:")) and not is_ruis(waarde):
                self.bestemmingen.add(stabiel(waarde))

        for sleutel in HX_ATTRS:
            if d.get(sleutel):
                self.htmx.add(f"{sleutel}={stabiel(d[sleutel])}")

        for sleutel in JS_ATTRS:
            self.functies.update(re.findall(r"([A-Za-z_$][\w$]*)\s*\(", d.get(sleutel, "")))

        besturing = (
            tag in ("input", "select", "textarea")
            or is_besturing(tag)
            or d.get("data-lotc-component") in ("text-input", "select-field", "textarea")
        )
        if besturing and d.get("name"):
            self.velden.add(d["name"])

        # Het id van zo'n webcomponent staat op ``input-id``: dat is het id dat het echte
        # <input> straks draagt, en dus waar een label of een hx-include naar wijst.
        for sleutel in ("id", "input-id"):
            if d.get(sleutel) and not is_afgeleid_id(d[sleutel]):
                self.ids.add(d[sleutel])


def meet(html: str) -> Oppervlak:
    oppervlak = Oppervlak()
    oppervlak.feed(html)
    return oppervlak


#: De categorieen zoals ze in de vastgelegde lijst staan.
CATEGORIEEN = ("bestemmingen", "htmx", "functies", "velden", "ids")


def als_lijsten(oppervlak: Oppervlak) -> dict[str, list[str]]:
    """Een gemeten oppervlak als gesorteerde lijsten, klaar om vast te leggen."""
    return {categorie: sorted(getattr(oppervlak, categorie)) for categorie in CATEGORIEEN}


def ontbreekt(vastgelegd: dict[str, list[str]], gemeten: Oppervlak) -> list[str]:
    """Wat de vastgelegde lijst noemt en de pagina niet meer heeft.

    Alleen deze richting. Iets ERBIJ is gewoon nieuw werk; iets dat WEG is, is bijna altijd
    een ongeluk - dat is precies wat er bij de omzetting drie keer gebeurde.
    """
    return [
        f"{categorie}: {item}"
        for categorie in CATEGORIEEN
        for item in vastgelegd.get(categorie, [])
        if item not in getattr(gemeten, categorie)
    ]
