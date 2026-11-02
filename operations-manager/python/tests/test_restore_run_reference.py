"""Een restore naar een nieuwe deployment noemt de backup-run, niet haar onderdelen (RC-404).

Het oude pad schreef de Kopia-snapshots van de run als ``clone-from.backup_items`` in het
projectbestand. Het schema kent dat veld niet en heeft dat nooit gekend, dus de restore
sloeg op met de validatie uit en elke verwerking daarna keurde het bestand af. Deze toetsen
pinnen het nieuwe contract: het bestand noemt de run, de managers halen de onderdelen op.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.core.project_schema import validate_project_schema
from opi.manager.backup.base import SnapshotInfo
from opi.manager.backup.runs import backup_run_items, clone_backup_items
from opi.services import CloneFromType

PROJECT = "tas-hl5"
SOURCE = "productie"
TARGET = "backup"
RUN = "20261030084847"
OTHER_RUN = "20261029084847"


def _project_data() -> dict[str, Any]:
    """Een projectbestand dat tegen het nieuwste schema valideert."""
    return {
        "schema-version": "2.16",
        "name": PROJECT,
        "users": [{"email": "admin@rijksoverheid.nl", "roles": ["admin"]}],
        "clusters": ["local"],
        "repositories": [{"name": "main", "url": "https://example.invalid/app.git"}],
        "components": [{"name": "mono", "type": "single"}],
        "deployments": [
            {
                "name": SOURCE,
                "cluster": "local",
                "namespace": PROJECT,
                "repository": "main",
                "components": [{"reference": "mono", "image": "nginx:1.27"}],
            }
        ],
    }


def _snapshot(resource_type: str, snapshot_id: str, run_id: str = RUN, **extra: Any) -> SnapshotInfo:
    return SnapshotInfo(
        snapshot_id=snapshot_id,
        pvc_name=extra.pop("pvc_name", f"{SOURCE}-mono-data"),
        timestamp=extra.pop("timestamp", "2026-10-30T08:48:47Z"),
        deployment_name=extra.pop("deployment_name", SOURCE),
        component_name="mono",
        resource_type=resource_type,
        backup_run_id=run_id,
        generation=0,
        **extra,
    )


def _run_snapshots() -> list[SnapshotInfo]:
    """De drie onderdelen van run RUN, plus een snapshot van een oudere run."""
    return [
        _snapshot("database", "snap-db", storage_name=f"{SOURCE}-postgresql"),
        _snapshot("bucket", "snap-bucket", storage_name=f"{PROJECT}-{SOURCE}"),
        _snapshot("pvc", "snap-pvc", storage_name="data"),
        _snapshot(
            "database",
            "snap-oud",
            run_id=OTHER_RUN,
            timestamp="2026-10-29T08:48:47Z",
            storage_name=f"{SOURCE}-postgresql",
        ),
    ]


def _restore_deployment() -> dict[str, Any]:
    """De deployment die de restore aanmaakt: hij noemt de run, niet haar onderdelen."""
    return {
        "name": TARGET,
        "cluster": "local",
        "namespace": PROJECT,
        "repository": "main",
        "components": [{"reference": "mono", "image": "nginx:1.27"}],
        "clone-from": {"type": CloneFromType.BACKUP.value, "reference": SOURCE, "mode": "once", "run": RUN},
    }


def _progress() -> MagicMock:
    progress = MagicMock()
    progress.add_task.return_value = "task-1"
    progress.add_subtask.return_value = "subtask-1"
    return progress


@pytest.fixture
def snapshots() -> Any:
    """``BackupManager.list_snapshots`` is de enige bron van de onderdelen van een run."""
    listing = AsyncMock(return_value=_run_snapshots())
    with patch("opi.manager.backup.runs.BackupManager") as manager_cls:
        manager_cls.return_value.list_snapshots = listing
        yield listing


class TestTheSavedProjectFile:
    """Wat ``_create_deployment_from_source`` in het projectbestand zet."""

    @pytest.fixture
    def saved(self) -> Any:
        project_data = _project_data()
        project = MagicMock()
        project.data = project_data
        manager = MagicMock()
        manager.get_contents = AsyncMock(return_value=project_data)
        manager.save_and_commit_project = AsyncMock()
        manager.close = AsyncMock()
        with (
            patch("opi.core.backup_tasks.get_project_store") as store,
            patch("opi.core.backup_tasks.ProjectManager", return_value=manager),
        ):
            store.return_value.get.return_value = project
            yield {"project_data": project_data, "save": manager.save_and_commit_project}

    @pytest.mark.asyncio
    async def test_the_saved_file_names_the_run_and_validates_against_the_schema(self, saved: Any) -> None:
        """De toets die het oude pad had moeten vangen: door de echte schemavalidatie heen."""
        from opi.core.backup_tasks import _create_deployment_from_source

        await _create_deployment_from_source(PROJECT, TARGET, SOURCE, backup_run_id=RUN)

        clone_from = saved["project_data"]["deployments"][1]["clone-from"]
        assert clone_from == {
            "type": CloneFromType.BACKUP.value,
            "reference": SOURCE,
            "mode": "once",
            "run": RUN,
        }
        assert "backup_items" not in clone_from
        validate_project_schema(saved["project_data"])

    @pytest.mark.asyncio
    async def test_the_file_is_saved_with_validation_on(self, saved: Any) -> None:
        """Een ongeldig bestand hoort de restore te stoppen voordat er iets is aangemaakt."""
        from opi.core.backup_tasks import _create_deployment_from_source

        await _create_deployment_from_source(PROJECT, TARGET, SOURCE, backup_run_id=RUN)

        assert saved["save"].await_args.kwargs.get("enforce_validation", True) is True


class TestLookingUpTheRun:
    """``clone-from.run`` naar de onderdelen van die run."""

    @pytest.mark.asyncio
    async def test_only_the_items_of_that_run(self, snapshots: Any) -> None:
        items = await backup_run_items(PROJECT, "local", f"rig-{PROJECT}", SOURCE, RUN)

        assert [(item["resource_type"], item["snapshot_id"]) for item in items] == [
            ("database", "snap-db"),
            ("bucket", "snap-bucket"),
            ("pvc", "snap-pvc"),
        ]

    @pytest.mark.asyncio
    async def test_an_unknown_run_is_an_error_not_an_empty_list(self, snapshots: Any) -> None:
        """Met een lege lijst maakt de restore een lege deployment aan en meldt hij succes."""
        with pytest.raises(ValueError, match="20261030084848") as error:
            await backup_run_items(PROJECT, "local", f"rig-{PROJECT}", SOURCE, "20261030084848")

        assert SOURCE in str(error.value)

    @pytest.mark.asyncio
    async def test_the_run_is_read_in_the_namespace_of_the_source_deployment(self, snapshots: Any) -> None:
        project_data = _project_data()
        deployment = {"name": TARGET, "cluster": "local", "namespace": PROJECT}
        clone_from = {"type": "backup", "reference": SOURCE, "mode": "once", "run": RUN}

        items = await clone_backup_items(project_data, PROJECT, deployment, clone_from)

        assert len(items) == 3
        snapshots.assert_awaited_once_with("local", f"rig-{PROJECT}", project_name=PROJECT)

    @pytest.mark.asyncio
    async def test_a_clone_from_without_a_run_is_refused(self, snapshots: Any) -> None:
        with pytest.raises(ValueError, match="zonder run"):
            await clone_backup_items(
                _project_data(), PROJECT, {"name": TARGET}, {"type": "backup", "reference": SOURCE}
            )

    @pytest.mark.asyncio
    async def test_a_reference_outside_the_project_is_refused(self, snapshots: Any) -> None:
        """Dezelfde guard als de andere kloonpaden: een reference bouwt resourcenamen."""
        with pytest.raises(ValueError, match="not found in project"):
            await clone_backup_items(
                _project_data(),
                PROJECT,
                {"name": TARGET},
                {"type": "backup", "reference": "van-een-ander-project", "run": RUN},
            )


class TestTheItemsReachTheRightManager:
    """De verwerking na het aanmaken: elk onderdeel van de run bij zijn eigen manager."""

    @pytest.mark.asyncio
    async def test_the_database_item_reaches_the_database_restore(self, snapshots: Any) -> None:
        from opi.manager.database_manager import DatabaseManager
        from opi.manager.revision_manager import RevisionManager

        project_data = _project_data()
        deployment = _restore_deployment()
        project_data["deployments"].append(deployment)

        postgres = MagicMock()
        postgres.create_database = AsyncMock(return_value={"status": "created"})
        postgres.create_schema = AsyncMock(return_value={"status": "created"})
        restore = AsyncMock(return_value=MagicMock(success=True, error=None))

        project_manager = MagicMock()
        project_manager._revision_manager = RevisionManager(MagicMock())
        db_manager = DatabaseManager(
            project_manager, db_host="postgres.local", admin_username="postgres", admin_password="postgres"
        )
        db_manager._postgres_connector = postgres

        with patch("opi.manager.backup.database_backup.create_database_backup_manager") as backup_manager:
            backup_manager.return_value.restore_database = restore
            await db_manager._ensure_database_state(
                project_name=PROJECT,
                deployment_name=TARGET,
                deployment=deployment,
                db_database=f"{PROJECT}_{TARGET}",
                db_schema=f"{PROJECT}_{TARGET}",
                db_username=f"{PROJECT}_{TARGET}",
                db_password="geheim",
                project_data=project_data,
                generation=0,
            )

        kwargs = restore.await_args.kwargs
        assert kwargs["snapshot_id"] == "snap-db"
        assert kwargs["reference_name"] == f"{SOURCE}-postgresql"
        assert kwargs["target_database_name"] == f"{PROJECT}_{TARGET}"

    @pytest.mark.asyncio
    async def test_the_bucket_item_reaches_the_bucket_restore(self, snapshots: Any) -> None:
        from opi.manager.minio_manager import MinioManager

        project_data = _project_data()
        deployment = _restore_deployment()
        project_data["deployments"].append(deployment)

        connector = MagicMock()
        connector.configure_alias = AsyncMock(return_value=True)
        connector.create_user = AsyncMock(return_value={"status": "created"})
        connector.create_bucket = AsyncMock(return_value={"status": "created"})
        connector.grant_bucket_access = AsyncMock(return_value={"status": "granted"})
        restore = AsyncMock(return_value=MagicMock(success=True, error=None))

        project_manager = MagicMock()
        project_manager.get_name = AsyncMock(return_value=PROJECT)
        project_manager.get_progress_manager.return_value = None
        minio_manager = MinioManager(project_manager)

        with (
            patch("opi.manager.minio_manager.create_minio_connector", lambda: connector),
            patch.object(MinioManager, "_deployment_uses_minio", AsyncMock(return_value=True)),
            patch.object(MinioManager, "_get_existing_minio_credentials_from_k8s", AsyncMock(return_value=None)),
            patch.object(MinioManager, "_apply_bucket_versioning", AsyncMock()),
            patch.object(MinioManager, "_apply_noncurrent_version_limit", AsyncMock()),
            patch.object(MinioManager, "_get_deployment_bucket_generation", return_value=None),
            patch("opi.manager.minio_manager.get_minio_host", lambda cluster: "minio"),
            patch("opi.manager.minio_manager.get_minio_port", lambda cluster: 9000),
            patch("opi.manager.backup.bucket_backup.create_bucket_backup_manager") as backup_manager,
        ):
            backup_manager.return_value.restore_bucket = restore
            await minio_manager.create_resources_for_deployment(project_data, deployment)

        kwargs = restore.await_args.kwargs
        assert kwargs["snapshot_id"] == "snap-bucket"
        assert kwargs["reference_name"] == f"{PROJECT}-{SOURCE}"
        assert kwargs["target_bucket_name"] == f"{PROJECT}-{TARGET}"

    @pytest.mark.asyncio
    async def test_a_completed_once_clone_does_not_look_the_run_up_again(self, snapshots: Any) -> None:
        """De bucket werd bij elke verwerking opnieuw teruggezet: de poort op
        ``status.completed`` stond wel in de kloonlogica, maar niet bij het terugzetten."""
        from opi.manager.minio_manager import MinioManager

        project_data = _project_data()
        deployment = _restore_deployment()
        deployment["clone-from"]["status"] = {"completed": True, "timestamp": "2026-10-30T09:00:00+00:00"}
        project_data["deployments"].append(deployment)

        connector = MagicMock()
        connector.configure_alias = AsyncMock(return_value=True)
        connector.create_user = AsyncMock(return_value={"status": "created"})
        connector.create_bucket = AsyncMock(return_value={"status": "created"})
        connector.grant_bucket_access = AsyncMock(return_value={"status": "granted"})
        restore = AsyncMock()

        project_manager = MagicMock()
        project_manager.get_name = AsyncMock(return_value=PROJECT)
        project_manager.get_progress_manager.return_value = None
        minio_manager = MinioManager(project_manager)

        with (
            patch("opi.manager.minio_manager.create_minio_connector", lambda: connector),
            patch.object(MinioManager, "_deployment_uses_minio", AsyncMock(return_value=True)),
            patch.object(MinioManager, "_get_existing_minio_credentials_from_k8s", AsyncMock(return_value=None)),
            patch.object(MinioManager, "_apply_bucket_versioning", AsyncMock()),
            patch.object(MinioManager, "_apply_noncurrent_version_limit", AsyncMock()),
            patch.object(MinioManager, "_get_deployment_bucket_generation", return_value=None),
            patch("opi.manager.minio_manager.get_minio_host", lambda cluster: "minio"),
            patch("opi.manager.minio_manager.get_minio_port", lambda cluster: 9000),
            patch("opi.manager.backup.bucket_backup.create_bucket_backup_manager") as backup_manager,
        ):
            backup_manager.return_value.restore_bucket = restore
            await minio_manager.create_resources_for_deployment(project_data, deployment)

        restore.assert_not_awaited()
        snapshots.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_pvc_items_reach_the_pvc_pre_restore(self) -> None:
        """De PVC's gaan voor de verwerking, dus ArgoCD neemt ze gevuld over."""
        from opi.core.task_handlers_backup import handle_restore

        payload = {
            "project_name": PROJECT,
            "backup_run_id": RUN,
            "target_deployment": TARGET,
            "create_new_deployment": True,
            "source_deployment": SOURCE,
            "backup_items": [
                {"resource_type": "database", "snapshot_id": "snap-db"},
                {"resource_type": "pvc", "snapshot_id": "snap-pvc", "component_name": "mono", "storage_name": "data"},
            ],
        }
        _thb = "opi.core.task_handlers_backup"
        with (
            patch(f"{_thb}._create_deployment_from_source", AsyncMock()) as create,
            patch(f"{_thb}._pre_restore_pvcs", AsyncMock()) as pre_restore,
            patch(f"{_thb}._provision_deployment_infrastructure", AsyncMock()),
        ):
            result = await handle_restore(payload, _progress())

        assert result["success"] is True
        create.assert_awaited_once()
        assert create.await_args.kwargs["backup_run_id"] == RUN
        assert pre_restore.await_args.kwargs["pvc_items"] == [payload["backup_items"][1]]


class TestAnUnknownRunCreatesNothing:
    @pytest.mark.asyncio
    async def test_a_restore_without_a_matching_run_creates_no_deployment(self) -> None:
        """Een lege run is schemageldig, dus de handler moet hem weigeren voor hij iets aanmaakt."""
        from opi.core.task_handlers_backup import handle_restore

        payload = {
            "project_name": PROJECT,
            "backup_run_id": "",
            "target_deployment": TARGET,
            "create_new_deployment": True,
            "source_deployment": "",
            "backup_items": [],
        }
        _thb = "opi.core.task_handlers_backup"
        with (
            patch(f"{_thb}._create_deployment_from_source", AsyncMock()) as create,
            patch(f"{_thb}._pre_restore_pvcs", AsyncMock()) as pre_restore,
            patch(f"{_thb}._provision_deployment_infrastructure", AsyncMock()) as provision,
        ):
            result = await handle_restore(payload, _progress())

        assert result["success"] is False
        create.assert_not_awaited()
        pre_restore.assert_not_awaited()
        provision.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_empty_clone_from_run_is_schema_valid(self) -> None:
        """Waarom de handler die poort nodig heeft: het schema houdt een lege run niet tegen."""
        project_data = _project_data()
        project_data["deployments"].append(
            {
                "name": TARGET,
                "cluster": "local",
                "namespace": PROJECT,
                "repository": "main",
                "components": [{"reference": "mono", "image": "nginx:1.27"}],
                "clone-from": {"type": CloneFromType.BACKUP.value, "reference": "", "mode": "once", "run": ""},
            }
        )

        validate_project_schema(project_data)

    @pytest.mark.asyncio
    async def test_the_database_path_stops_on_a_run_that_is_gone(self, snapshots: Any) -> None:
        from opi.manager.database_manager import DatabaseManager
        from opi.manager.revision_manager import RevisionManager

        project_data = _project_data()
        deployment = _restore_deployment()
        deployment["clone-from"]["run"] = "20261030084848"
        project_data["deployments"].append(deployment)

        postgres = MagicMock()
        postgres.create_database = AsyncMock(return_value={"status": "created"})
        postgres.create_schema = AsyncMock(return_value={"status": "created"})

        project_manager = MagicMock()
        project_manager._revision_manager = RevisionManager(MagicMock())
        db_manager = DatabaseManager(
            project_manager, db_host="postgres.local", admin_username="postgres", admin_password="postgres"
        )
        db_manager._postgres_connector = postgres

        with pytest.raises(ValueError, match="20261030084848"):
            await db_manager._ensure_database_state(
                project_name=PROJECT,
                deployment_name=TARGET,
                deployment=deployment,
                db_database=f"{PROJECT}_{TARGET}",
                db_schema=f"{PROJECT}_{TARGET}",
                db_username=f"{PROJECT}_{TARGET}",
                db_password="geheim",
                project_data=project_data,
                generation=0,
            )

        postgres.create_database.assert_not_awaited()


class TestTheWizardListsTheSameRuns:
    """De groepering is naar ``opi/manager/backup/runs.py`` verhuisd, niet veranderd."""

    @pytest.mark.asyncio
    async def test_the_restore_wizard_gets_one_entry_per_run(self) -> None:
        from opi.web.router_detail_edit import _gather_backup_runs_async

        with patch("opi.manager.backup.BackupManager") as manager_cls:
            manager_cls.return_value.list_snapshots = AsyncMock(return_value=_run_snapshots())
            runs = await _gather_backup_runs_async(PROJECT, _project_data(), "local")

        assert [run["backup_run_id"] for run in runs] == [RUN, OTHER_RUN]
        run = runs[0]
        assert run["deployment_name"] == SOURCE
        assert run["resource_count"] == 3
        assert run["resource_types"] == ["database", "bucket", "pvc"]
        assert run["items"][0] == {
            "snapshot_id": "snap-db",
            "resource_type": "database",
            "component_name": "mono",
            "storage_name": f"{SOURCE}-postgresql",
            "reference_name": f"{SOURCE}-postgresql",
            "generation": 0,
        }
