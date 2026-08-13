"""Live sandbox E2E voor de twee dingen die de tweede generale repetitie moest aantonen.

Beide komen rechtstreeks uit ``docs/generale-repetitie-2026-08-12.md`` en hadden daar geen
vangrail op de LEVENDE keten:

1. **Een afgewezen handeling levert een taak op waarvan de STATUS dat zegt** (bevinding 5).
   De beslissing zelf staat al vast in ``tests/test_task_status_reports_failure.py``, maar
   dat toetst de worker met nagemaakte handlers. Wat daar niet in zit is de weg die een
   aanroeper werkelijk loopt: een echte POST, een echte taak, en het veld waar de zad-cli
   als eerste op kijkt. Precies dat veld stond op ``completed`` terwijl het werk was
   geweigerd.

2. **De TLS-override per deployment-component doet wat hij belooft** (RC-78). De vorige
   doorloop heeft die expliciet NIET getoetst. ``tests/test_deployment_certificate_override.py``
   dekt het model, de haak en het gerenderde sjabloon; hier gaat het door de echte modal,
   het echte projectbestand en het echte ingress-object op het cluster. Dat is de enige
   plek waar blijkt dat de vier lagen onderweg niet alsnog uit elkaar lopen.

De toets van record is het projectbestand in Forgejo en het object op het cluster, niet de
pagina. Slaat over zonder E2E_BASE_URL.

Draaien:

    E2E_BASE_URL=https://zad.sandbox.rijksapp.dev \
    E2E_SECRET_KEY=sandbox-dev-secret-key-fixed-for-stable-sessions-32min \
    uv run pytest tests/e2e/test_sandbox_repetitie.py -m "e2e and sandbox" -q -o addopts=""
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import time
from typing import TYPE_CHECKING

import httpx
import pytest
from opi.services import ServiceType
from opi.services.registry import get_service
from tests.e2e.helpers import sandbox_api
from tests.e2e.helpers.edit_modal import EditModalHelper
from tests.e2e.helpers.lifecycle import RUNNABLE_IMAGE, create_project_with_services

if TYPE_CHECKING:
    from collections.abc import Generator

    from playwright.sync_api import BrowserContext
    from tests.e2e.helpers.forgejo import ForgejoClient

pytestmark = [pytest.mark.e2e, pytest.mark.sandbox]

_API_VERIFY_SSL = os.environ.get("E2E_API_VERIFY_SSL", "false").lower() in ("1", "true", "yes")
_USER_EMAIL = os.environ.get("E2E_SANDBOX_USER", "admin@sandbox.rijksapp.dev")

#: De dienst die het component vraagt zonder dat het project hem heeft. Hij moet op
#: projectniveau gekozen worden, dus de aanvraag hoort geweigerd te worden. Welke dienst
#: dat is, is geen aanname: de registry beslist het (``implicit_project_entry() is None``)
#: en de test controleert dat hieronder, zodat deze test niet stil iets anders gaat meten
#: als die regel verschuift.
GEWEIGERDE_DIENST = "attachments"

#: De tweede deployment, die zijn eigen certificaatafhandeling krijgt.
TWEEDE_DEPLOYMENT = "staging"


def _kubectl_json(*args: str) -> dict:
    """Vraag het cluster zelf naar een object; {} als kubectl er niet is of het er niet is."""
    try:
        result = subprocess.run(
            ["kubectl", *args, "-o", "json"], capture_output=True, text=True, timeout=60, check=False
        )
    except OSError, subprocess.SubprocessError:
        return {}
    if result.returncode != 0:
        return {}
    with contextlib.suppress(json.JSONDecodeError):
        return json.loads(result.stdout or "{}")
    return {}


def _ingress_annotaties(project_name: str, ingress: str) -> dict[str, str]:
    obj = _kubectl_json("get", "ingress", ingress, "-n", f"rig-{project_name}")
    return (obj.get("metadata") or {}).get("annotations") or {}


def _deployment_component_config(forgejo: ForgejoClient, project_name: str, deployment: str) -> dict:
    """De publish-on-web-config op de deployment-component-laag, of {}.

    Die laag bewaart zijn diensten als een echte MAP (anders dan de component-laag, die een
    gemengde lijst gebruikt); dat verschil is exact waarom de override een eigen pad heeft.
    """
    data = forgejo.get_project_yaml(project_name) or {}
    for dep in data.get("deployments") or []:
        if dep.get("name") != deployment:
            continue
        for component in dep.get("components") or []:
            diensten = component.get("services")
            if isinstance(diensten, dict):
                return (diensten.get("publish-on-web") or {}).get("config") or {}
    return {}


def _wacht_op(voorwaarde, timeout_s: float = 180.0, interval: float = 5.0):
    """Pollen tot de voorwaarde iets waars teruggeeft; geeft de laatste waarde terug.

    Wachten op de VOORWAARDE en niet op de klok: een opslag commit, verwerkt opnieuw en
    rolt uit, en dat is soms in tien seconden klaar. Een vaste sleep kost hier elke keer
    de volle tijd en verbergt bovendien een mislukking net zo goed als een vertraging.
    """
    deadline = time.monotonic() + timeout_s
    waarde = voorwaarde()
    while not waarde and time.monotonic() < deadline:
        time.sleep(interval)
        waarde = voorwaarde()
    return waarde


@pytest.fixture(scope="module")
def repetitie_project(
    sandbox_context: BrowserContext, sandbox_url: str, forgejo: ForgejoClient
) -> Generator[tuple[str, str]]:
    """Een project met publish-on-web en een TWEEDE deployment.

    Twee deployments zijn geen luxe voor deze module: de override is er juist om er een
    anders te laten zijn dan de ander, dus zonder een tweede deployment kan de test het
    verschil niet meten en zou hij ook groen zijn als de instelling projectbreed landde.
    """
    page = sandbox_context.new_page()
    try:
        project = create_project_with_services(
            page,
            sandbox_url,
            forgejo,
            "repetitie",
            user_email=_USER_EMAIL,
            services=["publish-on-web"],
            create_timeout=600.0,
        )
    finally:
        page.close()

    try:
        taak = sandbox_api.start_task(
            sandbox_url,
            "POST",
            f"/api/v2/projects/{project.name}/:upsert-deployment",
            project.api_key,
            {
                "deploymentName": TWEEDE_DEPLOYMENT,
                "components": [{"reference": "web", "image": RUNNABLE_IMAGE}],
            },
            verify_ssl=_API_VERIFY_SSL,
        )
        sandbox_api.wait_for_task(sandbox_url, taak, project.api_key, verify_ssl=_API_VERIFY_SSL, timeout=600)
        yield project.name, project.api_key
    finally:
        with contextlib.suppress(Exception):
            sandbox_api.delete_project_via_api(
                sandbox_url, project.name, project.api_key, verify_ssl=_API_VERIFY_SSL, timeout=600
            )


def test_de_geweigerde_dienst_moet_echt_geweigerd_worden() -> None:
    """De dienst die deze module gebruikt weigert zichzelf aan te melden, volgens de registry.

    Zonder deze controle zou de test hieronder groen blijven terwijl hij niets meer meet:
    een dienst die zich WEL mag aanmelden wordt gewoon toegevoegd, de taak slaagt, en dan
    toetst 'de taak meldt zijn mislukking' een taak die niet mislukt is.
    """
    assert get_service(ServiceType(GEWEIGERDE_DIENST)).implicit_project_entry() is None, (
        f"'{GEWEIGERDE_DIENST}' mag zich inmiddels wel impliciet aanmelden; kies een andere "
        f"dienst voor deze test, anders meet zij niets meer"
    )


def test_een_afgewezen_component_levert_een_taak_die_zegt_dat_het_misging(
    sandbox_url: str, repetitie_project: tuple[str, str], forgejo: ForgejoClient
) -> None:
    """Een geweigerde componenttoevoeging: ``status`` zelf zegt 'failed'.

    Bevinding 5 van de eerste doorloop: de fout stond in ``result.status``, in
    ``error_message`` en op de subtaak, maar ``status`` meldde ``completed``. Een aanroeper
    die daarop polt - en dat doet de zad-cli - zag een afgewezen wijziging aan voor een
    geslaagde. Daarom staat ``status`` hier vooraan in de assertie en niet als bijvangst.
    """
    project_name, api_key = repetitie_project
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}

    with httpx.Client(verify=_API_VERIFY_SSL, timeout=30.0) as client:
        response = client.post(
            f"{sandbox_url}/api/v2/projects/{project_name}/components",
            headers=headers,
            json={
                "name": "geweigerd",
                "image": RUNNABLE_IMAGE,
                "port": 8080,
                "services": [GEWEIGERDE_DIENST],
                "deployment_names": ["productie"],
            },
        )
        # De aanvraag wordt AANGENOMEN; het weigeren gebeurt in het werk, en dat is precies
        # waarom de uitkomst alleen uit de taak te lezen is.
        assert response.status_code == 202, f"verwachtte 202, kreeg {response.status_code}: {response.text}"
        task_id = (response.headers.get("Location") or "").rsplit("/", 1)[-1] or response.json().get("task_id")
        assert task_id, f"geen taak-id in het antwoord: {response.text}"

        def _terminaal():
            poll = client.get(f"{sandbox_url}/api/tasks/{task_id}", headers=headers)
            if poll.status_code != 200:
                return None
            body = poll.json()
            return body if body.get("status") in ("completed", "failed") else None

        taak = _wacht_op(_terminaal, timeout_s=300, interval=3)

    assert taak, f"taak {task_id} werd niet terminaal binnen de tijd"
    assert taak["status"] == "failed", (
        f"een geweigerde componenttoevoeging meldt status '{taak['status']}' in plaats van 'failed'; "
        f"wie op status polt ziet een afgewezen wijziging aan voor een geslaagde. Taak: {taak}"
    )
    assert GEWEIGERDE_DIENST in str(taak.get("error_message") or ""), (
        f"de fouttekst noemt niet welke dienst geweigerd werd: {taak.get('error_message')!r}"
    )
    assert any(sub.get("status") == "failed" for sub in taak.get("subtasks") or []), (
        f"geen enkele subtaak staat op 'failed' terwijl de taak faalde: {taak.get('subtasks')}"
    )

    # En het projectbestand is niet half aangepast: het geweigerde component staat er niet.
    data = forgejo.get_project_yaml(project_name) or {}
    namen = [component.get("name") for component in data.get("components") or []]
    assert "geweigerd" not in namen, f"het geweigerde component staat toch in het projectbestand: {namen}"


def test_de_tls_override_geldt_alleen_voor_de_deployment_die_hem_zet(
    sandbox_page, sandbox_url: str, repetitie_project: tuple[str, str], forgejo: ForgejoClient
) -> None:
    """Een certificaatkeuze op EEN deployment verandert alleen die deployment (RC-78).

    Gemeten op drie plekken achter elkaar, want daar kan het onderweg misgaan:
    de modal biedt het veld aan op de deployment-component-laag, het projectbestand bewaart
    het op het pad van die laag (en niet op het component, wat beide deployments zou raken),
    en het ingress-object van die ene deployment draagt de bijbehorende annotatie terwijl
    dat van de andere hem niet heeft.

    ``passthrough`` en niet ``provided``: dat is de modus die GEEN certificaat-bijlage
    nodig heeft, dus de test hoeft geen certificaat te verzinnen om de override zelf te
    kunnen meten.
    """
    project_name, _ = repetitie_project
    data = forgejo.get_project_yaml(project_name) or {}
    deployments = [dep.get("name") for dep in data.get("deployments") or []]
    index = deployments.index(TWEEDE_DEPLOYMENT)

    modal = EditModalHelper(sandbox_page, sandbox_url, project_name)
    modal.open_detail_page()
    modal.open_edit_modal(f"modal-edit-deployment-{index}", "Deployment bewerken")

    veld = f"deployments[{index}]/components[0]/services/publish-on-web/config/tls"
    keuze = sandbox_page.locator(f"[name='{veld}']").first
    assert keuze.count() > 0, (
        "de deploymentmodal biedt geen TLS-keuze per component aan; de dienst levert zijn "
        f"velden niet aan de deployment-component-laag (verwacht veld: {veld})"
    )
    opties = sandbox_page.eval_on_selector_all(f"[name='{veld}'] option", "els => els.map(e => e.value)")
    assert "" in opties, f"er is geen 'erven van het component'-optie; leeg moet erven betekenen: {opties}"
    assert "passthrough" in opties, f"'passthrough' ontbreekt in de keuzes: {opties}"

    modal.select_with_rerender(keuze, "passthrough")
    modal.submit_step_expect_progress()
    modal.wait_for_progress_complete(timeout=300000)

    # 1. Het projectbestand bewaart hem op de deployment-component-laag.
    config = _wacht_op(
        lambda: _deployment_component_config(forgejo, project_name, TWEEDE_DEPLOYMENT) or None,
        timeout_s=180,
    )
    assert (config or {}).get("tls") == "passthrough", (
        f"de override staat niet op de deployment-component-laag van '{TWEEDE_DEPLOYMENT}': {config}"
    )

    # De ANDERE deployment heeft niets gekregen: leeg is erven, en erven moet leeg blijven
    # staan in het bestand. Zou de waarde op het component belanden, dan gold hij voor beide.
    ander = _deployment_component_config(forgejo, project_name, "productie")
    assert "tls" not in ander, (
        f"de andere deployment kreeg de override er ook bij ({ander}); dan is het geen override per deployment"
    )

    # 2. En het cluster rendert er een ander ingress-object van.
    annotatie = "nginx.ingress.kubernetes.io/ssl-passthrough"
    van_staging = _wacht_op(
        lambda: _ingress_annotaties(project_name, f"{TWEEDE_DEPLOYMENT}-web").get(annotatie),
        timeout_s=240,
    )
    if not _ingress_annotaties(project_name, f"{TWEEDE_DEPLOYMENT}-web"):
        pytest.skip("kubectl geeft geen ingress terug; het clusterdeel van deze toets is niet te meten")
    assert van_staging == "true", (
        f"het ingress van '{TWEEDE_DEPLOYMENT}' draagt de passthrough-annotatie niet: {van_staging!r}"
    )
    assert annotatie not in _ingress_annotaties(project_name, "productie-web"), (
        "het ingress van 'productie' kreeg de passthrough-annotatie ook, terwijl die deployment "
        "de instelling van het component hoort te volgen"
    )
