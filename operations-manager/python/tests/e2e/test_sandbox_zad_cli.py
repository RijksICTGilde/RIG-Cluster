"""De zad-cli als tweede afnemer van de API, tegen een draaiende sandbox.

Deze doorloop is de rookmelder voor de CLI-weg: project zien, component toevoegen, uitkomst
opvragen, opruimen, allemaal over de CLI. Waarom die weg apart gemeten wordt en wanneer
deze toetsen overslaan staat in ``tests/e2e/helpers/zad_cli.py``.

Draaien:

    E2E_BASE_URL=https://zad.sandbox.rijksapp.dev \
    E2E_SECRET_KEY=sandbox-dev-secret-key-fixed-for-stable-sessions-32min \
    uv run pytest tests/e2e/test_sandbox_zad_cli.py -m "e2e and sandbox" -v
"""

from __future__ import annotations

import contextlib
import logging
import os
from typing import TYPE_CHECKING

import httpx
import pytest
from tests.e2e.helpers import sandbox_api
from tests.e2e.helpers.lifecycle import RUNNABLE_IMAGE, create_project_via_wizard
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


@pytest.fixture(scope="module")
def cli_project(
    sandbox_context: BrowserContext,
    sandbox_url: str,
    forgejo: ForgejoClient,
) -> Generator[CreatedProject]:
    """Een project voor de hele module, aangemaakt via de wizard.

    Aanmaken gaat NIET over de CLI: ``zad project create`` meldt zich aan met het account
    van de gebruiker (OIDC), en deze suite heeft geen browserlogin maar een voorgetekend
    sessiecookie. De CLI werkt vanaf hier met de PROJECTSLEUTEL.
    """
    page = sandbox_context.new_page()
    try:
        project = create_project_via_wizard(
            page,
            sandbox_url,
            forgejo,
            unique_project_name("zadcli"),
            user_email=_USER_EMAIL,
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
def cli(sandbox_url: str, cli_project: CreatedProject) -> ZadCli:
    return ZadCli(
        skip_zonder_cli(),
        sandbox_url,
        api_key=cli_project.api_key,
        project=cli_project.name,
    )


def test_cli_ziet_het_project(cli: ZadCli, cli_project: CreatedProject) -> None:
    """`project status` over de CLI noemt het project dat de wizard aanmaakte.

    De sleutel wordt geaccepteerd en het antwoord is te lezen. De tegencontrole staat in
    ``test_cli_valt_om_op_een_dood_adres``.
    """
    resultaat = cli.run("project", "status", verwacht_json=True).assert_ok()
    payload = resultaat.json()

    tekst = str(payload)
    assert cli_project.name in tekst, f"'{cli_project.name}' staat niet in het CLI-antwoord: {payload}"


def test_cli_valt_om_op_een_dood_adres(cli: ZadCli, sandbox_url: str) -> None:
    """De tegencontrole bij de toets hierboven.

    Wijst ``ZAD_API_URL`` naar een adres waar niets luistert, dan moet de CLI met een
    niet-nul exitcode stoppen en dat zeggen. Doet hij dat niet, dan bewijst de groene
    regel hierboven alleen dat het commando bestaat.
    """
    dood = ZadCli(cli.pad, "https://127.0.0.1:9", api_key=cli.api_key, project=cli.project, timeout=90.0)
    resultaat = dood.run("project", "status").assert_faalt()

    assert resultaat.uitvoer.strip(), "de CLI stopte zonder ook maar iets te zeggen"


def test_component_toevoegen_via_cli_landt_in_het_projectbestand(
    cli: ZadCli,
    cli_project: CreatedProject,
    forgejo: ForgejoClient,
) -> None:
    """Een component toevoegen over de CLI, en teruglezen waar de waarheid staat.

    De HTTP-respons is hier niet het bewijs. Het projectbestand in Forgejo is dat wel,
    net als in ``test_sandbox_flows.py``: dat is wat ZAD daarna uitrolt.
    """
    naam = "cli-web"
    resultaat = cli.run(
        "component",
        "add",
        naam,
        "--image",
        RUNNABLE_IMAGE,
        "--deployment",
        cli_project.deployment_name,
        "--port",
        "8080",
    ).assert_ok()
    logger.info("component add: %s", resultaat.uitvoer.strip()[:400])

    assert forgejo.wait_for_component(cli_project.name, naam, timeout=240.0), (
        f"component '{naam}' staat na de CLI-aanroep niet in het projectbestand van '{cli_project.name}'"
    )

    lijst = cli.run("component", "list", verwacht_json=True).assert_ok()
    assert naam in str(lijst.json()), f"'{naam}' staat niet in `component list`: {lijst.stdout}"


def test_een_component_in_gebruik_wordt_geweigerd_en_de_uitweg_staat_in_de_api(
    cli: ZadCli,
    cli_project: CreatedProject,
    forgejo: ForgejoClient,
    sandbox_url: str,
) -> None:
    """Het einde van de doorloop, en de plek waar de CLI en de API uiteenlopen.

    De component uit de vorige toets hangt aan een deployment. De API weigert hem dan met
    409 en biedt `confirm_in_use` als uitweg; het antwoord noemt volgens het
    OpenAPI-document elke plek waar hij nog gebruikt wordt. `zad component delete` kent die
    vlag niet (gemeten op zad-cli 1.0.0: alleen `--yes` en `--dry-run`), dus opruimen over
    de CLI eindigt hier.

    Wat deze toets vastlegt is daarom niet "de CLI kan het niet" - dat zou een gebrek
    vastpinnen dat morgen gerepareerd mag worden. Hij legt de twee dingen vast die hoe dan
    ook moeten blijven gelden: de weigering is leesbaar en zonder traceback, en de API
    HOUDT de uitweg.
    """
    naam = "cli-web"
    if naam not in forgejo.component_names(cli_project.name):
        pytest.skip(f"'{naam}' staat niet in het projectbestand; de toets die hem aanmaakt liep niet")

    resultaat = cli.run("component", "delete", naam, "--yes")
    logger.info("component delete: exit %d %s", resultaat.exitcode, resultaat.uitvoer.strip()[:400])
    resultaat.assert_faalt()
    assert "Traceback (most recent call last)" not in resultaat.uitvoer, (
        f"de CLI braakte een traceback uit:\n{resultaat.uitvoer}"
    )

    with httpx.Client(verify=_API_VERIFY_SSL, timeout=60.0) as client:
        document = client.get(f"{sandbox_url.rstrip('/')}/openapi.json").raise_for_status().json()
    operatie = document["paths"]["/api/v2/projects/{project_name}/components/{component_name}"]["delete"]
    vlaggen = {parameter["name"] for parameter in operatie.get("parameters", [])}
    assert "confirm_in_use" in vlaggen, (
        f"de API biedt geen uitweg meer voor een component in gebruik; gevonden: {sorted(vlaggen)}"
    )

    # Opruimen langs de weg die deze repo wel heeft, zodat het project schoon achterblijft.
    status, _ = sandbox_api.delete_component(
        sandbox_url,
        cli_project.name,
        cli_project.api_key,
        component_name=naam,
        confirm_in_use=True,
        verify_ssl=_API_VERIFY_SSL,
    )
    assert status == 202, f"opruimen met confirm_in_use gaf {status}"
    assert forgejo.wait_for_condition(
        cli_project.name,
        lambda yaml: naam not in [c.get("name") for c in (yaml.get("components") or [])],
        timeout=240.0,
    ), f"component '{naam}' staat er na het opruimen nog in"


def test_onbekend_project_geeft_een_leesbare_fout(cli: ZadCli) -> None:
    """Een projectsleutel die niet bij dit project hoort mag geen stapel JSON opleveren.

    Wat hier gemeten wordt is het minimum: een niet-nul exitcode en een melding zonder
    traceback. Het blok eromheen staat in ``features/foutmeldingen.md``.
    """
    vreemd = ZadCli(cli.pad, cli.api_url.removesuffix("/api"), api_key=cli.api_key, project="bestaat-echt-niet-rc227")
    resultaat = vreemd.run("project", "status").assert_faalt()

    uitvoer = resultaat.uitvoer
    assert "Traceback (most recent call last)" not in uitvoer, f"de CLI braakte een traceback uit:\n{uitvoer}"
    assert uitvoer.strip(), "de CLI weigerde zonder iets te zeggen"


@pytest.fixture(autouse=True)
def _log_cli_versie(cli: ZadCli) -> None:
    """Welke CLI dit was, zodat een uitslag later terug te plaatsen is."""
    with contextlib.suppress(AssertionError):
        logger.info("zad-cli: %s", cli.run("--version").stdout.strip())
