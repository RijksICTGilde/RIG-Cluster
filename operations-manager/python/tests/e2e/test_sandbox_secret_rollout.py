"""Op het cluster gemeten: een gewijzigde bijlage of env-var bereikt de draaiende pod.

Wat hier bewaakt wordt is de uitrol, niet het herstartmechanisme. Dat mechanisme bestaat
al: de ArgoCD CMP-plugin hasht elk Secret en elke ConfigMap van een Application en stempelt
dat als ``checksum/config`` op elke pod-template, dus een gewijzigd geheim verandert de
pod-spec en de pod rolt. Dat is op de sandbox nagemeten, ook met de per-component hash uit
de eerste opzet van RC-119 uitgezet: de pod herstartte en de nieuwe inhoud stond erin. Die
tweede hash is daarom niet gebouwd.

Wat ONTBRAK was de aanleiding. De bijlage-routes maakten geen taak en verwerkten het
project niet, dus er werd nooit een nieuw manifest gerenderd, dus zag de plugin nooit een
gewijzigd geheim en bleef de pod op de oude inhoud draaien. Deze test meet dat gat: een
schrijfactie via de API moet eindigen in een container die de nieuwe inhoud heeft.

Waarom er IN de container gekeken wordt en niet naar het Secret: ``envFrom`` wordt alleen
bij containerstart geinjecteerd en een ``subPath``-mount is een eenmalige kopie die
Kubernetes nooit ververst. De waarde in het proces is dus het enige bewijs dat de
container echt opnieuw is gestart met de nieuwe inhoud.

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
from tests.e2e.conftest import FORGEJO_VERIFY_SSL, SANDBOX_TEST_USER
from tests.e2e.helpers import cluster, sandbox_api
from tests.e2e.helpers.cluster import CONFIG_HASH_ANNOTATION
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


def _pods(namespace: str, project: CreatedProject) -> list[str]:
    return cluster.running_pod_names(namespace, project.deployment_name)


def _pod_where(namespace: str, project: CreatedProject, predicate, what: str, *, timeout: float = 420.0) -> str:
    """Wacht tot een DRAAIENDE pod aan *predicate* voldoet en geef zijn naam.

    Bewust niet "de ene pod": tijdens een rollout draaien de oude en de nieuwe even samen,
    en de oude houdt per definitie de oude inhoud. Wie de eerste de beste pod pakt, meet
    dan de pod die juist niet herstart is.
    """
    found: list[str] = []

    def _check() -> bool:
        for pod in _pods(namespace, project):
            if predicate(pod):
                found.append(pod)
                return True
        return False

    assert cluster.wait_for(_check, timeout=timeout, interval=5), (
        f"Geen draaiende pod in {namespace} waarvoor geldt: {what}. Pods nu: {_pods(namespace, project)}"
    )
    return found[-1]


def _hash(namespace: str) -> str | None:
    return cluster.deployment_pod_annotations(namespace).get(CONFIG_HASH_ANNOTATION)


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

    pod_before = _pod_where(
        namespace,
        project,
        lambda pod: "eerste-inhoud" in (cluster.read_file_in_pod(namespace, pod, _MOUNT_PATH, probe="rc119a") or ""),
        f"{_MOUNT_PATH} bevat 'eerste-inhoud'",
    )
    hash_before = _hash(namespace)
    assert hash_before, f"Geen {CONFIG_HASH_ANNOTATION} op de pod-template in {namespace}"
    logger.info("RC-119 bijlage gekoppeld: pod=%s hash=%s", pod_before, hash_before)

    _wait(project, sandbox_url, _upload(project, sandbox_url, b"tweede-inhoud\n", couple=False))

    hash_after = _hash(namespace)
    assert hash_after != hash_before, (
        f"De inhoud-hash veranderde niet ({hash_before}); de pod-spec is dan identiek gebleven en Kubernetes rolt niets"
    )
    # Dit is de kern: een subPath-mount wordt nooit ververst, dus een pod met de nieuwe
    # inhoud kan alleen een NIEUWE pod zijn.
    pod_after = _pod_where(
        namespace,
        project,
        lambda pod: "tweede-inhoud" in (cluster.read_file_in_pod(namespace, pod, _MOUNT_PATH, probe="rc119b") or ""),
        f"{_MOUNT_PATH} bevat 'tweede-inhoud'",
    )
    assert pod_after != pod_before, "Dezelfde pod met nieuwe inhoud kan niet: subPath wordt nooit ververst"
    logger.info("RC-119 bijlage vervangen: %s -> %s, hash %s -> %s", pod_before, pod_after, hash_before, hash_after)


def test_een_gewijzigde_env_var_bereikt_de_draaiende_pod(
    project: CreatedProject, sandbox_url: str, namespace: str
) -> None:
    if not cluster.kubectl_available():
        pytest.skip("kubectl niet beschikbaar")
    path = f"/api/v2/projects/{project.name}/services/user-env-vars/values/component/{_COMPONENT}"

    def _set(method: str, value: str) -> None:
        # POST voegt toe en weigert een naam die er al is; wijzigen is PATCH. Dat verschil
        # is het contract van de values-endpoints, niet iets van deze test.
        _wait(
            project,
            sandbox_url,
            sandbox_api.start_task(
                sandbox_url, method, path, project.api_key, {"values": {_ENV_NAME: value}}, verify_ssl=_VERIFY_SSL
            ),
        )

    _set("POST", "eerste")
    pod_before = _pod_where(
        namespace,
        project,
        lambda pod: cluster.env_in_pod(namespace, pod, _ENV_NAME, probe="rc119c") == "eerste",
        f"{_ENV_NAME}=eerste",
    )
    hash_before = _hash(namespace)
    assert hash_before, f"Geen {CONFIG_HASH_ANNOTATION} op de pod-template in {namespace}"

    _set("PATCH", "tweede")

    assert _hash(namespace) != hash_before, "De inhoud-hash veranderde niet bij een gewijzigde env-var"
    # envFrom wordt alleen bij containerstart geinjecteerd, dus de nieuwe waarde IN het
    # proces bewijst een herstart.
    pod_after = _pod_where(
        namespace,
        project,
        lambda pod: cluster.env_in_pod(namespace, pod, _ENV_NAME, probe="rc119d") == "tweede",
        f"{_ENV_NAME}=tweede",
    )
    assert pod_after != pod_before, "Dezelfde pod met een nieuwe env-var kan niet: envFrom wordt niet herladen"
    logger.info("RC-119 env-var: %s -> %s, hash %s -> %s", pod_before, pod_after, hash_before, _hash(namespace))
