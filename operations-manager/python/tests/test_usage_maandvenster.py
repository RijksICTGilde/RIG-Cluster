"""Het venster waarover de kostenpagina een maand meet.

DE BUG DIE HIER VAST KOMT TE LIGGEN. De query kijkt vanaf het EVALUATIEMOMENT terug over
een venster. Voor een afgesloten maand wordt op de laatste dag van die maand geevalueerd,
en dan levert een venster van een volle maand precies die maand op. Voor de LOPENDE maand
wordt op "nu" geevalueerd, en daar stond hetzelfde venster van een volle maand: op 28
augustus 2026 reikte dat terug tot 28 juli, dus stonden er vier dagen juli in de
augustusrij en ontbraken de laatste drie dagen van augustus.

Het venster loopt nu tot de eerste van de maand, terwijl de NOEMER de hele maand blijft.
Die twee moesten uit elkaar, en dat is precies wat hier getoetst wordt: een lopende maand
toont wat er tot nu toe is opgebouwd, in dezelfde grootheid als een afgesloten maand, en
niet een gemiddelde over een venster dat toevallig in de vorige maand hangt.
"""

from __future__ import annotations

from datetime import UTC, datetime

from freezegun import freeze_time
from opi.web.router_usage import (
    MEMORY_USAGE_QUERY,
    RECORDED_USAGE_QUERY,
    _get_month_end,
    _is_lopende_maand,
    _venster_en_noemer,
)

#: Halverwege de middag op 28 augustus 2026, het moment waarop de fout gemeten is.
NU = "2026-08-28 13:45:00"


class TestAfgeslotenMaand:
    @freeze_time(NU)
    def test_het_venster_is_de_hele_maand(self) -> None:
        assert _venster_en_noemer(2026, 7, 31) == ("744h", 744)

    @freeze_time(NU)
    def test_er_wordt_op_het_einde_van_de_maand_geevalueerd(self) -> None:
        assert _get_month_end(2026, 7, 31) == datetime(2026, 7, 31, 23, 59, 59, tzinfo=UTC)

    @freeze_time(NU)
    def test_een_kortere_maand_krijgt_een_kortere_noemer(self) -> None:
        """Februari telt 28 dagen; een vaste noemer van 31 dagen zou hem te laag zetten."""
        assert _venster_en_noemer(2026, 2, 28) == ("672h", 672)


class TestLopendeMaand:
    @freeze_time(NU)
    def test_het_venster_reikt_tot_de_eerste_en_niet_verder(self) -> None:
        """Van 1 augustus 00:00 tot 28 augustus 13:45 is 661,75 uur, naar boven 662.

        Een venster van 744h (de oude berekening) zou vanaf hetzelfde moment tot 28 JULI
        teruglopen, en dat is de fout die deze test vastlegt.
        """
        venster, noemer = _venster_en_noemer(2026, 8, 31)

        assert venster == "662h"
        assert noemer == 744, "de noemer blijft de hele maand, anders is het geen maandhoeveelheid"

    @freeze_time("2026-08-01 00:30:00")
    def test_vlak_na_middernacht_op_de_eerste_is_het_venster_geldig(self) -> None:
        """Een venster van 0h is geen geldige PromQL-duur en zou een lege pagina opleveren."""
        venster, _ = _venster_en_noemer(2026, 8, 31)

        assert venster == "1h"

    @freeze_time(NU)
    def test_de_lopende_maand_wordt_als_lopend_herkend(self) -> None:
        assert _is_lopende_maand(2026, 8) is True
        assert _is_lopende_maand(2026, 7) is False
        assert _is_lopende_maand(2025, 8) is False


class TestDeTweeQueries:
    """Beide wegen naar hetzelfde getal moeten hetzelfde venster en dezelfde noemer krijgen.

    De recording rule is de goedkope weg, de ruwe query de fallback voor maanden van voor
    juni 2026. Lopen die uiteen, dan verspringt een getal zodra de fallback aanslaat, en
    dat is precies het soort verschil dat niemand opmerkt.
    """

    def test_beide_queries_vragen_om_venster_en_noemer(self) -> None:
        for query in (RECORDED_USAGE_QUERY, MEMORY_USAGE_QUERY):
            assert "{venster}" in query
            assert "{noemer_uren}" in query
            # De oude, vaste dagparameter mag nergens meer staan: die was de bug.
            assert "{days}" not in query

    def test_beide_queries_zijn_in_te_vullen(self) -> None:
        for query in (RECORDED_USAGE_QUERY, MEMORY_USAGE_QUERY):
            ingevuld = query.format(namespace_filter="rig-prd-.*", venster="668h", noemer_uren=744)

            assert "[668h" in ingevuld
            assert "/ 744 /" in ingevuld
