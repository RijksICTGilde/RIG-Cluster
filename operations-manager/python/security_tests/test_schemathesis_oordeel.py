"""De beoordelingslogica van de schemathesis-proef, zonder sandbox.

``test_schemathesis.py`` zelf vraagt bij het laden altijd het OpenAPI-schema van een draaiende
sandbox op, dus zonder sandbox slaat pytest die hele module over (``allow_module_level=True``) en
blijft ``beoordeel_weigering`` ongetoetst in de standaardrun. Dit bestand beproeft die functie los,
via ``schemathesis_oordeel.py``.
"""

from __future__ import annotations

from security_tests.schemathesis_oordeel import GEWEIGERD, beoordeel_weigering


def test_een_geweigerde_status_geeft_geen_melding() -> None:
    assert beoordeel_weigering(401, "GET /x", "een naamloze aanroeper") is None


def test_een_doorgelaten_verzoek_geeft_een_melding() -> None:
    melding = beoordeel_weigering(200, "GET /x", "een naamloze aanroeper")
    assert melding == "GET /x laat een naamloze aanroeper door met 200"


def test_een_5xx_wordt_als_eigen_fout_herkend() -> None:
    melding = beoordeel_weigering(500, "GET /x", "een naamloze aanroeper")
    assert (
        melding
        == "GET /x geeft een naamloze aanroeper een 500; het verzoek raakte de applicatie in plaats van de grendel"
    )


def test_de_schemathesisproef_merkt_een_doorgelaten_verzoek_op() -> None:
    """Negatief voorbeeld: zo ziet een uitslag eruit die de proef rood hoort te maken."""
    assert all(beoordeel_weigering(status, "op", "wie") is None for status in GEWEIGERD)
    assert beoordeel_weigering(200, "op", "wie") is not None
    assert beoordeel_weigering(503, "op", "wie") is not None
