"""Jacht op punt 14 uit de zad-cli-doorlopen: `deployment_not_found` op de eigen deployment.

Punt 14 beschrijft een intermitterende fout. `zad deployment create <naam> --component web`
faalt af en toe met::

    error_type: deployment_not_found
    failed:    Component aan deployment toevoegen: Deployment 'productie' not found in project 'p1-huk'
    completed: Component validatie

Dat is de deelstap uit ``handle_add_component_to_deployment``: de deployment is vlak
daarvoor aangemaakt door een ``:upsert-deployment``-taak, en de taak die er een component
aan hangt vindt hem niet terug.

De zad-cli kon het los niet naspelen (met uitrollen aan, met uitrollen uit terwijl er al
een deployment is, en de eerste deployment van een vers project met uitrollen uit). Hun
observatie was dat het alleen optreedt als er **genoeg niet-uitgerolde wijzigingen wachten**.
Deze module bouwt precies die toestand en herhaalt het paar dan een aantal keer:

* een project met meerdere componenten;
* een stapel wijzigingen met ``rollout=false``, die per ronde verder groeit;
* per ronde ``:upsert-deployment`` (wachten tot de taak KLAAR is) en daarna meteen
  ``POST /deployments/<naam>/components``.

Het wachten op de eerste taak is essentieel voor de bewijswaarde. Vuur je het paar zonder
te wachten af, dan is "niet gevonden" geen bug maar de verwachte uitkomst; de zad-cli
wacht wel, dus deze test ook. Die niet-wachtende variant staat er apart in, puur om vast
te leggen wat hij doet - hij telt niet mee in het oordeel.

De derde test is de scherpste: die laat een componentwijziging NOG lopen terwijl de
deployment wordt aangemaakt. Dat kan echt, want de taakpoort sluit alleen taken met
dezelfde deploymentnaam uit en een componentwijziging heeft er geen.

Bedoeld om te draaien TERWIJL de reallife-suite loopt, zodat er werkelijk belasting op de
store staat. Eigen marker (``punt14``), buiten de standaard sandboxrun::

    E2E_BASE_URL=... E2E_SECRET_KEY=... FORGEJO_URL=... FORGEJO_USER=... FORGEJO_PASSWORD=... \
    uv run pytest tests/e2e/test_sandbox_punt14.py -m punt14 -o addopts=""

Het aantal rondes staat op ``PUNT14_RONDES`` (standaard 10).
"""

from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from tests.e2e.conftest import FORGEJO_VERIFY_SSL, SANDBOX_TEST_USER
from tests.e2e.helpers import sandbox_api
from tests.e2e.helpers.lifecycle import RUNNABLE_IMAGE, CreatedProject, create_project_via_wizard
from tests.e2e.helpers.wizard import unique_project_name

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Generator

    from playwright.sync_api import BrowserContext
    from tests.e2e.helpers.forgejo import ForgejoClient

pytestmark = [pytest.mark.e2e, pytest.mark.sandbox, pytest.mark.punt14, pytest.mark.serial]

_VERIFY_SSL = FORGEJO_VERIFY_SSL

WEB = "web"  # door de wizard aangemaakt
EXTRA_COMPONENTS = ("alpha", "beta")

RONDES = int(os.environ.get("PUNT14_RONDES", "10"))
#: Rondes voor de variant MET uitrol: die kost minuten per ronde.
RONDES_UITROL = int(os.environ.get("PUNT14_RONDES_UITROL", "5"))
TASK_TIMEOUT = 600.0

#: Waar de meetregels per ronde naartoe gaan. Pytest toont de logregels van de call-fase
#: van een geslaagde test niet, en juist bij "niet gereproduceerd" IS die opsomming het
#: resultaat: hoe vaak, hoe snel, en met welke uitkomst. Dus schrijven we ze zelf weg,
#: net als PYTEST_VOORTGANG doet. Zonder de variabele verandert er niets.
METINGEN = os.environ.get("PUNT14_METINGEN")


def _meet(regel: str) -> None:
    """Log een meetregel en schrijf hem weg als PUNT14_METINGEN gezet is."""
    logger.info("Punt 14: %s", regel)
    if not METINGEN:
        return
    with open(METINGEN, "a", encoding="utf-8") as bestand:
        bestand.write(f"{time.strftime('%H:%M:%S')}  {regel}\n")


def _post(base_url: str, path: str, api_key: str, body: dict) -> str:
    return sandbox_api.start_task(base_url, "POST", path, api_key, body, verify_ssl=_VERIFY_SSL)


def _patch(base_url: str, path: str, api_key: str, body: dict) -> str:
    return sandbox_api.start_task(base_url, "PATCH", path, api_key, body, verify_ssl=_VERIFY_SSL)


def _await_task(base_url: str, task_id: str, api_key: str) -> dict[str, Any]:
    """Poll a task until terminal and return the raw envelope, without asserting.

    ``sandbox_api.task_outcome`` collapses everything to a string; hier is juist het
    ``error_type`` van het resultaat de vraag, dus wordt de hele taak teruggegeven.
    """
    base = base_url.rstrip("/")
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    deadline = time.monotonic() + TASK_TIMEOUT
    last: dict[str, Any] = {}
    with httpx.Client(verify=_VERIFY_SSL, timeout=30.0) as client:
        while time.monotonic() < deadline:
            # Een trage statuspoll is een TOESTAND, geen uitkomst. Deze tests zetten OPI
            # bewust onder gelijktijdige druk -- en met ``rollout=true`` wacht een patch op
            # een volledige ArgoCD-uitrol, veruit het traagste deel van een actie (RC-117).
            # Dan haalt een enkele poll de 30 seconden van de client soms niet, en zonder
            # deze vangst sneuvelde de hele test op die ene traagheid terwijl er nog uren
            # deadline over waren. We hebben al een eigen deadline: binnen die grens is
            # opnieuw vragen het juiste antwoord, en erbuiten valt de lus vanzelf uit.
            try:
                response = client.get(f"{base}/api/tasks/{task_id}", headers=headers)
            except httpx.TransportError as trager_dan_de_client:
                last = {"status": "poll_timeout", "error": str(trager_dan_de_client)}
                time.sleep(2.0)
                continue
            last = response.json() if response.content else {}
            if response.status_code == 200:
                return last
            if response.status_code != 202:
                return {"status": "poll_error", "http_status": response.status_code, "body": response.text}
            time.sleep(2.0)
    return {"status": "running", "last": last}


def _failure(task: dict[str, Any]) -> tuple[str, str] | None:
    """(error_type, message) van een mislukte taak, of None als hij goed ging."""
    result = task.get("result") or {}
    if task.get("status") != "completed":
        return "task_status_" + str(task.get("status")), str(task.get("error_message") or task)
    if result.get("status") == "failed":
        return str(result.get("error_type") or "unknown"), str(
            task.get("error_message") or result.get("error") or "unknown"
        )
    for sub in task.get("subtasks") or []:
        if sub.get("status") == "failed":
            return str(result.get("error_type") or "subtask_failed"), (
                f"{sub.get('name')}: {sub.get('error') or 'unknown'}"
            )
    return None


@pytest.fixture(scope="module")
def punt14_project(
    sandbox_context: BrowserContext,
    sandbox_url: str,
    forgejo: ForgejoClient,
) -> Generator[CreatedProject]:
    """Een project met drie componenten, aangemaakt via de echte wizard."""
    page = sandbox_context.new_page()
    project: CreatedProject | None = None
    try:
        project = create_project_via_wizard(
            page,
            sandbox_url,
            forgejo,
            unique_project_name(prefix="p14"),
            user_email=SANDBOX_TEST_USER["email"],
        )
        logger.info("Punt 14: project '%s' aangemaakt", project.name)
        # De extra componenten horen bij de toestand die punt 14 beschrijft
        # (meerdere componenten), en zijn ook wat er straks aan de deployment
        # gehangen wordt. Met rollout=false, want dat is de toestand uit punt 14.
        for name in EXTRA_COMPONENTS:
            task_id = _post(
                sandbox_url,
                f"/api/v2/projects/{project.name}/components?rollout=false",
                project.api_key,
                {"name": name, "image": RUNNABLE_IMAGE, "deployment_names": []},
            )
            task = _await_task(sandbox_url, task_id, project.api_key)
            assert _failure(task) is None, f"Component '{name}' toevoegen mislukte: {task}"
        yield project
    finally:
        page.close()
        if project is not None:
            sandbox_api.delete_project_via_api(sandbox_url, project.name, project.api_key, verify_ssl=_VERIFY_SSL)


def _deployment_in_git(forgejo: ForgejoClient, project_name: str, deployment: str) -> bool:
    """Staat de deployment in het gecommitte projectbestand? De git-stand is de waarheid."""
    data = forgejo.get_project_yaml(project_name) or {}
    return any(d.get("name") == deployment for d in (data.get("deployments") or []) if isinstance(d, dict))


def _stapel_wachtende_wijzigingen(base_url: str, project: CreatedProject, ronde: int) -> None:
    """Voeg niet-uitgerolde wijzigingen toe, zodat de stapel per ronde groeit."""
    for index, component in enumerate((WEB, *EXTRA_COMPONENTS)):
        task_id = _patch(
            base_url,
            f"/api/v2/projects/{project.name}/components/{component}?rollout=false",
            project.api_key,
            {"memory_limit": f"{192 + 8 * ronde + index}Mi"},
        )
        task = _await_task(base_url, task_id, project.api_key)
        failure = _failure(task)
        if failure is not None:
            logger.warning("Ronde %d: patch op '%s' mislukte: %s", ronde, component, failure)


def _maak_deployment(base_url: str, project: CreatedProject, deployment: str) -> dict[str, Any]:
    task_id = _post(
        base_url,
        f"/api/v2/projects/{project.name}/:upsert-deployment?rollout=false",
        project.api_key,
        {"deploymentName": deployment, "components": [{"reference": WEB, "image": RUNNABLE_IMAGE}]},
    )
    return _await_task(base_url, task_id, project.api_key)


def _hang_component_aan_deployment(
    base_url: str, project: CreatedProject, deployment: str, component: str
) -> dict[str, Any]:
    task_id = _post(
        base_url,
        f"/api/v2/projects/{project.name}/deployments/{deployment}/components?rollout=false",
        project.api_key,
        {"component_name": component, "image": RUNNABLE_IMAGE},
    )
    return _await_task(base_url, task_id, project.api_key)


def _gelijktijdige_rondes(
    project: CreatedProject,
    sandbox_url: str,
    forgejo: ForgejoClient,
    *,
    prefix: str,
    rondes: int,
    patch_uitrol: bool,
) -> list[str]:
    """Rondes waarin componentpatches NOG lopen terwijl de deployment wordt aangemaakt.

    Geeft de bevindingen terug; een lege lijst betekent dat punt 14 zich niet voordeed.
    """
    bevindingen: list[str] = []
    uitrol = "true" if patch_uitrol else "false"

    for ronde in range(rondes):
        deployment = f"{prefix}{ronde}"

        patch_ids = [
            _patch(
                sandbox_url,
                f"/api/v2/projects/{project.name}/components/{component}?rollout={uitrol}",
                project.api_key,
                {"memory_limit": f"{256 + 8 * ronde + index}Mi"},
            )
            for index, component in enumerate(EXTRA_COMPONENTS)
        ]
        upsert = _maak_deployment(sandbox_url, project, deployment)
        for patch_id in patch_ids:
            _await_task(sandbox_url, patch_id, project.api_key)

        upsert_failure = _failure(upsert)
        if upsert_failure is not None:
            bevindingen.append(f"ronde {ronde}: upsert van '{deployment}' mislukte: {upsert_failure}")
            continue

        add_failure = _failure(_hang_component_aan_deployment(sandbox_url, project, deployment, EXTRA_COMPONENTS[0]))
        in_git = _deployment_in_git(forgejo, project.name, deployment)
        _meet(
            f"gelijktijdig (uitrol={uitrol}), ronde {ronde}: "
            f"uitkomst {'OK' if add_failure is None else add_failure[0]}, deployment in git: {in_git}"
        )
        if add_failure is not None:
            bevindingen.append(
                f"ronde {ronde}: component aan '{deployment}' hangen faalde: {add_failure}; "
                f"deployment staat {'WEL' if in_git else 'NIET'} in het projectbestand in git"
            )
        elif not in_git:
            bevindingen.append(f"ronde {ronde}: '{deployment}' ontbreekt in het projectbestand in git")

    return bevindingen


@pytest.mark.timeout(5400)
def test_deployment_create_vindt_zijn_eigen_deployment(
    punt14_project: CreatedProject,
    sandbox_url: str,
) -> None:
    """Herhaal `deployment create --component X` en let op `deployment_not_found`.

    Per ronde: eerst de stapel wachtende wijzigingen laten groeien, dan de deployment
    aanmaken en WACHTEN tot die taak klaar is, en er dan een component aan hangen. De
    tweede taak moet de deployment van de eerste zien.
    """
    project = punt14_project
    bevindingen: list[str] = []
    gemeten: list[str] = []

    for ronde in range(RONDES):
        deployment = f"p14r{ronde}"
        _stapel_wachtende_wijzigingen(sandbox_url, project, ronde)

        start = time.monotonic()
        upsert = _maak_deployment(sandbox_url, project, deployment)
        upsert_failure = _failure(upsert)
        if upsert_failure is not None:
            bevindingen.append(f"ronde {ronde}: upsert van '{deployment}' mislukte: {upsert_failure}")
            continue
        klaar = time.monotonic()

        add = _hang_component_aan_deployment(sandbox_url, project, deployment, EXTRA_COMPONENTS[0])
        add_failure = _failure(add)
        duur_tussen = time.monotonic() - klaar

        regel = (
            f"ronde {ronde}: upsert {klaar - start:.1f}s, component erbij {duur_tussen:.1f}s, "
            f"uitkomst {'OK' if add_failure is None else add_failure[0]}"
        )
        gemeten.append(regel)
        _meet(regel)
        if add_failure is not None:
            bevindingen.append(f"ronde {ronde}: component aan '{deployment}' hangen faalde: {add_failure}")

    logger.info("Punt 14 metingen:\n%s", "\n".join(gemeten))
    assert not bevindingen, f"Punt 14 gereproduceerd in {len(bevindingen)} van de {RONDES} rondes:\n" + "\n".join(
        bevindingen
    )


@pytest.mark.timeout(5400)
def test_deployment_overleeft_een_gelijktijdige_componentwijziging(
    punt14_project: CreatedProject,
    sandbox_url: str,
    forgejo: ForgejoClient,
) -> None:
    """De scherpste vorm: een componentpatch loopt NOG terwijl de deployment wordt aangemaakt.

    De taakpoort in ``claim_next_task`` sluit twee taken op hetzelfde project alleen uit
    als hun ``deployment_name`` gelijk is (``is_not_distinct_from``). Een
    ``update_component``-taak krijgt GEEN deploymentnaam en een ``upsert_deployment``-taak
    wel, dus die twee draaien wel degelijk tegelijk over hetzelfde projectbestand. Wint de
    patch, dan is de zojuist geschreven deployment weer weg en vindt de volgende stap hem
    niet - precies wat punt 14 beschrijft.

    Daarom hier: patch starten zonder te wachten, meteen de deployment aanmaken, beide
    afwachten, en dan pas het component eraan hangen. De controle op het projectbestand in
    git staat erbij, want die zegt of de deployment werkelijk verdwenen is (verloren
    wijziging) of dat alleen de lezing hem miste (cache).
    """
    bevindingen = _gelijktijdige_rondes(
        punt14_project, sandbox_url, forgejo, prefix="p14g", rondes=RONDES, patch_uitrol=False
    )
    assert not bevindingen, (
        f"Punt 14 gereproduceerd in {len(bevindingen)} van de {RONDES} gelijktijdige rondes:\n" + "\n".join(bevindingen)
    )


@pytest.mark.timeout(7200)
def test_deployment_overleeft_een_gelijktijdige_uitrol(
    punt14_project: CreatedProject,
    sandbox_url: str,
    forgejo: ForgejoClient,
) -> None:
    """Dezelfde vorm, maar met de componentwijziging MET uitrol - het grootste venster.

    Met ``rollout=false`` is een componentpatch in seconden klaar, en dan is de kans dat
    hij een gelijktijdige deploymentaanmaak raakt klein. Met uitrol aan verwerkt hij het
    hele project (manifesten, ArgoCD) en duurt hij minuten, dus loopt hij gegarandeerd
    nog terwijl de deployment geschreven wordt. Is de gelijktijdigheid uit de taakpoort
    de oorzaak van punt 14, dan is dit de vorm waarin het zich laat zien.

    Weinig rondes, want elke ronde kost minuten. Regelbaar met ``PUNT14_RONDES_UITROL``.
    """
    bevindingen = _gelijktijdige_rondes(
        punt14_project, sandbox_url, forgejo, prefix="p14u", rondes=RONDES_UITROL, patch_uitrol=True
    )
    assert not bevindingen, (
        f"Punt 14 gereproduceerd in {len(bevindingen)} van de {RONDES_UITROL} rondes met uitrol:\n"
        + "\n".join(bevindingen)
    )


@pytest.mark.timeout(1800)
def test_zonder_wachten_is_de_deployment_er_nog_niet(
    punt14_project: CreatedProject,
    sandbox_url: str,
) -> None:
    """Vastleggen wat er gebeurt als je NIET op de upserttaak wacht.

    Dit is geen bug maar de verwachte uitkomst, en staat hier zodat het verschil met de
    test hierboven zwart op wit staat: die wacht wel, en moet dus slagen. Faalt deze
    variant niet, dan zegt dat iets over de timing van de sandbox op dat moment - dus
    wordt de uitkomst gelogd en niet als eis opgeschreven.
    """
    project = punt14_project
    deployment = "p14nowait"

    upsert_task_id = _post(
        sandbox_url,
        f"/api/v2/projects/{project.name}/:upsert-deployment?rollout=false",
        project.api_key,
        {"deploymentName": deployment, "components": [{"reference": WEB, "image": RUNNABLE_IMAGE}]},
    )
    add = _hang_component_aan_deployment(sandbox_url, project, deployment, EXTRA_COMPONENTS[1])
    add_failure = _failure(add)

    upsert = _await_task(sandbox_url, upsert_task_id, project.api_key)
    _meet(f"zonder wachten: upsert={_failure(upsert) or 'OK'}, component erbij={add_failure or 'OK'}")
