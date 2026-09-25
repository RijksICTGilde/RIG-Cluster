"""Klonen op een draaiend cluster: landt de data, en komt er geen `_vN` naast.

`features/kloonpoging.md` beschrijft vijf wijzigingen rond `clone-from`. De kern ervan is
niet de kloon zelf maar wat er GEEN tweede keer gebeurt: een run die opnieuw langskomt
mag geen nieuwe generatie naast de bestaande database zetten. Dat is precies het soort
uitspraak die je op een unittest niet kunt doen, want daar bestaat de database niet en is
de naam een string.

Wat hier NIET gemeten wordt, met de reden: het plan van RC-227 vroeg om een run die
HALVERWEGE wordt afgebroken. De vlag `clone-from.status.in-progress` gaat aan vlak voor de
databasekloon en uit bij het vastleggen, dus de afgebroken staat is "vlag op schijf,
doeldatabase aanwezig, doelschema's afwezig". Die staat is op dit cluster alleen te maken
door OPI midden in een run om te leggen of het projectbestand met de hand te schrijven. Het
eerste is op een GEDEELD cluster niet te doen zonder de run van een ander te raken, en het
tweede meet de toets zelf in plaats van de code. Die helft blijft dus bij de unittests
(`tests/test_clone_attempt_flag.py`); de laatste toets hieronder dekt wel de kant die er in
de praktijk toe doet, namelijk dat een tweede run geen generatie kost.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import uuid
from typing import TYPE_CHECKING, Any

import pytest
from tests.e2e.conftest import SANDBOX_TEST_USER
from tests.e2e.helpers import sandbox_api
from tests.e2e.helpers.lifecycle import RUNNABLE_IMAGE, create_project_with_services
from tests.e2e.helpers.wizard import unique_project_name
from tests.e2e.helpers.zad_cli import GEEN_CLI, ZadCli, cli_pad, skip_zonder_cli

if TYPE_CHECKING:
    from collections.abc import Generator

    from playwright.sync_api import BrowserContext
    from tests.e2e.helpers.forgejo import ForgejoClient
    from tests.e2e.helpers.lifecycle import CreatedProject

logger = logging.getLogger(__name__)

# Zonder de CLI is er geen kloon te maken, en dan meten de toetsen na de eerste een
# deployment die er nooit kwam. De skip staat daarom op de module, voor het project.
# `serial`: het merkteken gaat in de bron, daarna kloont de volgende toets hem, en de
# laatste meet wat een TWEEDE run doet. Die volgorde is het onderwerp, niet een gemak.
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.sandbox,
    pytest.mark.slow,
    pytest.mark.serial,
    pytest.mark.skipif(cli_pad() is None, reason=GEEN_CLI),
]

_VERIFY_SSL = os.environ.get("E2E_API_VERIFY_SSL", "false").lower() in ("1", "true", "yes")
_SERVICES = ["publish-on-web", "postgresql-database"]

#: De rij die de kloon moet meenemen. Een eigen waarde per run, zodat een tabel die van een
#: eerdere run was blijven staan niet als geslaagde kloon telt.
_MERKTEKEN = f"rc227-{uuid.uuid4().hex[:8]}"

#: De componentnaam die de wizardhelper aanmaakt.
_COMPONENT = "web"


@pytest.fixture(scope="module")
def kloon_project(
    sandbox_context: BrowserContext,
    sandbox_url: str,
    forgejo: ForgejoClient,
) -> Generator[CreatedProject]:
    page = sandbox_context.new_page()
    gemaakt: CreatedProject | None = None
    try:
        gemaakt = create_project_with_services(
            page,
            sandbox_url,
            forgejo,
            unique_project_name(prefix="kloon"),
            user_email=SANDBOX_TEST_USER["email"],
            services=_SERVICES,
            # 240s is de default van de helper; op dit GEDEELDE cluster haalt een project
            # met diensten dat niet altijd.
            create_timeout=600.0,
        )
        logger.info("kloonproject %s, brondeployment %s", gemaakt.name, gemaakt.deployment_name)
        yield gemaakt
    finally:
        page.close()
        if gemaakt is not None:
            sandbox_api.delete_project_via_api(sandbox_url, gemaakt.name, gemaakt.api_key, verify_ssl=_VERIFY_SSL)


@pytest.fixture(scope="module")
def cli(sandbox_url: str, kloon_project: CreatedProject) -> ZadCli:
    return ZadCli(skip_zonder_cli(), sandbox_url, api_key=kloon_project.api_key, project=kloon_project.name)


@pytest.fixture(scope="module")
def doel_deployment() -> str:
    return "kopie"


def _kubectl_json(args: list[str]) -> dict[str, Any]:
    ruw = subprocess.run(["kubectl", *args, "-o", "json"], capture_output=True, text=True, timeout=60, check=True)
    return json.loads(ruw.stdout)


def _secret(namespace: str, naam: str) -> dict[str, str]:
    data = _kubectl_json(["get", "secret", naam, "-n", namespace])["data"]
    return {sleutel: base64.b64decode(waarde).decode() for sleutel, waarde in data.items()}


def _psql(secret: dict[str, str], sql: str, *, database: str = "") -> tuple[int, str]:
    """SQL tegen de databaseserver van de deployment, met zijn eigen inloggegevens.

    Dezelfde vorm als in ``test_sandbox_restore_generation.py``; ``database`` maakt het
    mogelijk om de serverlijst (``postgres``) te bevragen in plaats van de eigen database.
    """
    resultaat = subprocess.run(
        [
            "kubectl",
            "run",
            f"psql-rc227-{uuid.uuid4().hex[:8]}",
            "-n",
            "rig-system",
            "--rm",
            "-i",
            "--restart=Never",
            "--image=postgres:16-alpine",
            "--env",
            f"PGPASSWORD={secret['DATABASE_PASSWORD']}",
            "--command",
            "--",
            "psql",
            "-h",
            secret["DATABASE_SERVER_HOST"],
            "-U",
            secret["DATABASE_SERVER_USER"],
            "-d",
            database or secret["DATABASE_DB"],
            "-v",
            "ON_ERROR_STOP=1",
            "-tAc",
            sql,
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    return resultaat.returncode, (resultaat.stdout + resultaat.stderr).strip()


#: Laatste regel van de leesquery hieronder. `kubectl run --rm -i` hangt aan een container die
#: nog moet starten en kan terugkomen met exitcode NUL terwijl alles wat psql schreef weg is
#: (gemeten in ``test_sandbox_migratie_006.py``). Voor een LEESquery is opnieuw proberen gratis,
#: en zonder dit onderscheid ziet "niets teruggekregen" eruit als "geen database gevonden".
_SLUITSTUK = "rc227-databases-klaar"


def _databases(secret: dict[str, str], prefix: str) -> list[str]:
    """Elke database op de server waarvan de naam met ``prefix`` begint."""
    for _ in (1, 2, 3):
        code, uit = _psql(
            secret,
            f"SELECT datname FROM pg_database WHERE datname LIKE '{prefix}%' ORDER BY datname; SELECT '{_SLUITSTUK}'",
            database="postgres",
        )
        assert code == 0, f"pg_database uitlezen mislukt: {uit}"
        if _SLUITSTUK in uit:
            # kubectl run --rm schrijft zijn eigen regel ("pod ... deleted") op stderr mee.
            return [regel.strip() for regel in uit.splitlines() if regel.strip().startswith(prefix)]
        logger.warning("de psql-pod gaf niets terug, opnieuw: %s", uit[:200])
    raise AssertionError(f"pg_database bleef onleesbaar; laatste uitvoer: {uit[:300]}")


def _deployment_secret(project: str, deployment: str) -> dict[str, str]:
    namespace = f"rig-{project}"
    namen = [
        item["metadata"]["name"]
        for item in _kubectl_json(["get", "secrets", "-n", namespace])["items"]
        if deployment in item["metadata"]["name"] and "database" in item["metadata"]["name"].lower()
    ]
    assert namen, f"geen databasesecret voor deployment '{deployment}' in {namespace}"
    return _secret(namespace, namen[0])


def _clone_blok(forgejo: ForgejoClient, project: str, deployment: str) -> dict[str, Any]:
    yaml = forgejo.get_project_yaml(project) or {}
    for item in yaml.get("deployments") or []:
        if item.get("name") == deployment:
            return item.get("clone-from") or {}
    return {}


def test_de_brondatabase_krijgt_een_merkteken(kloon_project: CreatedProject) -> None:
    """Zonder een rij in de bron bewijst een geslaagde kloon niets over de INHOUD."""
    secret = _deployment_secret(kloon_project.name, kloon_project.deployment_name)
    schema = secret["DATABASE_SCHEMA"]

    code, uit = _psql(
        secret,
        f'CREATE TABLE IF NOT EXISTS "{schema}".rc227_kloon (merk text); '
        f"INSERT INTO \"{schema}\".rc227_kloon VALUES ('{_MERKTEKEN}')",
    )
    assert code == 0, f"het merkteken kon niet in de brondatabase: {uit}"


def test_klonen_via_de_cli_neemt_de_data_mee(
    cli: ZadCli,
    kloon_project: CreatedProject,
    doel_deployment: str,
) -> None:
    """De gewone weg: een deployment met `clone-from` over de CLI.

    De uitkomst wordt in de DOELdatabase gemeten en niet in het antwoord van de CLI: dat
    laatste zegt alleen dat de taak startte.
    """
    # `--component` en `--image` moeten mee: de CLI weigert een deployment zonder inhoud
    # ("Provide --component + --image, or a manifest with -f/--file"). De component is
    # dezelfde als die van de brondeployment, want het gaat hier om de DATA en niet om de
    # applicatie.
    resultaat = cli.run(
        "deployment",
        "create",
        doel_deployment,
        "--component",
        _COMPONENT,
        "--image",
        RUNNABLE_IMAGE,
        "--clone-from",
        kloon_project.deployment_name,
        "--yes",
    )
    logger.info("deployment create --clone-from: exit %d %s", resultaat.exitcode, resultaat.uitvoer.strip()[:500])
    resultaat.assert_ok()

    doel = _deployment_secret(kloon_project.name, doel_deployment)
    code, uit = _psql(doel, f'SELECT merk FROM "{doel["DATABASE_SCHEMA"]}".rc227_kloon')
    assert code == 0, f"de doeldatabase is niet te bevragen: {uit}"
    assert _MERKTEKEN in uit, f"het merkteken uit de bron staat niet in de kloon: {uit}"


def test_de_kloon_kostte_geen_generatie(kloon_project: CreatedProject, doel_deployment: str) -> None:
    """Geen `_v1` naast de doeldatabase.

    Dit is de kern van het blok: een kloon die wel landt maar een generatie kost laat een
    lege database achter en verandert de naam die de deployment gebruikt.
    """
    doel = _deployment_secret(kloon_project.name, doel_deployment)
    prefix = f"{kloon_project.name.replace('-', '_')}_{doel_deployment.replace('-', '_')}"

    gevonden = _databases(doel, prefix)
    # Eerst dat de doeldatabase zelf in de lijst staat. Zonder die regel is "geen generatie"
    # ook waar als de LIKE niets vindt, en dan blijft deze toets groen op een naamgeving die
    # verandert of op een kloon die er helemaal niet kwam.
    assert gevonden, f"geen enkele database begint met '{prefix}'; dan meet 'geen _vN' niets"
    generaties = [naam for naam in gevonden if naam.startswith(f"{prefix}_v")]
    assert not generaties, f"er staat een generatie naast de doeldatabase: {gevonden}"


def test_het_projectbestand_meldt_de_kloon_als_afgerond(
    kloon_project: CreatedProject,
    doel_deployment: str,
    forgejo: ForgejoClient,
) -> None:
    """ "Uit: zodra de kloon als afgerond wordt vastgelegd."

    Een vlag die na een geslaagde kloon blijft staan laat elke volgende run denken dat er
    een poging liep, en dat is precies de staat die de failover onderdrukt.
    """
    blok = _clone_blok(forgejo, kloon_project.name, doel_deployment)
    assert blok, f"deployment '{doel_deployment}' heeft geen clone-from in het projectbestand"

    status = blok.get("status") or {}
    assert status.get("completed") is True, f"de kloon staat niet als afgerond in het bestand: {status}"
    assert not status.get("in-progress"), f"de pogingsvlag staat na een geslaagde kloon nog aan: {status}"


def test_nog_een_run_zet_er_geen_generatie_naast(
    cli: ZadCli,
    kloon_project: CreatedProject,
    doel_deployment: str,
) -> None:
    """De tweede run is waar het misging: `mode: once` mag niet opnieuw klonen.

    ``project refresh`` is de weg die een gebruiker daarvoor heeft. Gemeten wordt dat er
    daarna nog steeds geen `_vN` staat en dat het merkteken er precies EEN keer in staat:
    een tweede kloon over hetzelfde schema zou de rij verdubbelen of de run laten vastlopen.
    """
    # `project refresh` kent geen --yes (gemeten op zad-cli 0.13.1: alleen --force-clone
    # en --dry-run), dus die vlag hoort er niet bij.
    resultaat = cli.run("project", "refresh")
    logger.info("project refresh: exit %d %s", resultaat.exitcode, resultaat.uitvoer.strip()[:400])
    resultaat.assert_ok()

    doel = _deployment_secret(kloon_project.name, doel_deployment)
    prefix = f"{kloon_project.name.replace('-', '_')}_{doel_deployment.replace('-', '_')}"
    gevonden = _databases(doel, prefix)
    assert gevonden, f"geen enkele database begint met '{prefix}'; dan meet 'geen _vN' niets"
    generaties = [naam for naam in gevonden if naam.startswith(f"{prefix}_v")]
    assert not generaties, f"de tweede run zette er een generatie naast: {generaties}"

    code, uit = _psql(doel, f'SELECT count(*) FROM "{doel["DATABASE_SCHEMA"]}".rc227_kloon')
    assert code == 0, f"de doeldatabase is na de tweede run niet te bevragen: {uit}"
    tellingen = [regel.strip() for regel in uit.splitlines() if regel.strip().isdigit()]
    assert tellingen, f"geen telling terug uit de doeldatabase: {uit}"
    assert tellingen[0] == "1", f"de rij staat er niet precies een keer in: {uit}"
