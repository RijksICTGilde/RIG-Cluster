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
from opi.services.marked_for_deletion_service import MarkedForDeletionService

# --- _row_to_dict tests ---


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

    def test_central_postgres_generation_in_expected_db_name(self) -> None:
        """A cloned central-postgres deployment's _vN database name is expected."""
        yamls = [
            {
                "name": "myproject",
                "deployments": [
                    {
                        "name": "staging",
                        "cluster": "local",
                        "namespace": "myproject",
                        "services": [
                            {"reference": "postgresql-database", "config": {"generation": 2}},
                        ],
                    }
                ],
            }
        ]
        result = _build_expected_resources(yamls)
        assert ("myproject_staging_v2", "local") in result["postgresql_database"]
        # The user never carries the generation suffix.
        assert ("myproject_staging", "local") in result["postgresql_user"]

    def test_namespace_postgres_generation_in_expected_db_name(self) -> None:
        """Regression: a cloned namespace-postgres deployment stores its generation
        under the namespace-postgresql-database service, not the central one.

        The expected-set builder must resolve generation against both DB service
        types; otherwise the live _vN database falls out of the expected set and
        the purge-time unmark safety can no longer protect it (waggl-9et class).
        """
        yamls = [
            {
                "name": "myproject",
                "deployments": [
                    {
                        "name": "staging",
                        "cluster": "local",
                        "namespace": "myproject",
                        "services": [
                            {"reference": "namespace-postgresql-database", "config": {"generation": 3}},
                        ],
                    }
                ],
            }
        ]
        result = _build_expected_resources(yamls)
        assert ("myproject_staging_v3", "local") in result["postgresql_database"]
        assert ("myproject_staging", "local") in result["postgresql_user"]


# --- MarkedForDeletionService tests (mocked DB) ---


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


# --- reconcile() tests ---


class TestReconcile:
    @pytest.mark.asyncio
    async def test_unmarks_restored_resources(self) -> None:
        """Resources that reappear in project YAMLs should be unmarked."""
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
            results = await reconcile(yamls, grace_period_days=7, dry_run=False)

        assert len(results["unmarked"]) == 1
        assert results["unmarked"][0]["type"] == "postgresql_database"
        mock_service.unmark_resource.assert_called_once_with("postgresql_database", "myproject_staging", "local")

    @pytest.mark.asyncio
    async def test_dry_run_does_not_unmark(self) -> None:
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
            results = await reconcile(yamls, grace_period_days=7, dry_run=True)

        assert len(results["unmarked"]) == 1
        mock_service.unmark_resource.assert_not_called()

    @pytest.mark.asyncio
    async def test_does_not_unmark_orphaned_resources(self) -> None:
        """Marks for resources NOT in expected set should remain."""
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
            results = await reconcile([], grace_period_days=7, dry_run=False)

        assert len(results["unmarked"]) == 0
        mock_service.unmark_resource.assert_not_called()


# --- cleanup_project() tests ---


class TestCleanupProject:
    @pytest.mark.asyncio
    async def test_uses_sql_level_project_filter(self) -> None:
        """cleanup_project should pass project_name to get_expired_marks."""
        mock_service = AsyncMock(spec=MarkedForDeletionService)
        mock_service.get_expired_marks = AsyncMock(return_value=[])

        with patch("opi.jobs.reconciliation.MarkedForDeletionService", return_value=mock_service):
            results = await cleanup_project("myproject", grace_period_days=7)

        mock_service.get_expired_marks.assert_called_once_with(7, project_name="myproject")
        assert results["project_name"] == "myproject"
        assert results["purged"] == []

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_expired_marks(self) -> None:
        mock_service = AsyncMock(spec=MarkedForDeletionService)
        mock_service.get_expired_marks = AsyncMock(return_value=[])

        with patch("opi.jobs.reconciliation.MarkedForDeletionService", return_value=mock_service):
            results = await cleanup_project("myproject", grace_period_days=7)

        assert results["purged"] == []
        assert results["errors"] == []

    @pytest.mark.asyncio
    async def test_uses_default_grace_period_from_settings(self) -> None:
        mock_service = AsyncMock(spec=MarkedForDeletionService)
        mock_service.get_expired_marks = AsyncMock(return_value=[])

        with (
            patch("opi.jobs.reconciliation.MarkedForDeletionService", return_value=mock_service),
            patch("opi.jobs.reconciliation.settings") as mock_settings,
        ):
            mock_settings.DELETION_GRACE_PERIOD_DAYS = 14
            results = await cleanup_project("myproject")

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


class TestBuildExpectedResourcesV2:
    """Schema-v2 shapes as they exist in production project files.

    Regression: the builder only read deployment-level ``services`` with
    legacy names (``database``/``minio``), so the expected set was empty for
    v2 projects and the restored->unmark safety never fired (waggl-9et).
    """

    def _waggl_shape(self) -> dict:
        """Mirrors waggl-9et.yaml: service on the catalog component, none on deployment."""
        return {
            "name": "waggl-9et",
            "schema-version": 2.2,
            "components": [
                {
                    "name": "backend",
                    "services": [
                        "publish-on-web",
                        "keycloak",
                        {"persistent-storage": {"config": [{"name": "data", "size": "100Mi"}]}},
                        "postgresql-database",
                    ],
                },
                {"name": "frontend", "services": ["publish-on-web"]},
            ],
            "deployments": [
                {
                    "name": "productie",
                    "cluster": "odcn-production",
                    "namespace": "waggl-9et",
                    "components": [{"reference": "frontend"}, {"reference": "backend"}],
                }
            ],
        }

    def test_component_catalog_service_is_seen(self) -> None:
        with (
            patch("opi.core.cluster_config.get_prefixed_namespace", side_effect=lambda c, ns: f"rig-prd-{ns}"),
            patch("opi.manager.backup.base.get_backup_bucket_name", return_value="backup-bucket"),
        ):
            result = _build_expected_resources([self._waggl_shape()])
        assert ("waggl_9et_productie", "odcn-production") in result["postgresql_database"]
        assert ("waggl_9et_productie", "odcn-production") in result["postgresql_user"]

    def test_deployment_level_plain_string_is_seen(self) -> None:
        """regel-k4c style: schema 2.2 with plain-string deployment-level services."""
        yamls = [
            {
                "name": "regel-k4c",
                "schema-version": 2.2,
                "components": [],
                "deployments": [
                    {
                        "name": "regelrecht",
                        "cluster": "odcn-production",
                        "namespace": "regel-k4c",
                        "services": ["postgresql-database"],
                    }
                ],
            }
        ]
        with (
            patch("opi.core.cluster_config.get_prefixed_namespace", side_effect=lambda c, ns: f"rig-prd-{ns}"),
            patch("opi.manager.backup.base.get_backup_bucket_name", return_value="backup-bucket"),
        ):
            result = _build_expected_resources(yamls)
        assert ("regel_k4c_regelrecht", "odcn-production") in result["postgresql_database"]

    def test_generation_suffix_on_database_not_user(self) -> None:
        """Clone/restore generation versions the database name, never the username."""
        yamls = [
            {
                "name": "regel-k4c",
                "schema-version": 2.2,
                "components": [],
                "deployments": [
                    {
                        "name": "pr748",
                        "cluster": "odcn-production",
                        "namespace": "regel-k4c",
                        "services": [{"reference": "postgresql-database", "config": {"generation": 1}}],
                    }
                ],
            }
        ]
        with (
            patch("opi.core.cluster_config.get_prefixed_namespace", side_effect=lambda c, ns: f"rig-prd-{ns}"),
            patch("opi.manager.backup.base.get_backup_bucket_name", return_value="backup-bucket"),
        ):
            result = _build_expected_resources(yamls)
        assert ("regel_k4c_pr748_v1", "odcn-production") in result["postgresql_database"]
        assert ("regel_k4c_pr748", "odcn-production") in result["postgresql_user"]

    def test_minio_storage_v2_name(self) -> None:
        yamls = [
            {
                "name": "amt-odc-prd",
                "schema-version": 2,
                "components": [],
                "deployments": [
                    {
                        "name": "productie",
                        "cluster": "odcn-production",
                        "namespace": "amt-odc-prd",
                        "services": ["minio-storage"],
                    }
                ],
            }
        ]
        with (
            patch("opi.core.cluster_config.get_prefixed_namespace", side_effect=lambda c, ns: f"rig-prd-{ns}"),
            patch("opi.manager.backup.base.get_backup_bucket_name", return_value="backup-bucket"),
        ):
            result = _build_expected_resources(yamls)
        assert len(result["minio_bucket"]) == 1
        assert len(result["minio_user"]) == 1


# --- purge safety gate tests ---


class TestPurgeSafetyGates:
    """Nothing that is actively used may ever be purged.

    Two independent gates, both re-checked at purge time:
    1. Expected-set membership: a mark whose resource is back in the project
       YAMLs is unmarked, not purged (the waggl-9et scenario).
    2. Active connections: a marked database with live connections is refused
       and reported, never dropped.
    """

    def _db_mark(self, name: str = "waggl_9et_productie") -> dict:
        return {
            "id": "mark-1",
            "resource_type": "postgresql_database",
            "resource_name": name,
            "project_name": "waggl-9et",
            "deployment_name": "productie",
            "cluster": "odcn-production",
        }

    @pytest.mark.asyncio
    async def test_mark_in_expected_set_is_unmarked_not_purged(self) -> None:
        from opi.jobs.reconciliation import _purge_marks

        mock_service = AsyncMock(spec=MarkedForDeletionService)
        results: dict = {"purged": [], "errors": []}
        expected = {"postgresql_database": {("waggl_9et_productie", "odcn-production")}}

        with patch("opi.jobs.reconciliation.create_postgres_connector") as mock_pg:
            await _purge_marks([self._db_mark()], mock_service, results, dry_run=False, expected=expected)

        mock_pg.assert_not_called()
        mock_service.delete_mark.assert_awaited_once_with("mark-1")
        assert results["unmarked"] == [
            {"type": "postgresql_database", "name": "waggl_9et_productie", "cluster": "odcn-production"}
        ]
        assert results["purged"] == []

    @pytest.mark.asyncio
    async def test_database_with_active_connections_is_refused(self) -> None:
        from opi.jobs.reconciliation import _purge_postgres_database

        mock_connector = AsyncMock()
        mock_connector.count_active_connections = AsyncMock(return_value=5)
        mock_service = AsyncMock(spec=MarkedForDeletionService)
        results: dict = {"purged": [], "errors": []}

        await _purge_postgres_database(mock_connector, self._db_mark(), mock_service, results, dry_run=False)

        mock_connector.delete_database.assert_not_called()
        mock_service.delete_mark.assert_not_called()
        assert results["purged"] == []
        assert len(results["refused"]) == 1
        assert "5 active connection(s)" in results["refused"][0]["reason"]

    @pytest.mark.asyncio
    async def test_database_without_connections_is_purged(self) -> None:
        from opi.jobs.reconciliation import _purge_postgres_database

        mock_connector = AsyncMock()
        mock_connector.count_active_connections = AsyncMock(return_value=0)
        mock_service = AsyncMock(spec=MarkedForDeletionService)
        results: dict = {"purged": [], "errors": []}

        mark = self._db_mark(name="regel_k4c_pr375")
        await _purge_postgres_database(mock_connector, mark, mock_service, results, dry_run=False)

        mock_connector.delete_database.assert_awaited_once_with("regel_k4c_pr375")
        mock_service.delete_mark.assert_awaited_once()
        assert results["purged"] == [{"type": "postgresql_database", "name": "regel_k4c_pr375"}]


# ---------------------------------------------------------------------------
# Real-Postgres tests for the ORM-backed MarkedForDeletionService
# ---------------------------------------------------------------------------


class TestMarkedForDeletionServiceRealDB:
    async def test_mark_upsert_refreshes_metadata_keeps_marked_at(self, orm_db):
        svc = MarkedForDeletionService()
        first = await svc.mark_resource("minio_bucket", "b1", "p1", "d1", "c1", {"namespace": "ns1"})
        assert first["metadata"] == {"namespace": "ns1"}
        again = await svc.mark_resource("minio_bucket", "b1", "p1-new", "d1", "c1", {"namespace": "ns2"})
        assert again["project_name"] == "p1-new"
        assert again["metadata"] == {"namespace": "ns2"}
        assert again["marked_at"] == first["marked_at"]  # grace-period start preserved
        assert len(await svc.get_all_marks()) == 1

    async def test_unmark_resource(self, orm_db):
        svc = MarkedForDeletionService()
        await svc.mark_resource("pvc", "v1", "p1", "d1", "c1")
        assert await svc.unmark_resource("pvc", "v1", "c1") is True
        assert await svc.unmark_resource("pvc", "v1", "c1") is False

    async def test_get_expired_marks_grace_and_project_filter(self, orm_db):
        svc = MarkedForDeletionService()
        await svc.mark_resource("db", "x", "p1", "d1", "c1")
        await svc.mark_resource("db", "y", "p2", "d1", "c1")
        assert len(await svc.get_expired_marks(0)) == 2  # grace 0 -> all past cutoff
        assert await svc.get_expired_marks(1) == []  # fresh marks are within grace
        assert len(await svc.get_expired_marks(0, project_name="p1")) == 1

    async def test_get_marks_for_project_and_delete(self, orm_db):
        svc = MarkedForDeletionService()
        m = await svc.mark_resource("db", "x", "p1", "d1", "c1")
        assert len(await svc.get_marks_for_project("p1")) == 1
        assert await svc.delete_mark(m["id"]) is True
        assert await svc.delete_mark(m["id"]) is False
        assert await svc.get_marks_for_project("p1") == []

    async def test_get_marks_in_namespace(self, orm_db):
        svc = MarkedForDeletionService()
        await svc.mark_resource("namespace", "ns1", "p1", "d1", "c1")
        await svc.mark_resource("pvc", "v1", "p1", "d1", "c1", {"namespace": "ns1"})
        await svc.mark_resource("pvc", "v2", "p1", "d1", "c1", {"namespace": "other"})
        names = sorted(m["resource_name"] for m in await svc.get_marks_in_namespace("ns1", "c1"))
        assert names == ["ns1", "v1"]
        assert await svc.get_marks_in_namespace("ns1", "other-cluster") == []
