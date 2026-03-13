"""Tests for marked-for-deletion service and reconciliation logic.

Covers:
- MarkedForDeletionService CRUD operations (via mocked DB pool)
- _build_expected_resources resource inventory builder
- reconcile() flow: unmark restored, purge expired
- cleanup_project() with SQL-level project filtering
- _purge_backup_data mark retention on incomplete metadata
- Deletion ordering in _purge_marks
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.jobs.reconciliation import _build_expected_resources, _purge_backup_data, cleanup_project, reconcile
from opi.services.marked_for_deletion_service import MarkedForDeletionService, _row_to_dict

# --- _row_to_dict tests ---


class TestRowToDict:
    def test_none_returns_none(self) -> None:
        assert _row_to_dict(None) is None

    def test_converts_uuid_to_string(self) -> None:
        test_uuid = uuid.uuid4()
        row = {"id": test_uuid, "name": "test"}
        result = _row_to_dict(row)
        assert result["id"] == str(test_uuid)
        assert result["name"] == "test"

    def test_converts_datetime_to_isoformat(self) -> None:
        now = datetime.now(tz=UTC)
        row = {"marked_at": now, "name": "test"}
        result = _row_to_dict(row)
        assert result["marked_at"] == now.isoformat()

    def test_passes_through_plain_values(self) -> None:
        row = {"name": "test", "count": 42, "active": True}
        result = _row_to_dict(row)
        assert result == {"name": "test", "count": 42, "active": True}


# --- _build_expected_resources tests ---


class TestBuildExpectedResources:
    def test_empty_input(self) -> None:
        result = _build_expected_resources([])
        for resource_set in result.values():
            assert len(resource_set) == 0

    def test_database_service(self) -> None:
        yamls = [
            {
                "name": "myproject",
                "deployments": [
                    {
                        "name": "staging",
                        "cluster": "local",
                        "namespace": "myproject",
                        "services": [{"reference": "database"}],
                    }
                ],
            }
        ]
        result = _build_expected_resources(yamls)
        # Should have a postgresql_database and postgresql_user entry
        assert len(result["postgresql_database"]) == 1
        assert len(result["postgresql_user"]) == 1
        # Each entry is a (name, cluster) tuple
        db_entry = next(iter(result["postgresql_database"]))
        assert db_entry[1] == "local"

    def test_minio_service(self) -> None:
        yamls = [
            {
                "name": "myproject",
                "deployments": [
                    {
                        "name": "staging",
                        "cluster": "local",
                        "namespace": "myproject",
                        "services": [{"reference": "minio"}],
                    }
                ],
            }
        ]
        result = _build_expected_resources(yamls)
        assert len(result["minio_bucket"]) == 1
        assert len(result["minio_user"]) == 1
        assert len(result["minio_policy"]) == 1

    def test_cluster_dimension_prevents_cross_cluster_match(self) -> None:
        """Two clusters with same project/deployment should produce distinct entries."""
        yamls = [
            {
                "name": "myproject",
                "deployments": [
                    {
                        "name": "main",
                        "cluster": "cluster-a",
                        "namespace": "myproject",
                        "services": [{"reference": "database"}],
                    },
                    {
                        "name": "main",
                        "cluster": "cluster-b",
                        "namespace": "myproject",
                        "services": [{"reference": "database"}],
                    },
                ],
            }
        ]
        with (
            patch("opi.core.cluster_config.get_prefixed_namespace", side_effect=lambda c, ns: f"{c}-{ns}"),
            patch("opi.manager.backup.base.get_backup_bucket_name", return_value="backup-bucket"),
        ):
            result = _build_expected_resources(yamls)
        # Same name, different clusters -> 2 entries
        assert len(result["postgresql_database"]) == 2
        clusters = {entry[1] for entry in result["postgresql_database"]}
        assert clusters == {"cluster-a", "cluster-b"}

    def test_non_list_services_skipped(self) -> None:
        yamls = [
            {
                "name": "myproject",
                "deployments": [
                    {
                        "name": "staging",
                        "cluster": "local",
                        "namespace": "myproject",
                        "services": "not-a-list",
                    }
                ],
            }
        ]
        result = _build_expected_resources(yamls)
        assert len(result["postgresql_database"]) == 0

    def test_multiple_service_references(self) -> None:
        """All valid reference aliases should be recognized."""
        yamls = [
            {
                "name": "myproject",
                "deployments": [
                    {
                        "name": "staging",
                        "cluster": "local",
                        "namespace": "myproject",
                        "services": [
                            {"reference": "postgresql"},
                            {"reference": "minio-storage"},
                        ],
                    }
                ],
            }
        ]
        result = _build_expected_resources(yamls)
        assert len(result["postgresql_database"]) == 1
        assert len(result["minio_bucket"]) == 1


# --- MarkedForDeletionService tests (mocked DB) ---


def _make_mock_pool() -> MagicMock:
    """Create a mock DatabasePool with acquire/release."""
    pool = MagicMock()
    conn = AsyncMock()
    pool.acquire = AsyncMock(return_value=conn)
    pool.release = AsyncMock()
    return pool


def _make_mark_row(
    resource_type: str = "postgresql_database",
    resource_name: str = "mydb",
    project_name: str = "myproject",
    deployment_name: str = "staging",
    cluster: str = "local",
    mark_id: str | None = None,
    marked_at: datetime | None = None,
) -> dict:
    """Create a mark dict matching what _row_to_dict would produce."""
    return {
        "id": mark_id or str(uuid.uuid4()),
        "resource_type": resource_type,
        "resource_name": resource_name,
        "project_name": project_name,
        "deployment_name": deployment_name,
        "cluster": cluster,
        "marked_at": (marked_at or datetime.now(tz=UTC)).isoformat(),
        "metadata": {},
    }


class TestMarkedForDeletionService:
    @pytest.mark.asyncio
    async def test_mark_resource_calls_upsert(self) -> None:
        pool = _make_mock_pool()
        conn = await pool.acquire()
        conn.fetchrow = AsyncMock(return_value={"id": uuid.uuid4(), "resource_type": "postgresql_database"})

        service = MarkedForDeletionService(pool)
        result = await service.mark_resource(
            resource_type="postgresql_database",
            resource_name="mydb",
            project_name="myproject",
            deployment_name="staging",
            cluster="local",
        )

        conn.fetchrow.assert_called_once()
        sql = conn.fetchrow.call_args[0][0]
        assert "ON CONFLICT" in sql
        assert "DO UPDATE" in sql

    @pytest.mark.asyncio
    async def test_unmark_resource_deletes_by_type_name_cluster(self) -> None:
        pool = _make_mock_pool()
        conn = await pool.acquire()
        conn.execute = AsyncMock(return_value="DELETE 1")

        service = MarkedForDeletionService(pool)
        result = await service.unmark_resource("postgresql_database", "mydb", "local")

        assert result is True
        conn.execute.assert_called_once()
        sql = conn.execute.call_args[0][0]
        assert "resource_type = $1" in sql
        assert "resource_name = $2" in sql
        assert "cluster = $3" in sql

    @pytest.mark.asyncio
    async def test_unmark_returns_false_when_no_match(self) -> None:
        pool = _make_mock_pool()
        conn = await pool.acquire()
        conn.execute = AsyncMock(return_value="DELETE 0")

        service = MarkedForDeletionService(pool)
        result = await service.unmark_resource("postgresql_database", "nonexistent", "local")

        assert result is False

    @pytest.mark.asyncio
    async def test_get_expired_marks_without_project_filter(self) -> None:
        pool = _make_mock_pool()
        conn = await pool.acquire()
        conn.fetch = AsyncMock(return_value=[])

        service = MarkedForDeletionService(pool)
        result = await service.get_expired_marks(7)

        conn.fetch.assert_called_once()
        sql = conn.fetch.call_args[0][0]
        assert "project_name" not in sql

    @pytest.mark.asyncio
    async def test_get_expired_marks_with_project_filter(self) -> None:
        pool = _make_mock_pool()
        conn = await pool.acquire()
        conn.fetch = AsyncMock(return_value=[])

        service = MarkedForDeletionService(pool)
        result = await service.get_expired_marks(7, project_name="myproject")

        conn.fetch.assert_called_once()
        sql = conn.fetch.call_args[0][0]
        assert "project_name = $2" in sql
        args = conn.fetch.call_args[0]
        assert args[1] == 7
        assert args[2] == "myproject"

    @pytest.mark.asyncio
    async def test_delete_mark_returns_true_on_success(self) -> None:
        pool = _make_mock_pool()
        conn = await pool.acquire()
        conn.execute = AsyncMock(return_value="DELETE 1")

        service = MarkedForDeletionService(pool)
        mark_id = str(uuid.uuid4())
        result = await service.delete_mark(mark_id)

        assert result is True

    @pytest.mark.asyncio
    async def test_delete_mark_returns_false_when_not_found(self) -> None:
        pool = _make_mock_pool()
        conn = await pool.acquire()
        conn.execute = AsyncMock(return_value="DELETE 0")

        service = MarkedForDeletionService(pool)
        result = await service.delete_mark(str(uuid.uuid4()))

        assert result is False


# --- reconcile() tests ---


class TestReconcile:
    @pytest.mark.asyncio
    async def test_unmarks_restored_resources(self) -> None:
        """Resources that reappear in project YAMLs should be unmarked."""
        pool = _make_mock_pool()
        mark_id = str(uuid.uuid4())

        mock_service = AsyncMock(spec=MarkedForDeletionService)
        mock_service.get_all_marks = AsyncMock(
            return_value=[
                _make_mark_row(
                    resource_type="postgresql_database",
                    resource_name="myproject_staging",
                    cluster="local",
                    mark_id=mark_id,
                ),
            ]
        )
        mock_service.get_expired_marks = AsyncMock(return_value=[])

        yamls = [
            {
                "name": "myproject",
                "deployments": [
                    {
                        "name": "staging",
                        "cluster": "local",
                        "namespace": "myproject",
                        "services": [{"reference": "database"}],
                    }
                ],
            }
        ]

        with patch("opi.jobs.reconciliation.MarkedForDeletionService", return_value=mock_service):
            results = await reconcile(pool, yamls, grace_period_days=7, dry_run=False)

        assert len(results["unmarked"]) == 1
        assert results["unmarked"][0]["type"] == "postgresql_database"
        mock_service.unmark_resource.assert_called_once_with("postgresql_database", "myproject_staging", "local")

    @pytest.mark.asyncio
    async def test_dry_run_does_not_unmark(self) -> None:
        pool = _make_mock_pool()

        mock_service = AsyncMock(spec=MarkedForDeletionService)
        mock_service.get_all_marks = AsyncMock(
            return_value=[
                _make_mark_row(
                    resource_type="postgresql_database",
                    resource_name="myproject_staging",
                    cluster="local",
                ),
            ]
        )
        mock_service.get_expired_marks = AsyncMock(return_value=[])

        yamls = [
            {
                "name": "myproject",
                "deployments": [
                    {
                        "name": "staging",
                        "cluster": "local",
                        "namespace": "myproject",
                        "services": [{"reference": "database"}],
                    }
                ],
            }
        ]

        with patch("opi.jobs.reconciliation.MarkedForDeletionService", return_value=mock_service):
            results = await reconcile(pool, yamls, grace_period_days=7, dry_run=True)

        assert len(results["unmarked"]) == 1
        mock_service.unmark_resource.assert_not_called()

    @pytest.mark.asyncio
    async def test_does_not_unmark_orphaned_resources(self) -> None:
        """Marks for resources NOT in expected set should remain."""
        pool = _make_mock_pool()

        mock_service = AsyncMock(spec=MarkedForDeletionService)
        mock_service.get_all_marks = AsyncMock(
            return_value=[
                _make_mark_row(
                    resource_type="postgresql_database",
                    resource_name="deleted_project_db",
                    cluster="local",
                ),
            ]
        )
        mock_service.get_expired_marks = AsyncMock(return_value=[])

        with patch("opi.jobs.reconciliation.MarkedForDeletionService", return_value=mock_service):
            results = await reconcile(pool, [], grace_period_days=7, dry_run=False)

        assert len(results["unmarked"]) == 0
        mock_service.unmark_resource.assert_not_called()


# --- cleanup_project() tests ---


class TestCleanupProject:
    @pytest.mark.asyncio
    async def test_uses_sql_level_project_filter(self) -> None:
        """cleanup_project should pass project_name to get_expired_marks."""
        pool = _make_mock_pool()

        mock_service = AsyncMock(spec=MarkedForDeletionService)
        mock_service.get_expired_marks = AsyncMock(return_value=[])

        with patch("opi.jobs.reconciliation.MarkedForDeletionService", return_value=mock_service):
            results = await cleanup_project(pool, "myproject", grace_period_days=7)

        mock_service.get_expired_marks.assert_called_once_with(7, project_name="myproject")
        assert results["project_name"] == "myproject"
        assert results["purged"] == []

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_expired_marks(self) -> None:
        pool = _make_mock_pool()

        mock_service = AsyncMock(spec=MarkedForDeletionService)
        mock_service.get_expired_marks = AsyncMock(return_value=[])

        with patch("opi.jobs.reconciliation.MarkedForDeletionService", return_value=mock_service):
            results = await cleanup_project(pool, "myproject", grace_period_days=7)

        assert results["purged"] == []
        assert results["errors"] == []

    @pytest.mark.asyncio
    async def test_uses_default_grace_period_from_settings(self) -> None:
        pool = _make_mock_pool()

        mock_service = AsyncMock(spec=MarkedForDeletionService)
        mock_service.get_expired_marks = AsyncMock(return_value=[])

        with (
            patch("opi.jobs.reconciliation.MarkedForDeletionService", return_value=mock_service),
            patch("opi.jobs.reconciliation.settings") as mock_settings,
        ):
            mock_settings.DELETION_GRACE_PERIOD_DAYS = 14
            results = await cleanup_project(pool, "myproject")

        mock_service.get_expired_marks.assert_called_once_with(14, project_name="myproject")


# --- _purge_backup_data tests ---


class TestPurgeBackupData:
    @pytest.mark.asyncio
    async def test_incomplete_metadata_keeps_mark(self) -> None:
        """When backup metadata is incomplete, the mark should be retained."""
        mock_service = AsyncMock(spec=MarkedForDeletionService)
        mark = _make_mark_row(
            resource_type="backup_data",
            resource_name="backup-bucket/prefix",
            mark_id="keep-this-mark",
        )
        mark["metadata"] = {"s3_bucket": "bucket", "s3_prefix": "prefix"}
        # Missing kopia_password

        results: dict[str, list] = {"purged": [], "errors": []}

        with patch("opi.jobs.reconciliation.settings") as mock_settings:
            mock_settings.BACKUP_S3_ENDPOINT = "s3.example.com"
            mock_settings.BACKUP_S3_ACCESS_KEY = "key"
            mock_settings.BACKUP_S3_SECRET_KEY = "secret"
            mock_settings.BACKUP_S3_USE_TLS = True
            await _purge_backup_data(mark, mock_service, results, dry_run=False)

        # Mark should NOT be deleted
        mock_service.delete_mark.assert_not_called()
        # Error should be recorded
        assert len(results["errors"]) == 1
        assert "Incomplete backup metadata" in results["errors"][0]
        assert "retained" in results["errors"][0]

    @pytest.mark.asyncio
    async def test_no_snapshots_purges_mark(self) -> None:
        """When Kopia has no snapshots, the mark should be deleted."""
        mock_service = AsyncMock(spec=MarkedForDeletionService)
        mark = _make_mark_row(
            resource_type="backup_data",
            resource_name="backup-bucket/prefix",
        )
        mark["metadata"] = {
            "s3_bucket": "bucket",
            "s3_prefix": "prefix",
            "kopia_password": "pass123",
        }

        results: dict[str, list] = {"purged": [], "errors": []}

        mock_kopia_instance = AsyncMock()
        mock_kopia_instance.list_snapshots = AsyncMock(return_value=[])

        mock_kopia_cls = MagicMock()
        mock_kopia_cls.is_kopia_available = True
        mock_kopia_cls.return_value = mock_kopia_instance
        mock_repo_config_cls = MagicMock()

        with (
            patch("opi.jobs.reconciliation.settings") as mock_settings,
            patch.dict(
                "sys.modules",
                {
                    "opi.connectors.kopia": MagicMock(
                        KopiaConnector=mock_kopia_cls,
                        KopiaRepositoryConfig=mock_repo_config_cls,
                    )
                },
            ),
        ):
            mock_settings.BACKUP_S3_ENDPOINT = "s3.example.com"
            mock_settings.BACKUP_S3_ACCESS_KEY = "key"
            mock_settings.BACKUP_S3_SECRET_KEY = "secret"
            mock_settings.BACKUP_S3_USE_TLS = True
            await _purge_backup_data(mark, mock_service, results, dry_run=False)

        mock_service.delete_mark.assert_called_once_with(mark["id"])
        assert len(results["purged"]) == 1

    @pytest.mark.asyncio
    async def test_partial_snapshot_deletion_keeps_mark(self) -> None:
        """When only some snapshots are deleted, mark should be retained for retry."""
        mock_service = AsyncMock(spec=MarkedForDeletionService)
        mark = _make_mark_row(
            resource_type="backup_data",
            resource_name="backup-bucket/prefix",
        )
        mark["metadata"] = {
            "s3_bucket": "bucket",
            "s3_prefix": "prefix",
            "kopia_password": "pass123",
        }

        results: dict[str, list] = {"purged": [], "errors": []}

        snapshot_1 = MagicMock(snapshot_id="snap-1")
        snapshot_2 = MagicMock(snapshot_id="snap-2")

        mock_kopia_instance = AsyncMock()
        mock_kopia_instance.list_snapshots = AsyncMock(return_value=[snapshot_1, snapshot_2])
        # First snapshot succeeds, second fails
        mock_kopia_instance.delete_snapshot = AsyncMock(side_effect=[True, False])

        mock_kopia_cls = MagicMock()
        mock_kopia_cls.is_kopia_available = True
        mock_kopia_cls.return_value = mock_kopia_instance
        mock_repo_config_cls = MagicMock()

        with (
            patch("opi.jobs.reconciliation.settings") as mock_settings,
            patch.dict(
                "sys.modules",
                {
                    "opi.connectors.kopia": MagicMock(
                        KopiaConnector=mock_kopia_cls,
                        KopiaRepositoryConfig=mock_repo_config_cls,
                    )
                },
            ),
        ):
            mock_settings.BACKUP_S3_ENDPOINT = "s3.example.com"
            mock_settings.BACKUP_S3_ACCESS_KEY = "key"
            mock_settings.BACKUP_S3_SECRET_KEY = "secret"
            mock_settings.BACKUP_S3_USE_TLS = True
            await _purge_backup_data(mark, mock_service, results, dry_run=False)

        # Mark should NOT be deleted (partial success)
        mock_service.delete_mark.assert_not_called()
        assert results["purged"][0]["snapshots_deleted"] == 1
