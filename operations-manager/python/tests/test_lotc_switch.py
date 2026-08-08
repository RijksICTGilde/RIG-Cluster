"""De schakelaar die bepaalt welke weergave een route rendert.

Zolang de omzetting per pagina loopt, kiest ``?ui=lotc`` de nieuwe weergave en blijft de
bestaande pagina zonder die vlag onveranderd. Deze test bewaakt dat de keuze precies op
die ene waarde valt: een schakelaar die ook op ``?ui=1`` of ``?ui=LOTC`` aanslaat, gaat
vroeg of laat per ongeluk aan.
"""

from types import SimpleNamespace


def _request(query: str = "", cookie: str = "") -> SimpleNamespace:
    """Het kleinste dat de schakelaar van een verzoek nodig heeft."""
    return SimpleNamespace(
        query_params={"layout": query} if query else {},
        cookies={"zad_layout": cookie} if cookie else {},
    )


def test_the_new_layout_is_the_default() -> None:
    """Wie niets kiest, krijgt de nieuwe vormgeving. We zijn aan het overgaan."""
    from opi.web.lotc_switch import wants_lotc

    assert wants_lotc(_request())


def test_the_old_layout_stays_reachable() -> None:
    """Zolang niet elke pagina om is, moet je terug kunnen."""
    from opi.web.lotc_switch import wants_lotc

    assert not wants_lotc(_request(query="roos"))
    assert not wants_lotc(_request(cookie="roos"))


def test_the_query_wins_from_the_cookie() -> None:
    """Met een link laten zien wat je bedoelt, zonder andermans voorkeur te overschrijven."""
    from opi.web.lotc_switch import wants_lotc

    assert wants_lotc(_request(query="nldd", cookie="roos"))
    assert not wants_lotc(_request(query="roos", cookie="nldd"))


def test_an_unknown_value_falls_back_to_the_default() -> None:
    """Een verminkte of verouderde waarde mag geen lege pagina opleveren."""
    from opi.web.lotc_switch import wants_lotc

    assert wants_lotc(_request(query="onzin"))
    assert wants_lotc(_request(cookie="onzin"))
