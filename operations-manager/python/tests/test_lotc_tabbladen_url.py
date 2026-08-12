"""Elk tabblad van de projectpagina heeft een eigen PAD (RC-76).

``?tab=deployments`` is ``/projects/deployments/<naam>`` geworden. Een querystring leest
als een filter op een pagina ("laat hiervan alleen dit zien"), en dat is een tabblad niet:
het is een andere pagina over hetzelfde project.

Wat hier bewaakt wordt:

1. elk tabblad heeft een pad EN een route die dat pad ook echt bedient - een tab die naar
   een 404 wijst is erger dan geen tab;
2. Overzicht houdt ``/projects/details/<naam>``; daar wijst alles al heen (de
   projectenlijst, het dashboard, de uitnodigingsmail);
3. een onbekend tabblad valt terug op Overzicht in plaats van een pad te verzinnen.
"""

from __future__ import annotations

from opi.web.lotc_switch import PROJECT_TABS, STANDAARD_TAB, project_tab_url, tab_from_path
from opi.web.router import web_router


def _paden() -> set[str]:
    return {route.path for route in web_router.routes}


def test_elk_tabblad_heeft_een_route() -> None:
    """Een tab die naar een niet-bestaand pad wijst is een dode knop."""
    for tab in PROJECT_TABS:
        pad = project_tab_url("{project_name}", tab)
        assert pad in _paden(), f"tabblad {tab} wijst naar {pad}, en daar luistert geen route"


def test_overzicht_blijft_op_het_bestaande_adres() -> None:
    """Alles wijst daar al heen; dat verhuizen breekt links zonder iets op te leveren."""
    assert project_tab_url("demo", "project") == "/projects/details/demo"


def test_elk_tabblad_krijgt_zijn_eigen_pad() -> None:
    paden = {project_tab_url("demo", tab) for tab in PROJECT_TABS}

    assert len(paden) == len(PROJECT_TABS), "twee tabbladen op hetzelfde adres"
    assert "/projects/deployments/demo" in paden


def test_de_query_reist_mee() -> None:
    """Zoeken en sorteren staan in de URL; een tabbladlink mag ze kunnen dragen."""
    assert project_tab_url("demo", "project", "q=pr&dsort=cluster") == "/projects/details/demo?q=pr&dsort=cluster"


def test_een_onbekend_tabblad_valt_terug_op_overzicht() -> None:
    """Uit een oude of geknutselde link; dan hoort er een pagina te staan."""
    assert project_tab_url("demo", "bestaat-niet") == project_tab_url("demo", STANDAARD_TAB)


def test_het_pad_zegt_welk_tabblad_actief_is() -> None:
    assert tab_from_path("/projects/deployments/demo") == "deployments"
    assert tab_from_path("/projects/metrics/demo") == "metrics"
    assert tab_from_path("/projects/details/demo") == "project"


def test_een_vreemd_pad_valt_terug_op_overzicht() -> None:
    assert tab_from_path("/projects") == STANDAARD_TAB
    assert tab_from_path("/") == STANDAARD_TAB


def test_elk_tabbladadres_komt_bij_de_projectpagina_uit() -> None:
    """De reden dat de zes paden LETTERLIJK geregistreerd staan en niet als
    ``/projects/{tab}/{project_name}``: dat laatste zou ook ``/projects/<naam>/tasks``
    opvangen, en dan bepaalt de volgorde van registreren welke route wint.

    Deze toets loopt de routes af zoals Starlette dat doet - op volgorde, eerste treffer
    wint - en vraagt waar een tabbladadres uitkomt. Een andere route die ertussen komt te
    staan valt hier dus door de mand.
    """
    for tab in PROJECT_TABS:
        pad = project_tab_url("demo", tab)
        scope = {"type": "http", "method": "GET", "path": pad, "headers": [], "root_path": ""}
        gevonden = next(
            (route for route in web_router.routes if route.matches(scope)[0].value >= 2),
            None,
        )
        assert gevonden is not None, f"{pad} wordt door geen enkele route bediend"
        assert gevonden.endpoint.__name__ == "project_details", f"{pad} komt uit bij {gevonden.path}"
