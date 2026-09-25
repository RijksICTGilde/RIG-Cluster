"""De speelruimte van een dienst, gemeten op een draaiend cluster.

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

# Eigen tijdsbudget, en waarom dat moet staat in `features/e2e-sandbox-tests.md`.
# Boven het aanmaken met diensten (600s) in de setup en de langste toets hieronder: twee
# CLI-aanroepen (300s elk) met de wachten op het projectbestand ertussen.
pytestmark = [pytest.mark.e2e, pytest.mark.sandbox, pytest.mark.timeout(1200)]

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
            # 240s is de default van de helper; op dit GEDEELDE cluster haalt een project
            # met diensten dat niet altijd.
            create_timeout=600.0,
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


def _zet(sandbox_url: str, project: CreatedProject, waarde: object, *, scope: str = "shared") -> httpx.Response:
    """De projectlaag van de dienst schrijven, rechtstreeks over de API.

    Dit is de kortste weg naar de grendel: geen browser, geen client ertussen. De CLI-weg
    staat er als eigen toets naast
    (``test_de_speelruimte_houdt_ook_als_de_cli_de_waarde_stuurt``), want dat is een tweede
    afnemer met een eigen beeld van het schema.
    """
    url = f"{sandbox_url.rstrip('/')}/api/v2/projects/{project.name}/services/{_DIENST}/config/project"
    with httpx.Client(verify=_API_VERIFY_SSL, timeout=120.0) as client:
        return client.put(
            url,
            json={"scope": scope, _VELD: waarde},
            headers={"X-API-Key": project.api_key, "Content-Type": "application/json"},
        )


def _limiet_in_projectbestand(forgejo: ForgejoClient, project: str) -> object:
    return _limiet_uit_yaml(forgejo.get_project_yaml(project) or {})


def _limiet_uit_yaml(yaml: dict) -> object:
    """De waarde van het veld in een AL GELEZEN projectbestand.

    ``wait_for_condition`` geeft zijn predicate het bestand dat het net ophaalde. Dat
    gebruiken scheelt niet alleen een ronde over het netwerk: haal je het bestand binnen de
    predicate nog een keer op, dan beslis je op een ANDERE lezing dan de lezing die hij
    straks teruggeeft.
    """
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
    sandbox_url: str,
    db_project: CreatedProject,
    forgejo: ForgejoClient,
) -> None:
    """Eerst de gewone weg, anders meet elke weigering hieronder ook 'het werkt sowieso niet'.

    Dit is de tegencontrole bij de twee toetsen erna: zonder een aantoonbaar geslaagde
    schrijfactie is een 4xx op een te grote waarde net zo goed een endpoint dat niets kan.
    """
    waarde = 42
    assert waarde != _STANDAARD, "kies een waarde die van de standaard verschilt, anders bewijst het niets"

    respons = _zet(sandbox_url, db_project, waarde)
    assert respons.status_code < 300, (
        f"een waarde binnen de speelruimte werd geweigerd: {respons.status_code} {respons.text[:300]}"
    )

    assert forgejo.wait_for_condition(
        db_project.name,
        lambda yaml: _limiet_uit_yaml(yaml) == waarde,
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
    sandbox_url: str,
    db_project: CreatedProject,
    forgejo: ForgejoClient,
    waarde: int,
    wat: str,
) -> None:
    """Weigeren is de helft; zeggen wat dan WEL mag is de andere helft.

    De feature-doc belooft een leesbare zin met de grenzen erin ("moet tussen 1 en 500
    liggen"), niet alleen een foutcode. Een weigering zonder die zin laat de gebruiker
    raden. En het projectbestand mag niet veranderd zijn: een weigering die pas na het
    schrijven komt is geen weigering.
    """
    ervoor = _limiet_in_projectbestand(forgejo, db_project.name)

    respons = _zet(sandbox_url, db_project, waarde)
    logger.info("%s (%s): HTTP %d %s", wat, waarde, respons.status_code, respons.text[:300])

    # DIT ENDPOINT IS ASYNCHROON. Het antwoordt 202 met een taak-id, ook op een waarde die
    # buiten de speelruimte valt: de grendel zit in de VERWERKING en niet op de HTTP-grens.
    # Een toets die hier op een 4xx wacht meet daarom niets, en voor een gebruiker betekent
    # het dat een ongeldige waarde er eerst aangenomen uitziet.
    assert respons.status_code == 202, (
        f"verwachtte 202 van dit asynchrone endpoint, kreeg {respons.status_code}: {respons.text[:300]}"
    )

    taak_id = respons.json().get("task_id")
    assert taak_id, f"geen taak-id in het antwoord: {respons.text[:300]}"
    uitkomst, reden = sandbox_api.task_outcome(
        sandbox_url,
        taak_id,
        db_project.api_key,
        verify_ssl=_API_VERIFY_SSL,
        timeout=300.0,
    )
    logger.info("%s (%s): taak %s -- %s", wat, waarde, uitkomst, (reden or "")[:400])
    assert uitkomst == "failed", f"de taak accepteerde {waarde} ({uitkomst}); de speelruimte hield niet"

    tekst = reden or ""
    assert "Traceback (most recent call last)" not in tekst, f"weigering met traceback:\n{tekst}"

    # Op de hele ZIN en niet op de twee getallen apart: "1" komt in bijna elke tekst voor
    # (ook in "501"), dus een controle per getal zou groen blijven op een weigering die de
    # speelruimte helemaal niet noemt.
    zin = f"moet tussen {_MINIMUM} en {_MAXIMUM} liggen"
    assert zin in " ".join(tekst.split()), f"de weigering zegt niet {zin!r}:\n{tekst[:600]}"

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

    De weigering is hier de ROUTERING: de server maakt per dienst alleen een endpoint voor
    de lagen die de dienst openzet, dus de componentlaag bestaat voor deze dienst niet. Dat
    staat er daarom naast gemeten op het document. Zonder die helft is een 404 op dit pad
    net zo goed een typefout in de URL, en dan blijft deze toets groen als het hele
    lagenmechanisme verdwijnt.
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
        document = client.get(f"{sandbox_url.rstrip('/')}/openapi.json").raise_for_status().json()

    logger.info("componentlaag: HTTP %d %s", respons.status_code, respons.text[:300])
    assert respons.status_code == 404, (
        f"de server nam '{_VELD}' aan op de componentlaag (HTTP {respons.status_code}), "
        f"terwijl de dienst die laag niet openzet: {respons.text[:400]}"
    )

    lagen = {
        pad.split(f"/services/{_DIENST}/config/", 1)[1]
        for pad in document["paths"]
        if f"/services/{_DIENST}/config/" in pad
    }
    assert "component/{component_name}" not in lagen, f"de dienst zet de componentlaag nu wel open: {sorted(lagen)}"
    assert {"project", "deployment/{deployment_name}"} <= lagen, (
        f"de lagen die deze dienst wel openzet zijn niet allebei meer bereikbaar: {sorted(lagen)}"
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

    `scope` gaat mee, en dat is de hele meting. Zonder dat veld valt het body al op de
    discriminator van de scope-unie, en dan zegt een 4xx niets over het onbekende veld: de
    toets zou net zo groen zijn als elk veld gewoon werd aangenomen.
    """
    url = f"{sandbox_url.rstrip('/')}/api/v2/projects/{db_project.name}/services/{_DIENST}/config/project"

    with httpx.Client(verify=_API_VERIFY_SSL, timeout=60.0) as client:
        respons = client.put(
            url,
            json={"scope": "shared", "deze-instelling-bestaat-niet-rc227": 1},
            headers={"X-API-Key": db_project.api_key, "Content-Type": "application/json"},
        )

    logger.info("onbekend veld: HTTP %d %s", respons.status_code, respons.text[:300])
    assert respons.status_code >= 400, (
        f"de server nam een niet-gedeclareerd veld aan (HTTP {respons.status_code}): {respons.text[:400]}"
    )


def test_de_speelruimte_houdt_ook_als_de_cli_de_waarde_stuurt(
    sandbox_url: str,
    db_project: CreatedProject,
    forgejo: ForgejoClient,
) -> None:
    """Dezelfde grens, over de tweede afnemer: `zad service config set`.

    Het plan van RC-227 vroeg hierom en noemde het de meest waardevolle toets van dit blok.
    Hij staat er als PAAR: een te lage waarde mag het projectbestand niet raken, en daarna
    verandert een geldige waarde het wel. Zonder die tweede helft bewijst de eerste niets,
    want een CLI die de aanroep helemaal niet doet laat het bestand ook onveranderd.

    Wat de CLI zelf van de weigering laat zien wordt hier NIET vastgepind: dit endpoint is
    asynchroon (202 met een taak-id), dus of de CLI op de uitkomst wacht is een keuze in de
    zad-cli-repository en geen belofte van deze repo.
    """
    cli = ZadCli(skip_zonder_cli(), sandbox_url, api_key=db_project.api_key, project=db_project.name)
    geldig = 43
    assert geldig != _STANDAARD, "kies een waarde die van de standaard verschilt"

    ervoor = _limiet_in_projectbestand(forgejo, db_project.name)
    assert ervoor != _MINIMUM - 1, "de beginstand mag niet al de te lage waarde zijn"

    te_laag = cli.run(
        "service", "config", "set", _DIENST, "--target", "project", "--set", f"{_VELD}={_MINIMUM - 1}", "--yes"
    )
    logger.info("CLI zet %d: exit %d %s", _MINIMUM - 1, te_laag.exitcode, te_laag.uitvoer.strip()[:400])
    assert "Traceback (most recent call last)" not in te_laag.uitvoer, (
        f"de CLI braakte een traceback uit:\n{te_laag.uitvoer}"
    )

    assert not forgejo.wait_for_condition(
        db_project.name,
        lambda yaml: _limiet_uit_yaml(yaml) == _MINIMUM - 1,
        # Een negatief bewijs kost de volle wachttijd, dus korter dan de 180s hieronder: een
        # geslaagde schrijfactie staat er binnen die tijd wel.
        timeout=60.0,
    ), f"'{_VELD}: {_MINIMUM - 1}' belandde via de CLI toch in het projectbestand"

    geldige_run = cli.run(
        "service", "config", "set", _DIENST, "--target", "project", "--set", f"{_VELD}={geldig}", "--yes"
    )
    logger.info("CLI zet %d: exit %d %s", geldig, geldige_run.exitcode, geldige_run.uitvoer.strip()[:400])
    geldige_run.assert_ok()

    assert forgejo.wait_for_condition(
        db_project.name,
        lambda yaml: _limiet_uit_yaml(yaml) == geldig,
        timeout=180.0,
    ), "een geldige waarde kwam via de CLI niet in het projectbestand; de weigering hierboven bewijst dan niets"


def test_het_veld_staat_in_het_document_dat_de_cli_leest(sandbox_url: str) -> None:
    """De serverkant, want dat is de kant die deze repo bezit.

    `connection-limit` moet in het OpenAPI-document blijven staan als een veld van de
    projectlaag, in allebei de scope-takken. Daarop steunt elke client die het veld aanbiedt:
    verdwijnt het hier, dan kan geen enkele CLI het nog versturen.

    Wat een CLI met het veld DOET wordt hier niet vastgepind; dat zou deze toets rood maken
    zodra iemand daar iets repareert, en dat is precies verkeerd om.
    """
    with httpx.Client(verify=_API_VERIFY_SSL, timeout=60.0) as client:
        document = client.get(f"{sandbox_url.rstrip('/')}/openapi.json").raise_for_status().json()

    schemas = document["components"]["schemas"]
    for tak in ("SharedScopeConfig", "ProjectScopeConfig"):
        eigenschappen = schemas[tak].get("properties") or {}
        assert _VELD in eigenschappen, f"'{_VELD}' staat niet meer in {tak}: {sorted(eigenschappen)}"

    unie = schemas["PostgresqlDatabaseProjectConfig"]
    mapping = (unie.get("discriminator") or {}).get("mapping") or {}
    assert set(mapping) == {"shared", "project"}, f"de scopes van de projectlaag zijn veranderd: {mapping}"
