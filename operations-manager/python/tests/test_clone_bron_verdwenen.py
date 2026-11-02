"""Een afgeronde ``mode: once`` kloon blijft provisionen nadat de bron verdween (RC-361).

Dit is de vorm van het productieproject wies: een deployment met ``clone-from`` van
type ``deployment``, reference ``staging``, mode ``once``, waarvan de kloon al gedraaid
heeft en waarvan de bron-deployment inmiddels uit het projectbestand is verdwenen. De
reference-toets uit RC-361 staat in alle drie de provisioningpaden na de
overslaan-logica, dus zo'n project mag niet op die toets stranden.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from opi.generation.manifests import ManifestGenerator
from opi.manager.database_manager import DatabaseManager
from opi.manager.minio_manager import MinioManager
from opi.manager.pvc_manager import PVCManager
from opi.manager.revision_manager import RevisionManager

AFGERONDE_KLOON = {
    "type": "deployment",
    "reference": "staging",
    "mode": "once",
    "status": {"completed": True, "timestamp": "2026-05-01T10:00:00+00:00"},
}


def _project_zonder_bron() -> dict[str, Any]:
    """Het projectbestand nadat de bron-deployment 'staging' verwijderd is."""
    return {
        "name": "wies",
        "deployments": [
            {
                "name": "productie",
                "cluster": "local",
                "namespace": "wies",
                "components": [{"reference": "web", "image": "nginx:latest"}],
                "clone-from": dict(AFGERONDE_KLOON),
            }
        ],
    }


@pytest.mark.asyncio
async def test_het_databasepad_provisioneert_zonder_de_verdwenen_bron() -> None:
    project_data = _project_zonder_bron()
    postgres = MagicMock()
    postgres.create_database = AsyncMock(return_value={"status": "exists"})
    postgres.create_schema = AsyncMock(return_value={"status": "exists"})
    postgres.clone_schema = AsyncMock()
    postgres.execute_init_sql = AsyncMock(return_value={"status": "success"})

    project_manager = MagicMock()
    project_manager._revision_manager = RevisionManager(MagicMock())
    db_manager = DatabaseManager(
        project_manager,
        db_host="localhost",
        admin_username="postgres",
        admin_password="postgres",
    )
    db_manager._postgres_connector = postgres

    result = await db_manager._ensure_database_state(
        project_name="wies",
        deployment_name="productie",
        deployment=project_data["deployments"][0],
        db_database="wies_productie",
        db_schema="wies_productie",
        db_username="wies_productie",
        db_password="secret",
        project_data=project_data,
        force_clone_override=False,
        generation=None,
    )

    assert result.database == "wies_productie"
    postgres.clone_schema.assert_not_called()


@pytest.mark.asyncio
async def test_het_bucketpad_provisioneert_zonder_de_verdwenen_bron() -> None:
    project_data = _project_zonder_bron()
    deployment = project_data["deployments"][0]

    connector = MagicMock()
    connector.configure_alias = AsyncMock(return_value=True)
    connector.create_user = AsyncMock(return_value={"status": "created"})
    connector.create_bucket = AsyncMock(return_value={"status": "created"})
    connector.grant_bucket_access = AsyncMock(return_value={"status": "granted"})

    project_manager = MagicMock()
    project_manager.get_name = AsyncMock(return_value="wies")
    project_manager.get_progress_manager.return_value = None
    minio_manager = MinioManager(project_manager)

    with (
        patch("opi.manager.minio_manager.create_minio_connector", lambda: connector),
        patch.object(MinioManager, "_deployment_uses_minio", AsyncMock(return_value=True)),
        patch.object(MinioManager, "_get_existing_minio_credentials_from_k8s", AsyncMock(return_value=None)),
        patch.object(MinioManager, "_apply_bucket_versioning", AsyncMock()),
        patch.object(MinioManager, "_apply_noncurrent_version_limit", AsyncMock()),
        patch.object(MinioManager, "_get_deployment_bucket_generation", return_value=None),
        patch.object(MinioManager, "clone_minio_from_deployment", AsyncMock()) as kloon,
        patch("opi.manager.minio_manager.get_minio_host", lambda cluster: "minio"),
        patch("opi.manager.minio_manager.get_minio_port", lambda cluster: 9000),
    ):
        await minio_manager.create_resources_for_deployment(project_data, deployment)

    kloon.assert_not_awaited()
    connector.create_bucket.assert_awaited_once()
    assert connector.create_bucket.await_args.args[1] == "wies-productie"


@pytest.mark.asyncio
async def test_het_volumepad_provisioneert_zonder_de_verdwenen_bron(tmp_path) -> None:
    project_data = _project_zonder_bron()
    project_manager = MagicMock()
    project_manager._project_file_handler.get_storage_generation.return_value = 0
    project_manager._project_file_handler.get_storage_backup_enabled.return_value = False
    pvc_manager = PVCManager(project_manager)

    with (
        patch("opi.core.database_pools.get_database_pool"),
        patch("opi.services.marked_for_deletion_service.MarkedForDeletionService") as service,
    ):
        service.return_value.get_marks_for_deployment = AsyncMock(return_value=[])
        service.return_value.delete_mark = AsyncMock()
        created = await pvc_manager.create_pvc_manifests_for_component(
            project_data=project_data,
            deployment=project_data["deployments"][0],
            component_name="web",
            unique_name="productie-web",
            persistent_storage=[{"name": "data", "size": "1Gi"}],
            namespace="wies",
            cluster="local",
            full_output_dir=str(tmp_path),
            manifest_generator=ManifestGenerator(),
        )

    assert created
    manifest = yaml.safe_load((tmp_path / "web-data-pvc.yaml").read_text())
    assert "dataSource" not in manifest["spec"]
