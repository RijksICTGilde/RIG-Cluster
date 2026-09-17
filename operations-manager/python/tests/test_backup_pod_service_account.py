"""Backup- en restorepods draaien op de serviceaccount van het project, getoetst op het manifest dat de manager toepast."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from jinja2 import Environment, StrictUndefined
from opi.manager.backup.base import BackupConfig, BaseBackupManager
from opi.manager.backup.bucket_backup import BucketBackupManager
from opi.manager.backup.database_backup import DatabaseBackupManager
from opi.manager.backup.pvc_backup import PVCBackupManager

PROJECT = "amt"
SERVICE_ACCOUNT = "amt-sa"

_DB = {
    "database_host": "pg",
    "database_port": 5432,
    "database_name": "amt_prod",
    "database_user": "amt",
    "database_password": "pw",
}
_BUCKET_SOURCE = {
    "source_minio_endpoint": "http://minio:9000",
    "source_bucket_name": "amt-prod",
    "source_access_key": "ak",
    "source_secret_key": "sk",
}
_BUCKET_TARGET = {
    "target_minio_endpoint": "http://minio:9000",
    "target_bucket_name": "amt-prod",
    "target_access_key": "ak",
    "target_secret_key": "sk",
}
_COMMON = {"namespace": "rig-amt", "pod_name": "pod", "kopia_password": "kp", "backup_prefix": "local/rig-amt"}


def _manager[M: BaseBackupManager](cls: type[M]) -> M:
    kubectl = MagicMock()
    kubectl.run_command = AsyncMock(return_value=("", "", 0))
    kubectl.template_manifest = lambda content, variables: (
        Environment(undefined=StrictUndefined).from_string(content).render(**variables)
    )
    with patch("opi.manager.backup.base.KubectlConnector", return_value=kubectl):
        return cls(BackupConfig(s3_endpoint="http://minio:9000", s3_bucket="b", s3_access_key="a", s3_secret_key="s"))


def _applied_pod(manager: BaseBackupManager) -> dict[str, Any]:
    return yaml.safe_load(manager.kubectl.run_command.await_args.kwargs["stdin_input"])


type PodCreator = Callable[[Any, str | None], Awaitable[None]]

POD_CREATORS: dict[str, tuple[type[BaseBackupManager], PodCreator]] = {
    "pvc-backup": (
        PVCBackupManager,
        lambda m, project: m._create_backup_pod(
            **_COMMON,
            pvc_name="data",
            clone_pvc_name="data-clone",
            timestamp="t",
            backup_run_id="r",
            project_name=project,
        ),
    ),
    "pvc-restore": (
        PVCBackupManager,
        lambda m, project: m._create_restore_pod(
            **_COMMON, pvc_name="data", target_pvc_name="data-restored", project_name=project
        ),
    ),
    "database-backup": (
        DatabaseBackupManager,
        lambda m, project: m._create_database_backup_pod(
            **_COMMON, **_DB, reference_name="db", source_type="shared", timestamp="t", project_name=project
        ),
    ),
    "database-restore": (
        DatabaseBackupManager,
        lambda m, project: m._create_database_restore_pod(
            **_COMMON,
            reference_name="db",
            target_database_host="pg",
            target_database_port=5432,
            target_database_name="amt_prod",
            target_database_user="amt",
            target_database_password="pw",
            project_name=project,
        ),
    ),
    "bucket-backup": (
        BucketBackupManager,
        lambda m, project: m._create_bucket_backup_pod(
            **_COMMON,
            **_BUCKET_SOURCE,
            reference_name="bucket",
            source_type="shared",
            timestamp="t",
            backup_run_id="r",
            project_name=project,
        ),
    ),
    "bucket-mirror": (
        BucketBackupManager,
        lambda m, project: m._create_bucket_mirror_pod(
            namespace="rig-amt",
            pod_name="pod",
            **_BUCKET_SOURCE,
            reference_name="bucket",
            source_type="shared",
            backup_path="p",
            timestamp="t",
            backup_run_id="r",
            project_name=project,
        ),
    ),
    "bucket-restore": (
        BucketBackupManager,
        lambda m, project: m._create_bucket_restore_pod(
            **_COMMON, **_BUCKET_TARGET, reference_name="bucket", project_name=project
        ),
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", POD_CREATORS)
async def test_the_applied_pod_runs_under_the_project_service_account(kind: str) -> None:
    cls, create = POD_CREATORS[kind]
    manager = _manager(cls)

    await create(manager, PROJECT)

    assert _applied_pod(manager)["spec"]["serviceAccountName"] == SERVICE_ACCOUNT


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", POD_CREATORS)
async def test_without_a_project_no_pod_is_applied(kind: str) -> None:
    cls, create = POD_CREATORS[kind]
    manager = _manager(cls)

    with pytest.raises(ValueError, match="service account"):
        await create(manager, None)

    manager.kubectl.run_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_namespace_restore_passes_its_project_and_cluster_to_the_pod() -> None:
    manager = _manager(PVCBackupManager)
    manager._get_pvc_info = AsyncMock(return_value={"name": "data"})  # type: ignore[method-assign]
    manager._derive_backup_key = AsyncMock(return_value="kp")  # type: ignore[method-assign]
    manager._wait_for_pod = AsyncMock(return_value=True)  # type: ignore[method-assign]
    manager._cleanup_pod = AsyncMock()  # type: ignore[method-assign]

    result = await manager._restore_pvc(
        cluster="sandboxed-local",
        namespace="rig-amt",
        pvc_name="data",
        target_pvc_name="data",
        overwrite=True,
        project_name=PROJECT,
    )

    assert result.success, result.error
    pod = _applied_pod(manager)
    assert pod["spec"]["serviceAccountName"] == SERVICE_ACCOUNT
    # Zonder cluster kreeg de pod de OpenShift-tak, en op kind weigert de kubelet dan de
    # niet-numerieke gebruiker van de backup-image.
    assert pod["spec"]["securityContext"]["runAsUser"] == 1001


_STEPS_AROUND_THE_POD = (
    "_ensure_backup_bucket_exists",
    "_get_pvc_info",
    "_create_snapshot",
    "_wait_for_snapshot",
    "_create_clone_pvc",
    "_wait_for_pvc",
    "_create_restore_pvc",
    "_create_project_restore_pvc",
    "_derive_backup_key",
    "_resolve_source_database_name",
    "_cleanup_pod",
    "_cleanup",
)

_PROJECT_DATA = {
    "name": PROJECT,
    "components": [
        {"name": "web", "services": [{"persistent-storage": {"config": [{"name": "data", "mount-path": "/data"}]}}]}
    ],
    "deployments": [{"name": "prod", "components": [{"reference": "web"}]}],
}

type EntryPoint = Callable[[Any], Awaitable[Any]]

# Elke publieke weg naar een backup- of restorepod, zoals de routers en taken hem aanroepen.
ENTRY_POINTS: dict[str, tuple[type[BaseBackupManager], EntryPoint]] = {
    "pvc-backup-project": (
        PVCBackupManager,
        lambda m: m.backup_project_deployment(
            project_name=PROJECT,
            project_data=_PROJECT_DATA,
            deployment_name="prod",
            namespace="rig-amt",
            cluster="sandboxed-local",
            backup_run_id="r",
        ),
    ),
    "pvc-restore": (
        PVCBackupManager,
        lambda m: m.restore_pvc(
            cluster="sandboxed-local", namespace="rig-amt", pvc_name="data", overwrite=True, project_name=PROJECT
        ),
    ),
    "pvc-restore-project": (
        PVCBackupManager,
        lambda m: m.restore_to_project_pvc(
            cluster="sandboxed-local",
            namespace="rig-amt",
            source_pvc_name="data",
            target_pvc_name="data-v1",
            storage_size="1Gi",
            project_name=PROJECT,
        ),
    ),
    "database-backup": (
        DatabaseBackupManager,
        lambda m: m.backup_database(
            namespace="rig-amt",
            **_DB,
            reference_name="db",
            backup_run_id="r",
            cluster="sandboxed-local",
            project_name=PROJECT,
        ),
    ),
    "database-restore": (
        DatabaseBackupManager,
        lambda m: m.restore_database(
            cluster="sandboxed-local",
            namespace="rig-amt",
            reference_name="db",
            target_database_host="pg",
            target_database_port=5432,
            target_database_name="amt_prod",
            target_database_user="amt",
            target_database_password="pw",
            project_name=PROJECT,
        ),
    ),
    "bucket-backup-kopia": (
        BucketBackupManager,
        lambda m: m.backup_bucket(
            namespace="rig-amt",
            **_BUCKET_SOURCE,
            reference_name="bucket",
            backup_run_id="r",
            cluster="sandboxed-local",
            project_name=PROJECT,
        ),
    ),
    "bucket-backup-mirror": (
        BucketBackupManager,
        lambda m: m.backup_bucket(
            namespace="rig-amt",
            **_BUCKET_SOURCE,
            reference_name="bucket",
            backup_run_id="r",
            use_kopia=False,
            cluster="sandboxed-local",
            project_name=PROJECT,
        ),
    ),
    "bucket-restore": (
        BucketBackupManager,
        lambda m: m.restore_bucket(
            cluster="sandboxed-local",
            namespace="rig-amt",
            reference_name="bucket",
            **_BUCKET_TARGET,
            project_name=PROJECT,
        ),
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ENTRY_POINTS)
async def test_every_entry_point_hands_its_project_to_the_pod(entry: str) -> None:
    cls, run = ENTRY_POINTS[entry]
    manager = _manager(cls)
    manager.lock = MagicMock(update_progress=AsyncMock())
    for step in _STEPS_AROUND_THE_POD:
        if hasattr(manager, step):
            setattr(manager, step, AsyncMock(return_value=None))
    manager._derive_backup_key = AsyncMock(return_value="kp")  # type: ignore[method-assign]
    manager._wait_for_pod = AsyncMock(return_value=True)  # type: ignore[method-assign]
    if cls is PVCBackupManager:
        manager._get_pvc_info = AsyncMock(return_value={"name": "data", "size": "1Gi", "storage_class": "standard"})  # type: ignore[method-assign]
    if entry == "pvc-restore-project":
        manager._get_pvc_info = AsyncMock(return_value=None)  # type: ignore[method-assign]

    result = await run(manager)

    for outcome in result if isinstance(result, list) else [result]:
        assert outcome.success, outcome.error
    applied = [
        yaml.safe_load(call.kwargs["stdin_input"])
        for call in manager.kubectl.run_command.await_args_list
        if call.kwargs.get("stdin_input")
    ]
    pods = [doc for doc in applied if doc["kind"] == "Pod"]
    assert len(pods) == 1, applied
    assert pods[0]["spec"]["serviceAccountName"] == SERVICE_ACCOUNT
