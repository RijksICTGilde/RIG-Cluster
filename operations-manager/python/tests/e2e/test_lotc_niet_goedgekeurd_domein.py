"""Wat een BROWSER toont als het aangevraagde domein er nog niet is.

De melding over ``cmt2-om5``: de portal zette ``https://frontend.rig-test.mijn-webshop-test.nl/``
neer als HET adres van de applicatie, terwijl dat domein nooit is goedgekeurd en er dus
niets op dat adres luistert. Geen woord erbij over waarom.

Er staan twee dingen op het spel en ze zijn allebei alleen in de gerenderde pagina te zien:

1. de knop moet naar het adres wijzen dat ECHT bediend wordt (het clusteradres), en de
   ``href``/``data-url`` van een knop is wat de klik doet -- niet wat de context zegt;
2. er moet naast die knop staan dat het gevraagde adres nog niet beschikbaar is, met de
   juiste van de drie statussen. Op het tabblad Componenten stond die melding helemaal
   niet, en dat blijkt uit niets anders dan de pagina zelf.

Het project wordt in de draaiende testserver gezet en daarna weer weggehaald, net als in
``tests/e2e/test_lotc_aanvragenbeheer.py``: de projectenlijst wordt elders in zijn geheel
getoetst en een blijvertje maakt die test stuk.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

PROJECT = "domein-adres-e2e"
EIGEN_DOMEIN = "mijn-webshop-test.nl"
SUBDOMEIN = "rig-test"
DEPLOYMENT = "rig-test"

#: Het adres dat het projectbestand VRAAGT. Dit mag nergens als link opduiken zolang het
#: domein niet is goedgekeurd.
GEVRAAGD_ADRES = f"frontend.{SUBDOMEIN}.{EIGEN_DOMEIN}"

#: Het adres dat ECHT bediend wordt: het veilige formaat op het clusterdomein van 'local'.
CLUSTERADRES = f"https://frontend-{DEPLOYMENT}-{PROJECT}.kind"

#: De vorm uit de melding: base-domain en subdomain, met de oude domain-mode en ZONDER
#: domain-format. Precies die vorm liep om de goedkeuringspoort heen.
WEBCONFIG: dict[str, Any] = {
    "base-domain": EIGEN_DOMEIN,
    "subdomain": SUBDOMEIN,
    "domain-mode": "nice-url",
}

#: Het sleutelpaar uit ``tests/e2e/fixtures/projects/test-project-detail.yaml``. De
#: detailpagina ontsleutelt ``config`` voordat ze iets rendert, dus een project zonder deze
#: twee sleutels geeft een 500 en meet niets.
AGE_PUBLIC_KEY = "age1drxwupvn5eg8wd9cdf05nrxp6usrpk7tarc09yzk4c3m7jzzaups8757zy"
AGE_PRIVATE_KEY = """-----BEGIN AGE ENCRYPTED FILE-----
YWdlLWVuY3J5cHRpb24ub3JnL3YxCi0+IFgyNTUxOSB0ZkhrWGhCVEdIT1B5SFEz
SVRJSG5zK1RHWFFBTmUvL040RU9LakdqeDE4Cld6c3MwWlJPWDgwYmlETjdEYnFC
SUN3U3NMdEFYSEhZZG5veHd3U09Dd2MKLS0tIDg3ZDlMNGZNdWo4WEVRcWRpbE4w
Z0x5bi9TT0hOazloWFFUOTJNL1BNYkUKHphHJ9YjoFAsm7M2ylEIRskosRJ4yjDz
FHvB2sekCLIoHuLGt0jLowrALzEcAsE0b+rkc1tt7YVswg+t0HvJx0kDdmTSL0X4
cXwv+GpqbnY5WLURIHtH8Fq3FubUc91kw532d8NnvC6KwA==
-----END AGE ENCRYPTED FILE-----
"""

PROJECT_DATA: dict[str, Any] = {
    "name": PROJECT,
    "config": {
        "api-key": "domein-adres-e2e-key",
        "age-public-key": AGE_PUBLIC_KEY,
        "age-private-key": AGE_PRIVATE_KEY,
    },
    "clusters": ["local"],
    "components": [
        {"name": "frontend", "type": "deployment", "services": ["publish-on-web"], "ports": {"inbound": [8080]}}
    ],
    "deployments": [
        {
            "name": DEPLOYMENT,
            "cluster": "local",
            "namespace": PROJECT,
            "components": [{"reference": "frontend", "image": "nginx:latest"}],
            "services": [{"reference": "publish-on-web", "config": WEBCONFIG}],
        }
    ],
    "services": [
        {
            "reference": "publish-on-web",
            "config": {
                "domains": {
                    "allowed-domains": [
                        {
                            "domain": EIGEN_DOMEIN,
                            "status": "requested",
                            "history": [{"date": "2026-08-01T00:00:00+00:00", "status": "requested"}],
                        }
                    ]
                }
            },
        }
    ],
}


def _met_status(status: str) -> dict[str, Any]:
    """Hetzelfde project, met een andere stand op de domeinaanvraag."""
    data = copy.deepcopy(PROJECT_DATA)
    domein = data["services"][0]["config"]["domains"]["allowed-domains"][0]
    domein["status"] = status
    domein["history"] = [{"date": "2026-08-01T00:00:00+00:00", "status": status, "by": "beheerder@example.nl"}]
    return data


@pytest.fixture
def project(app_server: str, request: pytest.FixtureRequest) -> Iterator[str]:
    """Zet het project in de draaiende testserver, en haal het daarna weg."""
    from opi.services.project_service import get_project_service

    data = getattr(request, "param", None) or PROJECT_DATA
    dienst = get_project_service()
    dienst.register(PROJECT, "domein-adres-e2e-key", f"{PROJECT}.yaml", [], data)
    try:
        yield PROJECT
    finally:
        dienst.remove_project(PROJECT)


def _links(page: Page) -> list[str]:
    """Elk adres waar een knop op deze pagina naartoe gaat.

    Een publieke link is soms een ``href`` (het tabblad Deployments) en soms een
    ``data-url`` die de onclick opent (het tabblad Componenten). Beide tellen, want beide
    zijn wat een klik doet.
    """
    return page.evaluate(
        """() => [...document.querySelectorAll('[href], [data-url]')]
                   .map(e => e.getAttribute('data-url') || e.getAttribute('href'))
                   .filter(Boolean)"""
    )


def _open(page: Page, app_server: str, pad: str) -> None:
    page.goto(f"{app_server}{pad}")
    page.wait_for_load_state("networkidle")


def _meldingen(page: Page) -> list[tuple[str, str]]:
    """Elke goedkeuringsmelding op de pagina, als (soort, tekst).

    De tekst staat als ATTRIBUUT op ``<nldd-banner>`` en het component zet hem in zijn
    schaduw-DOM; ``innerText`` van de pagina bevat hem dus niet, terwijl een gebruiker hem
    gewoon leest. Daarom wordt het attribuut gelezen -- dat is wat het component toont --
    en toetst de test hieronder met ``get_by_text`` dat het ook echt op het scherm staat.
    """
    return page.evaluate(
        """() => [...document.querySelectorAll('nldd-banner')]
                   .map(e => [e.getAttribute('variant') || '', e.getAttribute('text') || ''])"""
    )


# ---------------------------------------------------------------------------
# De link wijst naar het adres dat bediend wordt
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pad", [f"/projects/{PROJECT}/componenten", f"/projects/{PROJECT}/deployments"])
def test_de_knop_gaat_naar_het_clusteradres_en_niet_naar_het_gevraagde_domein(
    app_server: str, auth_page: Page, project: str, pad: str
) -> None:
    """De gemelde fout, gemeten waar hij gemeld werd: in de browser."""
    _open(auth_page, app_server, pad)

    adressen = _links(auth_page)
    assert CLUSTERADRES in adressen, f"het clusteradres staat er niet: {adressen}"
    assert not [a for a in adressen if GEVRAAGD_ADRES in a], (
        f"het niet-goedgekeurde domein staat als link op de pagina: {adressen}"
    )


# ---------------------------------------------------------------------------
# En er staat bij waarom
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pad", [f"/projects/{PROJECT}/componenten", f"/projects/{PROJECT}/deployments"])
def test_naast_de_link_staat_dat_de_aanvraag_nog_loopt(
    app_server: str, auth_page: Page, project: str, pad: str
) -> None:
    """Een adres dat niet is wat je vroeg, zonder uitleg, is precies de klacht."""
    _open(auth_page, app_server, pad)

    meldingen = _meldingen(auth_page)
    assert meldingen, f"geen enkele melding naast de link op {pad}"
    soort, tekst = meldingen[0]
    assert soort == "warning"
    assert EIGEN_DOMEIN in tekst, "het gevraagde domein wordt niet genoemd"
    assert "wacht op goedkeuring" in tekst, f"de lopende aanvraag wordt niet gemeld op {pad}"
    assert "clusteradres" in tekst, "het gevolg (publiceren op het clusteradres) staat er niet bij"

    # En het staat ook echt op het scherm: get_by_text kijkt door de schaduw-DOM heen.
    auth_page.get_by_text("wacht op goedkeuring").first.wait_for(state="visible", timeout=10000)


@pytest.mark.parametrize(
    ("project", "verwacht", "soort"),
    [
        (_met_status("requested"), "wacht op goedkeuring", "warning"),
        # ``c-alert type="error"`` komt er als ``variant="critical"`` uit; de test noemt de
        # waarde die de BROWSER krijgt, want dat is wat de kleur bepaalt.
        (_met_status("denied"), "is afgewezen", "critical"),
    ],
    indirect=["project"],
)
def test_een_afgewezen_aanvraag_leest_anders_dan_een_lopende(
    app_server: str, auth_page: Page, project: str, verwacht: str, soort: str
) -> None:
    """Op afgewezen ga je niet zitten wachten, dus dat mag niet dezelfde melding zijn.

    De SOORT melding telt mee: rood tegenover geel is het verschil tussen "dit komt niet
    meer goed" en "dit loopt nog".
    """
    _open(auth_page, app_server, f"/projects/{PROJECT}/componenten")

    gevonden = _meldingen(auth_page)
    assert [s for s, _ in gevonden] == [soort], f"verkeerde soort melding: {gevonden}"
    assert verwacht in gevonden[0][1], f"de melding leest niet als {verwacht!r}: {gevonden}"


@pytest.mark.parametrize("project", [_met_status("approved")], indirect=True)
def test_een_goedgekeurd_domein_toont_gewoon_het_eigen_adres_zonder_melding(
    app_server: str, auth_page: Page, project: str
) -> None:
    """De negatieve kant: de poort mag geen adressen weghalen die wel mogen."""
    _open(auth_page, app_server, f"/projects/{PROJECT}/componenten")

    adressen = _links(auth_page)
    assert [a for a in adressen if GEVRAAGD_ADRES in a], f"het eigen domein ontbreekt: {adressen}"
    assert CLUSTERADRES not in adressen
    assert _meldingen(auth_page) == []
