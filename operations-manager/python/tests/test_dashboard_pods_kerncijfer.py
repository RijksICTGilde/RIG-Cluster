"""De kaart Pods op het dashboard stond altijd op 0.

Gevonden in de sandboxdoorloop van RC-110 door naar het scherm te kijken: drie projecten
met elk een draaiende pod, en de kaart meldde 0. Prometheus wist het wel
(``count(kube_pod_info)`` gaf 66), dus het lag niet aan de meting.

De oorzaak is de opsplitsing van het dashboard: het resourcegebruik wordt sinds
``2b70b13b`` APART opgehaald, want die Prometheus-queries duurden te lang om de pagina op
te laten wachten. Het aantal pods komt uit diezelfde queries, maar de kaart bleef in de
PAGINA staan - die rendert dus met de vaste ``pod_count = 0`` van de route, en het
fragment dat het echte getal binnenhaalde gooide het weg (``_pods``).

Dit toetst de drie schakels waar het op stukging: de route geeft het getal aan het
fragment, het fragment schuift de kaart er out-of-band overheen, en de pagina heeft het
doel waar die swap op landt.
"""

from __future__ import annotations

from pathlib import Path

SJABLONEN = Path(__file__).resolve().parent.parent / "opi" / "templates_lotc"


def _fragment() -> str:
    return (SJABLONEN / "bg" / "_dashboard-usage.html.j2").read_text()


def _pagina() -> str:
    return (SJABLONEN / "bg" / "dashboard.html.j2").read_text()


def test_de_pagina_heeft_een_doel_voor_de_swap() -> None:
    """Zonder id landt de out-of-band swap nergens en blijft de 0 staan."""
    assert 'id="kerncijfer-pods"' in _pagina()


def test_het_fragment_schuift_de_kaart_er_out_of_band_overheen() -> None:
    fragment = _fragment()
    assert 'id="kerncijfer-pods"' in fragment
    assert 'hx-swap-oob="true"' in fragment
    assert 'label="Pods"' in fragment


def test_de_swap_staat_buiten_de_prometheus_voorwaarde() -> None:
    """Ook zonder Prometheus moet de kaart een getal krijgen in plaats van te blijven hangen.

    Het fragment eindigt op een ``{% else %}``-tak voor "Resourcegebruik niet
    beschikbaar"; staat de swap daarbinnen, dan komt hij er in precies dat geval niet uit.
    """
    fragment = _fragment()
    laatste_endif = fragment.rindex("{% endif %}")
    assert fragment.index('id="kerncijfer-pods"') > laatste_endif


def test_de_route_geeft_het_getal_mee_aan_het_fragment() -> None:
    """Het fragment gooide het weg als ``_pods``; dan is het sjabloon vergeefs."""
    router = (Path(__file__).resolve().parent.parent / "opi" / "web" / "router.py").read_text()
    fragment_deel = router[router.index("async def dashboard_resource_usage_fragment") :]
    fragment_deel = fragment_deel[: fragment_deel.index("async def project_resource_usage_fragment")]

    assert "_pods" not in fragment_deel
    assert '"pod_count": pod_count' in fragment_deel
