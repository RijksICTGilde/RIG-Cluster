"""Elk tabblad van de projectpagina heeft een eigen PAD (RC-76).

``?tab=deployments`` is ``/projects/<naam>/deployments`` geworden. Een querystring leest
als een filter op een pagina ("laat hiervan alleen dit zien"), en dat is een tabblad niet:
het is een andere pagina over hetzelfde project.

Wat hier bewaakt wordt:

1. elk tabblad heeft een pad EN een route die dat pad ook echt bedient - een tab die naar
   een 404 wijst is erger dan geen tab;
2. de PROJECTNAAM staat voorop en het tabblad erachter (RC-93), en de oude vorm met het
   tabblad voorop verwijst door zodat gedeelde links blijven werken;
3. een onbekend tabblad valt terug op Overzicht in plaats van een pad te verzinnen.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opi.web.lotc_switch import (
    PROJECT_TABS,
    STANDAARD_TAB,
    TABS_MET_DEPLOYMENT,
    project_tab_url,
    tab_from_path,
)
from opi.web.router import web_router


def _paden() -> set[str]:
    return {route.path for route in web_router.routes}


def test_elk_tabblad_heeft_een_route() -> None:
    """Een tab die naar een niet-bestaand pad wijst is een dode knop."""
    for tab in PROJECT_TABS:
        pad = project_tab_url("{project_name}", tab)
        assert pad in _paden(), f"tabblad {tab} wijst naar {pad}, en daar luistert geen route"


def test_de_projectnaam_staat_voorop() -> None:
    """Het project is waar je bent, het tabblad is wat je erbinnen bekijkt (RC-93)."""
    assert project_tab_url("demo", "project") == "/projects/demo/details"
    assert project_tab_url("demo", "deployments") == "/projects/demo/deployments"


def test_elk_tabblad_krijgt_zijn_eigen_pad() -> None:
    paden = {project_tab_url("demo", tab) for tab in PROJECT_TABS}

    assert len(paden) == len(PROJECT_TABS), "twee tabbladen op hetzelfde adres"
    assert "/projects/demo/deployments" in paden


def test_de_query_reist_mee() -> None:
    """Zoeken en sorteren staan in de URL; een tabbladlink mag ze kunnen dragen."""
    assert project_tab_url("demo", "project", "q=pr&dsort=cluster") == "/projects/demo/details?q=pr&dsort=cluster"


def test_een_onbekend_tabblad_valt_terug_op_overzicht() -> None:
    """Uit een oude of geknutselde link; dan hoort er een pagina te staan."""
    assert project_tab_url("demo", "bestaat-niet") == project_tab_url("demo", STANDAARD_TAB)


def test_het_pad_zegt_welk_tabblad_actief_is() -> None:
    assert tab_from_path("/projects/demo/deployments") == "deployments"
    assert tab_from_path("/projects/demo/metrics") == "metrics"
    assert tab_from_path("/projects/demo/details") == "project"


def test_een_vreemd_pad_valt_terug_op_overzicht() -> None:
    assert tab_from_path("/projects") == STANDAARD_TAB
    assert tab_from_path("/projects/demo") == STANDAARD_TAB
    assert tab_from_path("/") == STANDAARD_TAB


def test_elk_tabbladadres_komt_bij_de_projectpagina_uit() -> None:
    """De reden dat de zes paden LETTERLIJK geregistreerd staan en niet als
    ``/projects/{project_name}/{tab}``: dat laatste zou ook ``/projects/details/<naam>``
    opvangen (met ``project_name="details"``), en dan bepaalt de volgorde van registreren
    welke route wint.

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


# --------------------------------------------------- de deployment in het pad (RC-92)


def test_de_tabbladen_met_een_deployment_dragen_hem_in_hun_pad() -> None:
    """Zo blijft de keuze staan bij het wisselen van tabblad: de tabbalk geeft de naam mee
    en er hoeft niets onthouden te worden."""
    assert project_tab_url("demo", "deployments", deployment="productie") == "/projects/demo/deployments/productie"
    assert project_tab_url("demo", "metrics", deployment="productie") == "/projects/demo/metrics/productie"


def test_de_andere_tabbladen_krijgen_de_deployment_niet() -> None:
    """Overzicht toont ze allemaal in een tabel; ``/projects/demo/details/productie`` heeft
    geen route, dus de tabbalk zou naar een 404 wijzen."""
    for tab in PROJECT_TABS:
        if tab in TABS_MET_DEPLOYMENT:
            continue
        assert project_tab_url("demo", tab, deployment="productie") == project_tab_url("demo", tab)


def test_een_deploymentnaam_wordt_veilig_in_het_pad_gezet() -> None:
    assert project_tab_url("demo", "deployments", deployment="a/b") == "/projects/demo/deployments/a%2Fb"


def test_de_query_reist_mee_naast_de_deployment() -> None:
    assert (
        project_tab_url("demo", "deployments", "q=pr", deployment="productie")
        == "/projects/demo/deployments/productie?q=pr"
    )


def test_elk_deploymentadres_heeft_een_route() -> None:
    """Een kiezer die naar een niet-bestaand pad wijst is een dode knop."""
    for tab in TABS_MET_DEPLOYMENT:
        # De naam wordt in het pad ge-escaped (dat is de bedoeling: een deploymentnaam is
        # gebruikersinvoer), dus het routepad wordt hier opgebouwd en niet gequote.
        pad = project_tab_url("{project_name}", tab) + "/{deployment_name}"
        assert pad in _paden(), f"tabblad {tab} wijst naar {pad}, en daar luistert geen route"


def test_een_deploymentadres_komt_bij_de_projectpagina_uit() -> None:
    """Zelfde reden als hierboven: de paden staan letterlijk geregistreerd, dus een route
    die ertussen komt te staan valt hier door de mand."""
    for tab in TABS_MET_DEPLOYMENT:
        pad = project_tab_url("demo", tab, deployment="productie")
        scope = {"type": "http", "method": "GET", "path": pad, "headers": [], "root_path": ""}
        gevonden = next(
            (route for route in web_router.routes if route.matches(scope)[0].value >= 2),
            None,
        )
        assert gevonden is not None, f"{pad} wordt door geen enkele route bediend"
        assert gevonden.endpoint.__name__ == "project_deployment_details", f"{pad} komt uit bij {gevonden.path}"


def test_het_pad_met_een_deployment_wijst_nog_steeds_zijn_tabblad_aan() -> None:
    assert tab_from_path("/projects/demo/deployments/productie") == "deployments"
    assert tab_from_path("/projects/demo/metrics/productie") == "metrics"


# ------------------------------------- de projectnaam voorop, de oude vorm blijft (RC-93)

#: De tabbladadressen van voor RC-93, met het tabblad VOOR de projectnaam. Ze zijn een dag
#: in de sandbox in gebruik geweest en kunnen gedeeld zijn.
OUDE_ADRESSEN = {
    "/projects/details/demo": "/projects/demo/details",
    "/projects/team/demo": "/projects/demo/team",
    "/projects/toegang/demo": "/projects/demo/toegang",
    "/projects/componenten/demo": "/projects/demo/componenten",
    "/projects/services/demo": "/projects/demo/services",
    "/projects/deployments/demo": "/projects/demo/deployments",
    "/projects/metrics/demo": "/projects/demo/metrics",
    "/projects/taken/demo": "/projects/demo/taken",
    "/projects/deployments/demo/productie": "/projects/demo/deployments/productie",
    "/projects/metrics/demo/productie": "/projects/demo/metrics/productie",
}


@pytest.fixture
def client() -> TestClient:
    """De webroutes zonder de rest van de applicatie.

    De doorverwijzing kijkt alleen naar het pad - geen project, geen gebruiker - dus dit is
    genoeg om hem te meten, en de test hoeft niets te mocken.
    """
    app = FastAPI()
    app.include_router(web_router)
    return TestClient(app)


@pytest.mark.parametrize(("oud", "nieuw"), sorted(OUDE_ADRESSEN.items()))
def test_het_oude_adres_verwijst_door_naar_het_nieuwe(client: TestClient, oud: str, nieuw: str) -> None:
    """Geen enkel bestaand pad wordt stil een 404: ze zijn gedeeld en horen te blijven werken."""
    antwoord = client.get(oud, follow_redirects=False)

    assert antwoord.status_code == 302, f"{oud} verwijst niet door"
    assert antwoord.headers["location"] == nieuw


def test_elke_geregistreerde_oude_vorm_verwijst_ook_echt_door(client: TestClient) -> None:
    """De routes en de vertaaltabel worden met de HAND naast elkaar gehouden, en dat liep
    uit de pas: ``/projects/team/<naam>`` stond wel als route geregistreerd maar niet in
    OUDE_TABBLADPADEN, dus die zoekopdracht wierp een KeyError en het adres gaf een 500 in
    plaats van een doorverwijzing (gevonden en gerepareerd in RC-101).

    Daarom worden de routes hier ZELF afgelopen: elke oude vorm die geregistreerd staat
    moet ook doorverwijzen, hoe de tabel er ook uitziet.
    """
    oude_vormen = sorted(
        route.path
        for route in web_router.routes
        if getattr(route, "endpoint", None) is not None
        and route.endpoint.__name__ == "project_tab_oude_vorm"
        and "{deployment_name}" not in route.path
    )

    assert oude_vormen, "geen enkele oude vorm gevonden; deze meting kijkt naar de verkeerde routes"
    for pad in oude_vormen:
        antwoord = client.get(pad.replace("{project_name}", "demo"), follow_redirects=False)
        assert antwoord.status_code == 302, f"{pad} verwijst niet door (status {antwoord.status_code})"


def test_de_zoekopdracht_reist_mee_in_de_doorverwijzing(client: TestClient) -> None:
    """Een gedeelde link draagt zijn filters; die onderweg laten vallen is stil verlies."""
    antwoord = client.get("/projects/deployments/demo?q=pr&dsort=cluster", follow_redirects=False)

    assert antwoord.headers["location"] == "/projects/demo/deployments?q=pr&dsort=cluster"


def test_het_oude_adres_leest_de_projectnaam_op_de_juiste_plek(client: TestClient) -> None:
    """``/projects/details/<naam>`` mag NIET als project "details" gelezen worden.

    Dat is precies wat er gebeurt als een van de twee vormen als wildcard geregistreerd
    staat (``/projects/{project_name}/{tab}`` vangt ook het oude pad op). De naam hoort in
    het derde segment te blijven.
    """
    antwoord = client.get("/projects/details/tfc-nfv", follow_redirects=False)

    assert antwoord.headers["location"] == "/projects/tfc-nfv/details"


@pytest.mark.parametrize("tabpad", sorted(gegevens["path"] for gegevens in PROJECT_TABS.values()))
def test_een_project_dat_naar_een_tabblad_is_vernoemd_komt_op_zijn_eigen_pagina_uit(tabpad: str) -> None:
    """Een project dat ``details`` of ``deployments`` heet leest in beide vormen. Het adres
    van vandaag wint, dus zijn tabbladen werken; de oude vorm is de transitie."""
    pad = project_tab_url(tabpad, "deployments")
    scope = {"type": "http", "method": "GET", "path": pad, "headers": [], "root_path": ""}
    gevonden = next((route for route in web_router.routes if route.matches(scope)[0].value >= 2), None)

    assert gevonden is not None, f"{pad} wordt door geen enkele route bediend"
    assert gevonden.endpoint.__name__ == "project_details", f"{pad} komt uit bij {gevonden.path}"
    assert gevonden.matches(scope)[1]["path_params"]["project_name"] == tabpad
