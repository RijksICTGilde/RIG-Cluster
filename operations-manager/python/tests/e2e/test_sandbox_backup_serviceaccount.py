"""
Sandbox E2E: backup- en restorepods van een PVC draaien op de serviceaccount van het project (RC-207).

Dat de podspec het veld draagt toetst ``test_backup_pod_service_account.py``. Hier draaien een
PVC-backup en beide PVC-restorewegen (via de backup-run en via ``/restore/pvc/...``) echt onder
die serviceaccount. De database- en bucketpods lopen door ``test_sandbox_restore_van_buiten.py``.

Vereist een draaiende sandbox met JOUW build, E2E_BASE_URL en kubectl-toegang.
Draaien met:

    E2E_BASE_URL=https://zad.sandbox.rijksapp.dev \
    E2E_SECRET_KEY=<SECRET_KEY van de sandbox> \
    uv run pytest tests/e2e/test_sandbox_backup_serviceaccount.py -m "e2e and sandbox" \
      -o addopts="" -v -s
"""

from __future__ import annotations

import logging
import subprocess
import threading
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from tests.e2e.conftest import FORGEJO_VERIFY_SSL, SANDBOX_TEST_USER
from tests.e2e.helpers import cluster, sandbox_api
from tests.e2e.helpers.lifecycle import CreatedProject, create_project_with_services
from tests.e2e.helpers.wizard import unique_project_name

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Generator

    from playwright.sync_api import BrowserContext
    from tests.e2e.helpers.forgejo import ForgejoClient

pytestmark = [pytest.mark.e2e, pytest.mark.sandbox]

_VERIFY_SSL = FORGEJO_VERIFY_SSL
_CLUSTER = "sandboxed-local"
_SERVICES = ["persistent-storage"]


@pytest.fixture(scope="module")
def storage_project(
    sandbox_context: BrowserContext,
    sandbox_url: str,
    forgejo: ForgejoClient,
) -> Generator[CreatedProject]:
    display_name = unique_project_name(prefix="sa")
    page = sandbox_context.new_page()
    created: CreatedProject | None = None
    try:
        created = create_project_with_services(
            page,
            sandbox_url,
            forgejo,
            display_name,
            user_email=SANDBOX_TEST_USER["email"],
            services=_SERVICES,
            # Gemeten op de sandbox: een project met opslag deed er 274 s over.
            create_timeout=480.0,
        )
        logger.info("Project met opslag: %s (deployment %s)", created.name, created.deployment_name)
        yield created
    finally:
        page.close()
        if created is not None:
            sandbox_api.delete_project_via_api(sandbox_url, created.name, created.api_key, verify_ssl=_VERIFY_SSL)


def _api(sandbox_url: str, method: str, path: str, api_key: str, **kwargs: Any) -> httpx.Response:
    with httpx.Client(verify=_VERIFY_SSL, timeout=900.0) as client:
        return client.request(
            method,
            f"{sandbox_url.rstrip('/')}{path}",
            headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            **kwargs,
        )


class _BackupPodWatcher:
    """Houdt bij onder welke serviceaccount elke backup- en restorepod in de namespace draait.

    De managers ruimen hun pods op, dus achteraf is er niets meer te zien.
    """

    def __init__(self, namespace: str) -> None:
        self.namespace = namespace
        self.seen: dict[str, str] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._poll, daemon=True)

    def _poll(self) -> None:
        while not self._stop.is_set():
            result = subprocess.run(
                [
                    "kubectl",
                    "get",
                    "pods",
                    "-n",
                    self.namespace,
                    "-l",
                    "app.kubernetes.io/managed-by=opi-backup",
                    "-o",
                    "jsonpath={range .items[*]}{.metadata.name} {.spec.serviceAccountName}{'\\n'}{end}",
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            for line in result.stdout.splitlines():
                name, _, account = line.partition(" ")
                self.seen[name] = account
            self._stop.wait(1.0)

    def __enter__(self) -> _BackupPodWatcher:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join(timeout=60)


@pytest.mark.timeout(1800)
def test_pvc_backup_en_restores_draaien_op_de_projectserviceaccount(
    storage_project: CreatedProject, sandbox_url: str
) -> None:
    if not cluster.kubectl_available():
        pytest.skip("kubectl niet beschikbaar; deze suite kijkt naar de pods")

    project = storage_project.name
    deployment = storage_project.deployment_name
    key = storage_project.api_key
    namespace = f"rig-{project}"

    with _BackupPodWatcher(namespace) as watcher:
        backup = _api(
            sandbox_url,
            "POST",
            f"/api/v1/backup/project/{project}/deployment/{deployment}",
            key,
            json={"resource_types": ["pvc"]},
        )
        logger.info("BACKUP %s %s", backup.status_code, backup.text[:1000])
        assert backup.status_code == 200, backup.text

        runs = _api(sandbox_url, "GET", f"/api/v1/backup/runs/{project}/{deployment}", key)
        assert runs.status_code == 200, runs.text
        run_id = max(runs.json()["runs"], key=lambda r: r.get("timestamp", ""))["backup_run_id"]

        project_restore = _api(
            sandbox_url,
            "POST",
            f"/api/v1/restore/project/{project}/deployment/{deployment}/run/{run_id}",
            key,
            json={},
        )
        logger.info("RESTORE RUN %s %s", project_restore.status_code, project_restore.text[:1000])
        assert project_restore.status_code == 200, project_restore.text
        assert project_restore.json()["status"] == "success", project_restore.text

        snapshots = _api(
            sandbox_url,
            "GET",
            f"/api/v1/restore/snapshots/{_CLUSTER}/{namespace}?project_name={project}",
            key,
        )
        assert snapshots.status_code == 200, snapshots.text
        snapshot = snapshots.json()["snapshots"][0]

        # Zonder opslagklasse kiest de restore de standaardklasse; is die WaitForFirstConsumer
        # (de sandbox), dan wacht de restore op een binding die pas na zijn eigen pod komt.
        storage_class = subprocess.run(
            ["kubectl", "get", "pvc", "-n", namespace, "-o", "jsonpath={.items[0].spec.storageClassName}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout

        namespace_restore = _api(
            sandbox_url,
            "POST",
            f"/api/v1/restore/pvc/{_CLUSTER}/{namespace}/{snapshot['pvc_name']}?project_name={project}",
            key,
            json={"snapshot_id": snapshot["snapshot_id"], "storage_size": "1Gi", "storage_class": storage_class},
        )
        logger.info("RESTORE PVC %s %s", namespace_restore.status_code, namespace_restore.text[:1000])
        assert namespace_restore.status_code == 200, namespace_restore.text

    logger.info("PODS %s", watcher.seen)
    assert any(name.startswith("backup-") for name in watcher.seen), watcher.seen
    assert sum(name.startswith("restore-") for name in watcher.seen) >= 2, watcher.seen
    assert set(watcher.seen.values()) == {f"{project}-sa"}, watcher.seen
