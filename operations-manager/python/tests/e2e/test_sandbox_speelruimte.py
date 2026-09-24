"""De speelruimte van een dienst, gemeten op een draaiend cluster via de CLI.

`features/speelruimte-van-een-dienst.md` zegt dat een dienst per veld declareert hoe ver
een project mag gaan, en dat de grens op precies een plek staat. In de unittests klopt
dat. Wat daar niet in zit is de weg waarlangs een gebruiker de waarde er echt in krijgt:
daar komt hij uit een formulier of uit de CLI, gaat hij als JSON over de API, wordt hij
in het projectbestand geschreven en pas daarna gelezen. Precies het soort grendel dat in
een fixture sluit en langs de rand van een echte aanroep glipt.

Wat hier gemeten wordt is de PROJECTLAAG en de DEPLOYMENTLAAG van `connection-limit` op
`postgresql-database` (`opi/services/catalog/postgresql_database/connection_limit.py`:
minimum 1, maximum 500, standaard 20). Dat is vandaag de enige gedeclareerde instelling
op het hele platform.

Twee dingen die het plan van RC-227 vroeg staan er daarom NIET in, met de meting erbij:

- Een `grow_only`-veld dat omlaag wil. `grow_only` bestaat in het mechanisme
  (`opi/services/catalog/config_settings.py`), maar geen enkele dienst declareert een
  veld met die vlag: `grep -rn grow_only opi/` buiten `config_settings.py` geeft niets.
  Er is dus geen veld om op te richten, en een clustertoets kan die helft niet dekken
  zolang dat zo blijft. De unittests dekken hem wel.
- Een waarde op een laag die de dienst niet openzet, ingestuurd door de CLI. De CLI
  weigert die laag zelf op grond van de catalogus, dus wat er dan gemeten wordt is de
  CLI en niet de server. Daarom staat de servertoets daarvoor hieronder als een RECHTE
  API-aanroep ernaast: de vraag is of de SERVER hem weigert, ook als er iets langskomt
  dat de catalogus niet raadpleegt.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

import httpx
import pytest
from tests.e2e.helpers import sandbox_api
from tests.e2e.helpers.lifecycle import create_project_with_services
from tests.e2e.helpers.wizard import unique_project_name
from tests.e2e.helpers.zad_cli import ZadCli, skip_zonder_cli

if TYPE_CHECKING:
    from collections.abc import Generator

    from playwright.sync_api import BrowserContext
    from tests.e2e.helpers.forgejo import ForgejoClient
    from tests.e2e.helpers.lifecycle import CreatedProject

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.e2e, pytest.mark.sandbox]

_API_VERIFY_SSL = os.environ.get("E2E_API_VERIFY_SSL", "false").lower() in ("1", "true", "yes")
_USER_EMAIL = os.environ.get("E2E_SANDBOX_USER", "admin@sandbox.rijksapp.dev")

_DIENST = "postgresql-database"
_VELD = "connection-limit"
#: Uit de declaratie. Staat hier als losse getallen zodat een toets die op de GRENS meet
#: niet stilletjes meebeweegt met een wijziging van die grens: verandert de declaratie,
#: dan hoort deze toets om te vallen en met de hand herzien te worden.
_MINIMUM = 1
_MAXIMUM = 500
_STANDAARD = 20
#: De componentnaam die de wizardhelper aanmaakt; hier alleen nodig om een laag aan te
#: wijzen die de dienst NIET openzet.
_COMPONENT = "web"


@pytest.fixture(scope="module")
def db_project(
    sandbox_context: BrowserContext,
    sandbox_url: str,
    forgejo: ForgejoClient,
) -> Generator[CreatedProject]:
    """Een project met postgresql-database, want zonder die dienst is er niets te begrenzen."""
    page = sandbox_context.new_page()
    try:
        project = create_project_with_services(
            page,
            sandbox_url,
            forgejo,
            unique_project_name("speelrui"),
            user_email=_USER_EMAIL,
            services=["publish-on-web", _DIENST],
        )
    finally:
        page.close()

    try:
        yield project
    finally:
        sandbox_api.delete_project_via_api(
            sandbox_url,
            project.name,
            project.api_key,
            verify_ssl=_API_VERIFY_SSL,
        )


@pytest.fixture(scope="module")
def cli(sandbox_url: str, db_project: CreatedProject) -> ZadCli:
    return ZadCli(skip_zonder_cli(), sandbox_url, api_key=db_project.api_key, project=db_project.name)


def _zet(cli: ZadCli, waarde: object, *, laag: str = "project", deployment: str = ""):
    args = ["service", "config", "set", _DIENST, "--target", laag, "--set", f"{_VELD}={waarde}", "--yes"]
    if deployment:
        args += ["--deployment", deployment]
    return cli.run(*args)


def _limiet_in_projectbestand(forgejo: ForgejoClient, project: str) -> object:
    yaml = forgejo.get_project_yaml(project) or {}
    for entry in yaml.get("services") or []:
        naam = entry if isinstance(entry, str) else (entry.get("name") or next(iter(entry), ""))
        if naam != _DIENST:
            continue
        if isinstance(entry, str):
            return None
        config = entry.get("config") or (entry.get(_DIENST) or {}).get("config") or {}
        return config.get(_VELD)
    return None


def test_een_waarde_binnen_de_speelruimte_wordt_opgeslagen(
    cli: ZadCli,
    db_project: CreatedProject,
    forgejo: ForgejoClient,
) -> None:
    """Eerst de gewone weg, anders meet elke weigering hieronder ook 'het werkt sowieso niet'.

    Dit is de tegencontrole bij de drie toetsen erna: zonder een aantoonbaar geslaagde
    schrijfactie is een rode exitcode op een te grote waarde net zo goed een CLI die
    helemaal niets kan.
    """
    waarde = 42
    assert waarde != _STANDAARD, "kies een waarde die van de standaard verschilt, anders bewijst het niets"

    resultaat = _zet(cli, waarde).assert_ok()
    logger.info("binnen de speelruimte: %s", resultaat.uitvoer.strip()[:300])

    assert forgejo.wait_for_condition(
        db_project.name,
        lambda yaml: _limiet_in_projectbestand(forgejo, db_project.name) == waarde,
        timeout=180.0,
    ), f"'{_VELD}: {waarde}' staat niet in het projectbestand van '{db_project.name}'"


@pytest.mark.parametrize(
    ("waarde", "wat"),
    [
        (_MINIMUM - 1, "onder de ondergrens"),
        (_MAXIMUM + 1, "boven de bovengrens"),
    ],
)
def test_buiten_de_speelruimte_wordt_geweigerd(
    cli: ZadCli,
    db_project: CreatedProject,
    forgejo: ForgejoClient,
    waarde: int,
    wat: str,
) -> None:
    """Weigeren is de helft; zeggen wat dan WEL mag is de andere helft.

    De feature-doc belooft een leesbare zin met de grenzen erin ("moet tussen 1 en 500
    liggen"), niet alleen een rode exitcode. Een weigering zonder die zin laat de
    gebruiker raden, dus wordt hij hier gemeten. En het projectbestand mag niet
    veranderd zijn: een weigering die pas na het schrijven komt is geen weigering.
    """
    ervoor = _limiet_in_projectbestand(forgejo, db_project.name)

    resultaat = _zet(cli, waarde).assert_faalt()
    uitvoer = resultaat.uitvoer
    logger.info("%s (%s): %s", wat, waarde, uitvoer.strip()[:300])

    assert "Traceback (most recent call last)" not in uitvoer, f"weigering met traceback:\n{uitvoer}"
    ontbreekt = [str(grens) for grens in (_MINIMUM, _MAXIMUM) if str(grens) not in uitvoer]
    assert not ontbreekt, (
        f"de weigering noemt {ontbreekt} niet, dus weet de gebruiker niet wat wel mag "
        f"(speelruimte {_MINIMUM}..{_MAXIMUM}):\n{uitvoer}"
    )

    assert _limiet_in_projectbestand(forgejo, db_project.name) == ervoor, (
        f"'{_VELD}' is in het projectbestand veranderd terwijl {waarde} geweigerd werd"
    )


def test_de_server_weigert_een_laag_die_de_dienst_niet_openzet(
    sandbox_url: str,
    db_project: CreatedProject,
    forgejo: ForgejoClient,
) -> None:
    """`connection-limit` staat op PROJECT en DEPLOYMENT, dus niet op een component.

    Rechtstreeks over de API en niet over de CLI, met opzet: de CLI kent de lagen uit de
    catalogus en weigert de aanroep zelf. Wat hier gemeten moet worden is de SERVER, want
    die is wat een script, een oudere CLI of een curl-aanroep raakt.
    """
    ervoor = _limiet_in_projectbestand(forgejo, db_project.name)
    url = (
        f"{sandbox_url.rstrip('/')}/api/v2/projects/{db_project.name}/services/{_DIENST}/config/component/{_COMPONENT}"
    )

    with httpx.Client(verify=_API_VERIFY_SSL, timeout=60.0) as client:
        respons = client.put(
            url,
            json={_VELD: 50},
            headers={"X-API-Key": db_project.api_key, "Content-Type": "application/json"},
        )

    logger.info("componentlaag: HTTP %d %s", respons.status_code, respons.text[:300])
    assert respons.status_code >= 400, (
        f"de server nam '{_VELD}' aan op de componentlaag (HTTP {respons.status_code}), "
        f"terwijl de dienst die laag niet openzet: {respons.text[:400]}"
    )

    assert _limiet_in_projectbestand(forgejo, db_project.name) == ervoor, (
        f"'{_VELD}' veranderde op projectniveau door een aanroep op de componentlaag"
    )


def test_een_veld_dat_de_dienst_niet_declareert_wordt_geweigerd(
    sandbox_url: str,
    db_project: CreatedProject,
) -> None:
    """ "Wat een dienst niet declareert, is niet instelbaar" - ook niet via de API.

    De feature-doc noemt dit als het antwoord op "mag een gebruiker dit zelf zetten".
    Zonder een toets erop is dat een zin in een document.
    """
    url = f"{sandbox_url.rstrip('/')}/api/v2/projects/{db_project.name}/services/{_DIENST}/config/project"

    with httpx.Client(verify=_API_VERIFY_SSL, timeout=60.0) as client:
        respons = client.put(
            url,
            json={"deze-instelling-bestaat-niet-rc227": 1},
            headers={"X-API-Key": db_project.api_key, "Content-Type": "application/json"},
        )

    logger.info("onbekend veld: HTTP %d %s", respons.status_code, respons.text[:300])
    assert respons.status_code >= 400, (
        f"de server nam een niet-gedeclareerd veld aan (HTTP {respons.status_code}): {respons.text[:400]}"
    )
