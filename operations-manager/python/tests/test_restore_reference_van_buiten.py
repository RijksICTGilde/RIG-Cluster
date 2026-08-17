"""De naam die de leesendpoints publiceren is de naam die de restore accepteert.

Vraag 10 uit `plans/vragen-uit-zad-cli.md`: de zad-cli las `backup` uit
``GET /api/v1/backup/runs/...`` en uit ``GET /api/v1/restore/snapshots/...``, en
kreeg op precies die naam een 404 van ``POST /api/v1/restore/database/...``.

De oorzaak zit in de vertaling van een Kopia-snapshot naar een naam. Een database-
of bucketsnapshot draagt geen ``pvc``-tag, dus viel ``pvc_name`` terug op het laatste
stuk van het bronpad: ``/tmp/backup`` bij een databasedump en ``/tmp/bucket-backup``
bij een gespiegelde bucket. Beide lijsten publiceerden die mapnaam, terwijl de
restore-route de referentie wil waaronder de backup geregistreerd staat
(``{deployment}-postgresql``, ``{deployment}-minio``).

Wat hier vastligt:

* welke naam een snapshot van elk soort publiceert;
* dat de lijst die beide leesendpoints voedt die naam doorgeeft;
* dat de gepubliceerde naam door de restore-route herkend wordt;
* dat de 404 zegt welke namen er dan wel zijn, zonder te gokken.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opi.api.restore_router import _known_reference_names, restore_router
from opi.connectors.kopia import KopiaSnapshot
from opi.manager.backup.pvc_backup import PVCBackupManager

API_KEY = "test-restore-api-key"
PROJECT = "restore-test"
NAMESPACE = f"rig-{PROJECT}"


def _snapshot(source_path: str, tags: dict[str, str]) -> KopiaSnapshot:
    return KopiaSnapshot(
        snapshot_id="k123",
        source_path=source_path,
        timestamp="2026-08-13T10:41:14Z",
        size_bytes=2307,
        tags=tags,
    )


DATABASE_SNAPSHOT = _snapshot(
    "/tmp/backup",
    {"tag:resource_type": "database", "tag:database": "productie-postgresql", "tag:component": "web"},
)
BUCKET_SNAPSHOT = _snapshot(
    "/tmp/bucket-backup",
    {"tag:resource_type": "bucket", "tag:bucket": "productie-minio", "tag:component": "web"},
)
PVC_SNAPSHOT = _snapshot(
    "/data",
    {
        "tag:resource_type": "pvc",
        "tag:pvc": "productie-web-data-v0",
        "tag:storage": "data",
        "tag:component": "web",
    },
)


class TestRestoreReferenceVanEenSnapshot:
    def test_een_databasesnapshot_publiceert_zijn_referentie_en_niet_het_bronpad(self) -> None:
        assert DATABASE_SNAPSHOT.pvc_name == "backup"  # de oude, onbruikbare waarde
        assert DATABASE_SNAPSHOT.restore_reference == "productie-postgresql"

    def test_een_bucketsnapshot_publiceert_zijn_referentie_en_niet_het_bronpad(self) -> None:
        assert BUCKET_SNAPSHOT.pvc_name == "bucket-backup"
        assert BUCKET_SNAPSHOT.restore_reference == "productie-minio"

    def test_een_pvc_houdt_zijn_pvc_naam(self) -> None:
        # De PVC-restore neemt de PVC-naam in het pad, niet de logische opslagnaam.
        assert PVC_SNAPSHOT.restore_reference == "productie-web-data-v0"

    def test_zonder_bruikbare_tag_blijft_de_oude_waarde_over(self) -> None:
        # Een oud snapshot zonder database-tag levert nog steeds iets op in plaats
        # van een lege naam; het is dan hooguit even onvindbaar als voorheen.
        naamloos = _snapshot("/tmp/backup", {"tag:resource_type": "database"})
        assert naamloos.restore_reference == "backup"


class TestDeLijstDieBeideLeesendpuntenVoedt:
    """``PVCBackupManager.list_snapshots`` voedt zowel backup-runs als de snapshotlijst."""

    @pytest.mark.asyncio
    async def test_de_lijst_geeft_de_restore_referentie_door(self) -> None:
        manager = PVCBackupManager()
        kopia = MagicMock()
        kopia.list_snapshots = AsyncMock(return_value=[DATABASE_SNAPSHOT, BUCKET_SNAPSHOT, PVC_SNAPSHOT])

        with (
            patch.object(PVCBackupManager, "_derive_backup_key", AsyncMock(return_value="pw")),
            patch("opi.connectors.kopia.KopiaConnector", return_value=kopia),
            patch("opi.connectors.kopia.KopiaConnector.is_kopia_available", True),
        ):
            snapshots = await manager.list_snapshots("sandboxed-local", NAMESPACE, project_name=PROJECT)

        assert [s.pvc_name for s in snapshots] == [
            "productie-postgresql",
            "productie-minio",
            "productie-web-data-v0",
        ]

    @pytest.mark.asyncio
    async def test_filteren_gebeurt_op_diezelfde_naam(self) -> None:
        manager = PVCBackupManager()
        kopia = MagicMock()
        kopia.list_snapshots = AsyncMock(return_value=[DATABASE_SNAPSHOT, BUCKET_SNAPSHOT])

        with (
            patch.object(PVCBackupManager, "_derive_backup_key", AsyncMock(return_value="pw")),
            patch("opi.connectors.kopia.KopiaConnector", return_value=kopia),
            patch("opi.connectors.kopia.KopiaConnector.is_kopia_available", True),
        ):
            snapshots = await manager.list_snapshots(
                "sandboxed-local", NAMESPACE, "productie-postgresql", project_name=PROJECT
            )

        assert [s.pvc_name for s in snapshots] == ["productie-postgresql"]


def _project_file() -> dict[str, Any]:
    """Een project met een deployment `productie` en een component dat beide diensten gebruikt."""
    return {
        "schema-version": 2,
        "name": PROJECT,
        "users": [{"email": "admin@example.com", "role": "admin"}],
        "clusters": ["local"],
        "components": [{"name": "web", "type": "single", "services": ["postgresql-database", "minio-storage"]}],
        "deployments": [
            {
                "name": "productie",
                "cluster": "local",
                "namespace": PROJECT,
                "components": [{"reference": "web"}],
            }
        ],
    }


class _FakeStore:
    def __init__(self, data: dict[str, Any]) -> None:
        project = MagicMock()
        project.name = PROJECT
        project.api_key = API_KEY
        project.filename = f"{PROJECT}.yaml"
        project.data = data
        self._project = project

    def get(self, project_name: str) -> Any:
        return self._project if project_name == PROJECT else None


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(restore_router)
    return TestClient(app)


@pytest.fixture
def mock_store() -> Any:
    store = _FakeStore(_project_file())
    with (
        patch("opi.api.endpoint_util.get_project_store", return_value=store),
        patch("opi.api.restore_router.get_project_store", return_value=store),
    ):
        yield store


class TestDeGepubliceerdeNaamWordtHerkend:
    def test_de_naam_uit_de_lijst_is_de_naam_die_de_route_kent(self) -> None:
        # Wat de lijst publiceert voor de snapshots van dit project ...
        gepubliceerd = [DATABASE_SNAPSHOT.restore_reference, BUCKET_SNAPSHOT.restore_reference]
        # ... staat in wat de route accepteert. Dit is de poort die drie rondes lang open stond:
        # de twee kanten werden apart getoetst en nooit tegen elkaar.
        accepteert = _known_reference_names(
            _project_file(), ["postgresql-database", "namespace-postgresql-database"], "database"
        ) + _known_reference_names(_project_file(), ["minio-storage"], "minio")
        assert gepubliceerd == accepteert

    def test_zonder_component_met_de_dienst_geldt_de_deployment_brede_terugval(self) -> None:
        data = _project_file()
        data["components"][0]["services"] = ["publish-on-web"]
        assert _known_reference_names(data, ["minio-storage"], "minio") == ["productie-minio"]


class TestDe404NoemtDeNamenDieErWelZijn:
    def test_database(self, client: TestClient, mock_store: Any) -> None:
        response = client.post(
            f"/api/v1/restore/database/local/{NAMESPACE}/backup?project_name={PROJECT}",
            headers={"X-API-Key": API_KEY},
            json={},
        )

        assert response.status_code == 404, response.text
        detail = response.json()["detail"]
        assert "'backup'" in detail
        assert "'productie-postgresql'" in detail

    def test_bucket(self, client: TestClient, mock_store: Any) -> None:
        response = client.post(
            f"/api/v1/restore/bucket/local/{NAMESPACE}/bucket-backup?project_name={PROJECT}",
            headers={"X-API-Key": API_KEY},
            json={},
        )

        assert response.status_code == 404, response.text
        detail = response.json()["detail"]
        assert "'productie-minio'" in detail

    def test_een_project_zonder_backups_van_dat_soort_zegt_dat(self, client: TestClient, mock_store: Any) -> None:
        mock_store._project.data["deployments"] = []

        response = client.post(
            f"/api/v1/restore/database/local/{NAMESPACE}/backup?project_name={PROJECT}",
            headers={"X-API-Key": API_KEY},
            json={},
        )

        assert response.status_code == 404, response.text
        assert "no database backups registered" in response.json()["detail"].lower()
