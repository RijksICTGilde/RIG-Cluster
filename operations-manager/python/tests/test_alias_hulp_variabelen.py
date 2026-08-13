"""De uitleg bij het aliassenveld: welke variabelen mag je hier gebruiken.

Aanleiding: het hulptekstje zei "gebruik $VARIABELE_NAAM" zonder ooit een naam te noemen.
Die namen staan verspreid over de dienstdefinities, dus wie ze niet uit zijn hoofd wist
moest de code in, en dan vul je het veld niet in.

DE POORT DIE HIER OM HET MEEST TOE DOET is niet dat er een lijst is, maar dat het DEZELFDE
lijst is als waar de validatie op afgaat. Een uitleg die een naam noemt die het formulier
vervolgens weigert, is erger dan geen uitleg: dan volgt iemand de instructie en krijgt hij
een foutmelding. Vandaar dat elke genoemde naam hieronder door ``is_reference`` moet komen,
de functie die het formulier gebruikt om te oordelen.
"""

from __future__ import annotations

from typing import ClassVar

from opi.core.templates_lotc import templates_lotc
from opi.services.catalog.aliases.overzicht import alias_variabelen
from opi.services.catalog.aliases.references import is_reference
from opi.services.catalog.aliases.visualizers import COMPONENT_ALIASES

HULP = "aliassen.html.j2"


def test_elke_genoemde_naam_wordt_door_de_validatie_geaccepteerd() -> None:
    """De uitleg kan geen naam noemen die het formulier daarna afkeurt."""
    for dienst in alias_variabelen():
        for variabele in dienst.variabelen:
            assert is_reference(f"${variabele.naam}"), f"{dienst.label}: ${variabele.naam} wordt geweigerd"
            for andere in variabele.andere_namen:
                assert is_reference(f"${andere}"), f"{dienst.label}: ${andere} wordt geweigerd"


def test_de_diensten_die_variabelen_leveren_staan_erin() -> None:
    """Afgeleid uit de dienstdefinities, dus niet een tweede lijst die uit de pas loopt."""
    namen = {dienst.naam for dienst in alias_variabelen()}

    assert {"keycloak", "minio-storage", "namespace-postgresql-database"} <= namen
    assert all(dienst.variabelen for dienst in alias_variabelen()), "een dienst zonder variabelen hoort er niet in"


def test_een_variabele_staat_er_een_keer_in() -> None:
    """De alternatieve namen krijgen geen eigen regel: dezelfde waarde drie keer opsommen
    maakt de uitleg langer dan het probleem dat hij oplost."""
    for dienst in alias_variabelen():
        namen = [variabele.naam for variabele in dienst.variabelen]
        assert len(namen) == len(set(namen)), f"{dienst.label} noemt een variabele dubbel"


def test_het_veld_wijst_naar_de_uitleg() -> None:
    assert COMPONENT_ALIASES.help_template == HULP


def test_de_uitleg_rendert_en_noemt_de_variabelen() -> None:
    """Gerenderd en niet alleen gevonden: het sjabloon roept alias_variabelen() aan als
    GLOBAL, want de hulproute geeft een .html.j2 alleen het verzoek mee. Ontbreekt die
    global, dan rendert de pagina zonder een enkele variabele en zonder een foutmelding."""
    html = templates_lotc.env.get_template(f"help/{HULP}").render(request=None)

    assert "$DATABASE_SERVER_HOST" in html
    assert "$OIDC_CLIENT_ID" in html
    # Het kopieerveld per variabele, waar de vraag mee begon: per veld een kopieerknop.
    assert html.count("data-lotc-copy") >= 10


def test_het_veld_krijgt_een_hulpknop() -> None:
    """De knop staat op het VELD. Secties hadden er al een, velden niet, en juist dit veld
    heeft de uitleg nodig."""

    class _Veld:
        label = "Aliassen"
        path = "components/0/aliases"
        help_template = HULP
        help_text = None
        description = None
        errors: ClassVar[list[str]] = []
        value = ""
        readonly = False
        examples: ClassVar[list[str]] = []
        attributes: ClassVar[dict[str, str]] = {"kv_format": "env"}

    html = templates_lotc.env.get_template("widgets/key_value_editor.html.j2").render(field=_Veld())

    assert f"openServiceHelp('{HULP}')" in html
