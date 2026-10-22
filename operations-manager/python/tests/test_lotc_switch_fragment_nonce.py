"""render_fragment zet de csp-nonce van het VERZOEK in de context.

``render()`` krijgt die via de context_processor van ``templates_lotc``
(``opi/core/templates_lotc.py``), maar een los ``env.get_template(...).render()`` loopt
daar niet langs. Zonder de regel in ``render_fragment`` die dit opvangt, zou elk
``nonce="{{ csp_nonce }}"`` in een fragmentsjabloon (bijvoorbeeld
``bg/_deployment-metrics.html.j2``) altijd een lege nonce renderen, nooit de echte van het
verzoek.
"""

from __future__ import annotations

from opi.core.templates_lotc import templates_lotc
from opi.web.lotc_switch import render_fragment
from starlette.requests import Request

SJABLOON_MET_NONCE = '<script nonce="{{ csp_nonce }}"></script>'


def _request_met_nonce(nonce: str) -> Request:
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    request.state.csp_nonce = nonce
    return request


def test_render_fragment_zet_de_nonce_van_het_verzoek_in_de_context(monkeypatch) -> None:
    sjabloon = templates_lotc.env.from_string(SJABLOON_MET_NONCE)
    monkeypatch.setattr(templates_lotc.env, "get_template", lambda _naam: sjabloon)

    html = render_fragment(_request_met_nonce("echte-nonce"), template="irrelevant.html.j2", context={})

    assert 'nonce="echte-nonce"' in html


def test_render_fragment_negeert_een_csp_nonce_die_al_in_de_context_stond(monkeypatch) -> None:
    """De nonce van het VERZOEK moet winnen; een context met een eigen (verkeerde) waarde
    mag die niet overschrijven, anders meet een fragment zijn eigen nonce in plaats van
    die van het document waarin het terechtkomt."""
    sjabloon = templates_lotc.env.from_string(SJABLOON_MET_NONCE)
    monkeypatch.setattr(templates_lotc.env, "get_template", lambda _naam: sjabloon)

    html = render_fragment(
        _request_met_nonce("echte-nonce"),
        template="irrelevant.html.j2",
        context={"csp_nonce": "oude-nonce"},
    )

    assert 'nonce="echte-nonce"' in html
    assert "oude-nonce" not in html
