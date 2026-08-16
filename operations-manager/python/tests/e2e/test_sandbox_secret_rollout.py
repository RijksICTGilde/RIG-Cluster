"""Op het cluster gemeten: een gewijzigd geheim bereikt de draaiende pod (RC-119).

Dit is de meting die het plan eist, en het is de enige plek waar de ``subPath``-aanname
zich kan bewijzen. De redenering die eronder ligt:

* de env-vars van een gebruiker komen binnen via een Secret met een VASTE naam
  (``{prefix}-user``, ``envFrom``), en ``envFrom`` wordt alleen bij containerstart
  geinjecteerd;
* een bijlage komt binnen via een Secret met een VASTE naam (``{deployment}-attch-{id}``)
  dat met een ``subPath`` gemount wordt, en zo'n bestand werkt Kubernetes principieel nooit
  bij.

In beide gevallen verandert de Deployment-spec niet als alleen de inhoud verandert, dus
rolt de pod niet en houdt de container wat hij bij zijn start kreeg. De hash-annotatie op
de pod-template maakt de inhoud onderdeel van de spec; dat is wat hieronder gemeten wordt,
in de enige vorm die telt: het bestand IN de container en de env-var IN het proces.

Draaien (heeft een sandbox nodig die op deze commit staat)::

    E2E_BASE_URL=https://zad.sandbox.rijksapp.dev \
    E2E_SECRET_KEY=<SECRET_KEY van de sandbox> \
    uv run pytest tests/e2e/test_sandbox_secret_rollout.py -m "e2e and sandbox" -v -o addopts=""
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx
import pytest
from opi.utils.secret_hash import SECRET_HASH_ANNOTATION
from tests.e2e.conftest import FORGEJO_VERIFY_SSL, SANDBOX_TEST_USER
from tests.e2e.helpers import cluster, sandbox_api
from tests.e2e.helpers.lifecycle import CreatedProject, create_project_via_wizard
from tests.e2e.helpers.wizard import unique_project_name

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Generator

    from playwright.sync_api import BrowserContext
    from tests.e2e.helpers.forgejo import ForgejoClient

pytestmark = [pytest.mark.e2e, pytest.mark.sandbox, pytest.mark.slow, pytest.mark.timeout(1500)]

_VERIFY_SSL = FORGEJO_VERIFY_SSL
_COMPONENT = "web"
_ATTACHMENT_ID = "rc119cert"
_MOUNT_PATH = "/tmp/rc119-attachment.txt"
_ENV_NAME = "RC119_SECRET"
#: Ruim genomen: een uitrol op de drukke Kind-sandbox wacht op ArgoCD.
_TASK_TIMEOUT = 420.0


@pytest.fixture(scope="module")
def project(
    sandbox_context: BrowserContext,
    sandbox_url: str,
    forgejo: ForgejoClient,
) -> Generator[CreatedProject]:
    """Een eigen project met een draaiend component, en het ruimt zichzelf op."""
    page = sandbox_context.new_page()
    created: CreatedProject | None = None
    try:
        created = create_project_via_wizard(
            page,
            sandbox_url,
            forgejo,
            unique_project_name(),
            user_email=SANDBOX_TEST_USER["email"],
            component_name=_COMPONENT,
        )
        yield created
    finally:
        page.close()
        if created is not None:
            sandbox_api.delete_project_via_api(sandbox_url, created.name, created.api_key, verify_ssl=_VERIFY_SSL)


@pytest.fixture(scope="module")
def namespace(project: CreatedProject) -> str:
    namespaces = cluster._project_namespaces(project.name)
    assert namespaces, f"Geen namespace voor project '{project.name}'"
    return namespaces[0]


def _pod(namespace: str, project: CreatedProject) -> str:
    """De naam van de draaiende applicatiepod. Faalt als er geen (of geen enkele) is."""

    def _one() -> bool:
        return len(cluster.running_pod_names(namespace, project.deployment_name)) == 1

    assert cluster.wait_for(_one, timeout=300, interval=5), (
        f"Geen enkele draaiende pod in {namespace} met prefix '{project.deployment_name}': "
        f"{cluster.running_pod_names(namespace, project.deployment_name)}"
    )
    return cluster.running_pod_names(namespace, project.deployment_name)[0]


def _hash(namespace: str) -> str | None:
    return cluster.deployment_pod_annotations(namespace).get(SECRET_HASH_ANNOTATION)


def _upload(project: CreatedProject, sandbox_url: str, content: bytes, *, couple: bool) -> str:
    """(Her)definieer de bijlage; bij ``couple`` ook koppelen aan het component.

    Antwoordt sinds RC-119 met 202 en een task-id: de schrijfactie is klaar, de uitrol
    loopt. Geeft dat task-id terug.
    """
    base = sandbox_url.rstrip("/")
    path = (
        f"/api/v2/projects/{project.name}/services/attachments/component/{_COMPONENT}/attachment"
        if couple
        else f"/api/v2/projects/{project.name}/services/attachments/attachment/{_ATTACHMENT_ID}"
    )
    data = {"attachment_id": _ATTACHMENT_ID, "provide-as": "file", "path": _MOUNT_PATH} if couple else {}
    with httpx.Client(verify=_VERIFY_SSL, timeout=60.0) as client:
        response = client.request(
            "POST" if couple else "PUT",
            f"{base}{path}",
            headers={"X-API-Key": project.api_key},
            data=data,
            files={"file": ("rc119.txt", content, "text/plain")},
        )
    assert response.status_code == 202, f"Verwacht 202 (uitrol als taak), kreeg {response.status_code}: {response.text}"
    task_id = response.headers.get("Location", "").rsplit("/", 1)[-1] or response.json().get("task_id")
    assert task_id, f"Geen task-id in het antwoord: {response.text} / {dict(response.headers)}"
    return task_id


def _wait(project: CreatedProject, sandbox_url: str, task_id: str) -> None:
    sandbox_api.wait_for_task(sandbox_url, task_id, project.api_key, verify_ssl=_VERIFY_SSL, timeout=_TASK_TIMEOUT)


def test_kubectl_is_reachable() -> None:
    if not cluster.kubectl_available():
        pytest.skip("kubectl kan het cluster niet bereiken; deze meting draait op de sandboxmachine")


def test_een_vervangen_bijlage_bereikt_de_draaiende_pod(
    project: CreatedProject, sandbox_url: str, namespace: str
) -> None:
    """Koppelen, meten, vervangen, opnieuw meten -- in een test, want het is een keten.

    Losse tests zouden op volgorde gaan leunen (pytest-randomly schudt), en de tweede helft
    heeft de eerste nodig: je kunt geen bijlage vervangen die nog niet gekoppeld is.
    """
    if not cluster.kubectl_available():
        pytest.skip("kubectl niet beschikbaar")

    _wait(project, sandbox_url, _upload(project, sandbox_url, b"eerste-inhoud\n", couple=True))

    pod_before = _pod(namespace, project)
    assert cluster.wait_for(
        lambda: "eerste-inhoud" in (cluster.read_file_in_pod(namespace, pod_before, _MOUNT_PATH) or ""),
        timeout=180,
        interval=5,
    ), f"Bijlage niet gemount in {pod_before}:{_MOUNT_PATH}"
    hash_before = _hash(namespace)
    assert hash_before, f"Geen {SECRET_HASH_ANNOTATION} op de pod-template in {namespace}"

    _wait(project, sandbox_url, _upload(project, sandbox_url, b"tweede-inhoud\n", couple=False))

    hash_after = _hash(namespace)
    assert hash_after != hash_before, (
        f"De inhoud-hash veranderde niet ({hash_before}); de pod-spec is dan identiek gebleven en Kubernetes rolt niets"
    )
    assert cluster.wait_for(
        lambda: cluster.running_pod_names(namespace, project.deployment_name) not in ([], [pod_before]),
        timeout=300,
        interval=5,
    ), f"Pod {pod_before} is niet vervangen; hash ging van {hash_before} naar {hash_after}"

    pod_after = _pod(namespace, project)
    assert pod_after != pod_before
    # Dit is de kern: een subPath-mount wordt nooit ververst, dus dit kan alleen kloppen als
    # de container opnieuw is gestart.
    assert cluster.wait_for(
        lambda: "tweede-inhoud" in (cluster.read_file_in_pod(namespace, pod_after, _MOUNT_PATH) or ""),
        timeout=180,
        interval=5,
    ), f"De pod draait met de oude inhoud: {cluster.read_file_in_pod(namespace, pod_after, _MOUNT_PATH)!r}"
    logger.info("RC-119 meting bijlage: %s -> %s, hash %s -> %s", pod_before, pod_after, hash_before, hash_after)


def test_een_gewijzigde_env_var_bereikt_de_draaiende_pod(
    project: CreatedProject, sandbox_url: str, namespace: str
) -> None:
    if not cluster.kubectl_available():
        pytest.skip("kubectl niet beschikbaar")
    path = f"/api/v2/projects/{project.name}/services/user-env-vars/values/component/{_COMPONENT}"
    _wait(
        project,
        sandbox_url,
        sandbox_api.start_task(
            sandbox_url, "POST", path, project.api_key, {"values": {_ENV_NAME: "eerste"}}, verify_ssl=_VERIFY_SSL
        ),
    )
    pod_before = _pod(namespace, project)
    assert cluster.wait_for(
        lambda: cluster.env_in_pod(namespace, pod_before, _ENV_NAME) == "eerste", timeout=180, interval=5
    ), f"{_ENV_NAME} staat niet in {pod_before}"
    hash_before = _hash(namespace)

    _wait(
        project,
        sandbox_url,
        sandbox_api.start_task(
            sandbox_url, "POST", path, project.api_key, {"values": {_ENV_NAME: "tweede"}}, verify_ssl=_VERIFY_SSL
        ),
    )

    assert _hash(namespace) != hash_before, "De inhoud-hash veranderde niet bij een gewijzigde env-var"
    assert cluster.wait_for(
        lambda: cluster.running_pod_names(namespace, project.deployment_name) not in ([], [pod_before]),
        timeout=300,
        interval=5,
    ), f"Pod {pod_before} is niet vervangen na een gewijzigde env-var"
    pod_after = _pod(namespace, project)
    assert cluster.wait_for(
        lambda: cluster.env_in_pod(namespace, pod_after, _ENV_NAME) == "tweede", timeout=180, interval=5
    ), f"De pod draait met de oude waarde: {cluster.env_in_pod(namespace, pod_after, _ENV_NAME)!r}"
