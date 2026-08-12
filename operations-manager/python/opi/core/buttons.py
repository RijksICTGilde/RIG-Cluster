"""De knoppenregel: welke varianten en welke maten een ``<c-button>`` mag dragen.

Eén plek, want deze twee vocabulaires stonden nergens: het component kent ze wel, maar
het STRUIKELT niet over een woord dat het niet kent. ``type="submit"`` (dat is geen
variant maar het HTML-type, dat in LOTC ``html-type`` heet) leverde een knop op zonder
enkele stijlklasse, en ``size="lg"`` levert een knop op zonder maatklasse. Allebei
zonder foutmelding, allebei zichtbaar als een knop die net anders is dan zijn buren.

De lijst met varianten is geen voorkeur maar een meting, uit twee bronnen die het eens
zijn: ``lotc_nldd/renderers.py`` (``_BUTTON_VARIANT_MAP``, het thema waarin het portaal
draait) en ``lord_of_the_components/templates/components/button.html.j2`` (de kale
bouwlijn). Het NLDD-thema geeft een woord dat niet in zijn tabel staat ONGEWIJZIGD door
als ``variant``; de kale bouwlijn hangt er geen klasse aan. In allebei de gevallen komt
er geen melding en ziet de knop er alleen maar anders uit.

De maten zijn wél een keuze, en die staat in ``features/knopmaten.md``:

* ``sm`` -- knoppen in een herhaalde, dichte context: een rij in een tabel, een kaart
  in een lijst, de acties bij één item.
* de standaard (``md``) -- alles daarbuiten: de hoofdactie van een pagina, de knoppen
  onder een formulier, de knoppen in een dialoog. Die schrijf je NIET op: ``md`` is de
  standaard van het component, en een maat die je twee kanten op kunt schrijven wordt
  vanzelf op twee manieren geschreven.

``xs`` staat er niet in. Die maat stond op zes knoppen die in niets van hun buren
verschilden behalve dat ze kleiner waren.

Bewaakt door ``tests/test_lotc_knopmaten.py``, dat de sjablonen zelf leest.
"""

from __future__ import annotations

#: Elke ``type``-waarde waar het component een klasse aan hangt.
BUTTON_VARIANTS = frozenset(
    {
        "primary",
        "secondary",
        "tertiary",
        "quaternary",
        "subtle",
        "warning",
        "warning-subtle",
    }
)

#: De maat voor een knop in een dichte, herhaalde context.
DENSE_BUTTON_SIZE = "sm"

#: Elke ``size``-waarde die een sjabloon mag opschrijven. Alles daarbuiten is ofwel de
#: standaard (en dan schrijf je hem niet op) ofwel een maat die het component niet kent.
BUTTON_SIZES = frozenset({DENSE_BUTTON_SIZE})


def check_button_variant(kind: str, owner: str) -> None:
    """Struikel over een knopvariant die het component niet kent.

    Voor de plekken waar de variant uit PYTHON komt en pas in het sjabloon een
    ``type``-attribuut wordt: daar kan de sjabloonbewaker niets lezen, dus valt zo'n
    waarde anders pas op als de knop kaal in beeld staat.
    """
    if kind not in BUTTON_VARIANTS:
        raise ValueError(
            f"{owner} kiest knopvariant '{kind}', en die bestaat niet. "
            f"Kies er een uit: {', '.join(sorted(BUTTON_VARIANTS))}"
        )
