"""De schakelaar die bepaalt welke weergave een route rendert.

Zolang de omzetting per pagina loopt, kiest ``?ui=lotc`` de nieuwe weergave en blijft de
bestaande pagina zonder die vlag onveranderd. Deze test bewaakt dat de keuze precies op
die ene waarde valt: een schakelaar die ook op ``?ui=1`` of ``?ui=LOTC`` aanslaat, gaat
vroeg of laat per ongeluk aan.
"""

from types import SimpleNamespace


def _request(query: str) -> SimpleNamespace:
    """Het kleinste dat de schakelaar van een verzoek nodig heeft."""
    return SimpleNamespace(query_params={"ui": query} if query else {})


def test_wants_lotc_only_on_the_exact_value() -> None:
    from opi.web.lotc_switch import wants_lotc

    assert wants_lotc(_request("lotc"))
    assert not wants_lotc(_request(""))
    assert not wants_lotc(_request("iets-anders"))
