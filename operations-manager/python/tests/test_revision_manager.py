"""Tests for the RevisionManager."""

from unittest.mock import MagicMock

import pytest
from opi.manager.revision_manager import RevisionManager


@pytest.fixture
def mock_project_file_handler() -> MagicMock:
    """Create a mock ProjectFileHandler."""
    return MagicMock()


@pytest.fixture
def revision_manager(mock_project_file_handler: MagicMock) -> RevisionManager:
    """Create a RevisionManager instance with mocked dependencies."""
    return RevisionManager(mock_project_file_handler)


@pytest.fixture
def sample_project_data() -> dict:
    """Create sample project data for testing."""
    return {
        "name": "test-project",
        "deployments": [
            {
                "name": "staging",
                "cluster": "local",
                "services": [
                    {
                        "reference": "postgresql-database",
                        "config": {
                            "generation": 1,
                        },
                    }
                ],
            },
            {
                "name": "production",
                "cluster": "local",
                "services": [],
            },
        ],
    }


class TestRevisionManagerGetRevisions:
    """Tests for get_revisions method."""

    def test_returns_empty_list_when_no_revisions(
        self, revision_manager: RevisionManager, sample_project_data: dict
    ) -> None:
        """Should return empty list when no revisions exist."""
        revisions = revision_manager.get_revisions(sample_project_data, "staging", "postgresql-database")
        assert revisions == []

    def test_returns_empty_list_for_unknown_deployment(
        self, revision_manager: RevisionManager, sample_project_data: dict
    ) -> None:
        """Should return empty list for non-existent deployment."""
        revisions = revision_manager.get_revisions(sample_project_data, "unknown", "postgresql-database")
        assert revisions == []

    def test_returns_revisions_when_exist(self, revision_manager: RevisionManager, sample_project_data: dict) -> None:
        """Should return revisions when they exist."""
        # Add revision data
        sample_project_data["deployments"][0]["services"][0]["config"]["revisions"] = [
            {
                "generation": 1,
                "resource": "test_staging_v1",
                "status": "active",
                "created_at": "2026-02-03T10:00:00+00:00",
            }
        ]

        revisions = revision_manager.get_revisions(sample_project_data, "staging", "postgresql-database")
        assert len(revisions) == 1
        assert revisions[0]["generation"] == 1


class TestRevisionManagerGetActiveRevision:
    """Tests for get_active_revision method."""

    def test_returns_none_when_no_revisions(self, revision_manager: RevisionManager, sample_project_data: dict) -> None:
        """Should return None when no revisions exist."""
        active = revision_manager.get_active_revision(sample_project_data, "staging", "postgresql-database")
        assert active is None

    def test_returns_active_revision(self, revision_manager: RevisionManager, sample_project_data: dict) -> None:
        """Should return the active revision when it exists."""
        sample_project_data["deployments"][0]["services"][0]["config"]["revisions"] = [
            {
                "generation": 2,
                "resource": "test_staging_v2",
                "status": "active",
                "created_at": "2026-02-03T12:00:00+00:00",
            },
            {
                "generation": 1,
                "resource": "test_staging_v1",
                "status": "superseded",
                "created_at": "2026-02-03T10:00:00+00:00",
            },
        ]

        active = revision_manager.get_active_revision(sample_project_data, "staging", "postgresql-database")
        assert active is not None
        assert active["generation"] == 2
        assert active["status"] == "active"


class TestRevisionManagerRecordClone:
    """Tests for record_clone method."""

    def test_creates_first_revision(self, revision_manager: RevisionManager, sample_project_data: dict) -> None:
        """Should create the first revision entry."""
        result = revision_manager.record_clone(
            project_data=sample_project_data,
            deployment_name="staging",
            service_type="postgresql-database",
            generation=1,
            resource_name="test_staging_v1",
            source="deployment:production",
        )

        # Verify revision was added
        revisions = result["deployments"][0]["services"][0]["config"]["revisions"]
        assert len(revisions) == 1
        assert revisions[0]["generation"] == 1
        assert revisions[0]["resource"] == "test_staging_v1"
        assert revisions[0]["status"] == "active"
        assert revisions[0]["actions"][0]["type"] == "clone"
        assert revisions[0]["actions"][0]["source"] == "deployment:production"

    def test_supersedes_previous_revision(self, revision_manager: RevisionManager, sample_project_data: dict) -> None:
        """Should mark previous active revision as superseded."""
        # Add existing revision
        sample_project_data["deployments"][0]["services"][0]["config"]["revisions"] = [
            {
                "generation": 1,
                "resource": "test_staging_v1",
                "status": "active",
                "created_at": "2026-02-03T10:00:00+00:00",
                "actions": [{"timestamp": "2026-02-03T10:00:00+00:00", "type": "clone", "source": None}],
            }
        ]

        result = revision_manager.record_clone(
            project_data=sample_project_data,
            deployment_name="staging",
            service_type="postgresql-database",
            generation=2,
            resource_name="test_staging_v2",
            source="external:db.example.com:5432/source_db",
        )

        revisions = result["deployments"][0]["services"][0]["config"]["revisions"]
        assert len(revisions) == 2

        # New revision should be first (most recent) and active
        assert revisions[0]["generation"] == 2
        assert revisions[0]["status"] == "active"

        # Old revision should be superseded
        assert revisions[1]["generation"] == 1
        assert revisions[1]["status"] == "superseded"
        assert "superseded_at" in revisions[1]

    def test_updates_generation_in_config(self, revision_manager: RevisionManager, sample_project_data: dict) -> None:
        """Should update the generation number in service config."""
        result = revision_manager.record_clone(
            project_data=sample_project_data,
            deployment_name="staging",
            service_type="postgresql-database",
            generation=5,
            resource_name="test_staging_v5",
            source="deployment:other",
        )

        generation = result["deployments"][0]["services"][0]["config"]["generation"]
        assert generation == 5

    def test_creates_service_entry_if_missing(
        self, revision_manager: RevisionManager, sample_project_data: dict
    ) -> None:
        """Should create service entry if it doesn't exist."""
        result = revision_manager.record_clone(
            project_data=sample_project_data,
            deployment_name="production",  # This deployment has empty services
            service_type="minio-storage",
            generation=1,
            resource_name="test_production_v1",
            source="deployment:staging",
        )

        # Find the production deployment
        prod_deployment = next(d for d in result["deployments"] if d["name"] == "production")
        assert len(prod_deployment["services"]) == 1
        assert prod_deployment["services"][0]["reference"] == "minio-storage"
        assert prod_deployment["services"][0]["config"]["generation"] == 1


class TestRevisionManagerRecordRestore:
    """Tests for record_restore method."""

    def test_records_restore_action(self, revision_manager: RevisionManager, sample_project_data: dict) -> None:
        """Should record a restore operation."""
        result = revision_manager.record_restore(
            project_data=sample_project_data,
            deployment_name="staging",
            service_type="postgresql-database",
            generation=2,
            resource_name="test_staging_v2",
            backup_reference="backup:2026-02-01",
        )

        revisions = result["deployments"][0]["services"][0]["config"]["revisions"]
        assert len(revisions) == 1
        assert revisions[0]["actions"][0]["type"] == "restore"
        assert revisions[0]["actions"][0]["source"] == "backup:2026-02-01"


class TestRevisionManagerAddAction:
    """Tests for add_action method."""

    def test_adds_action_to_existing_revision(
        self, revision_manager: RevisionManager, sample_project_data: dict
    ) -> None:
        """Should add an action to an existing revision."""
        # Create initial revision
        sample_project_data["deployments"][0]["services"][0]["config"]["revisions"] = [
            {
                "generation": 1,
                "resource": "test_staging_v1",
                "status": "active",
                "created_at": "2026-02-03T10:00:00+00:00",
                "actions": [{"timestamp": "2026-02-03T10:00:00+00:00", "type": "clone", "source": None}],
            }
        ]

        result = revision_manager.add_action(
            project_data=sample_project_data,
            deployment_name="staging",
            service_type="postgresql-database",
            generation=1,
            action="backup",
            source="backup:2026-02-03",
        )

        revisions = result["deployments"][0]["services"][0]["config"]["revisions"]
        assert len(revisions[0]["actions"]) == 2
        assert revisions[0]["actions"][1]["type"] == "backup"

    def test_no_change_for_unknown_generation(
        self, revision_manager: RevisionManager, sample_project_data: dict
    ) -> None:
        """Should not modify data when generation is not found."""
        sample_project_data["deployments"][0]["services"][0]["config"]["revisions"] = [
            {
                "generation": 1,
                "resource": "test_staging_v1",
                "status": "active",
                "actions": [],
            }
        ]

        result = revision_manager.add_action(
            project_data=sample_project_data,
            deployment_name="staging",
            service_type="postgresql-database",
            generation=999,  # Non-existent generation
            action="backup",
        )

        revisions = result["deployments"][0]["services"][0]["config"]["revisions"]
        assert len(revisions[0]["actions"]) == 0  # No action added


class TestRevisionManagerPrune:
    """Tests for prune method."""

    def test_keeps_active_entries(self, revision_manager: RevisionManager, sample_project_data: dict) -> None:
        """Should keep all active entries."""
        sample_project_data["deployments"][0]["services"][0]["config"]["revisions"] = [
            {"generation": 3, "status": "active"},
            {"generation": 2, "status": "superseded", "superseded_at": "2026-02-02T00:00:00+00:00"},
            {"generation": 1, "status": "superseded", "superseded_at": "2026-02-01T00:00:00+00:00"},
        ]

        result = revision_manager.prune(sample_project_data, "staging", "postgresql-database", max_superseded_entries=1)

        revisions = result["deployments"][0]["services"][0]["config"]["revisions"]
        assert len(revisions) == 2  # 1 active + 1 superseded (max)

        # Active should still be there
        active = [r for r in revisions if r["status"] == "active"]
        assert len(active) == 1

    def test_keeps_most_recent_superseded(self, revision_manager: RevisionManager, sample_project_data: dict) -> None:
        """Should keep the most recent superseded entries."""
        sample_project_data["deployments"][0]["services"][0]["config"]["revisions"] = [
            {"generation": 5, "status": "active"},
            {"generation": 4, "status": "superseded", "superseded_at": "2026-02-04T00:00:00+00:00"},
            {"generation": 3, "status": "superseded", "superseded_at": "2026-02-03T00:00:00+00:00"},
            {"generation": 2, "status": "superseded", "superseded_at": "2026-02-02T00:00:00+00:00"},
            {"generation": 1, "status": "superseded", "superseded_at": "2026-02-01T00:00:00+00:00"},
        ]

        result = revision_manager.prune(sample_project_data, "staging", "postgresql-database", max_superseded_entries=2)

        revisions = result["deployments"][0]["services"][0]["config"]["revisions"]
        superseded = [r for r in revisions if r["status"] == "superseded"]

        assert len(superseded) == 2
        # Should have kept generations 4 and 3 (most recent)
        superseded_gens = {r["generation"] for r in superseded}
        assert superseded_gens == {4, 3}


class TestRevisionManagerGetSupersededResources:
    """Tests for get_superseded_resources method."""

    def test_returns_superseded_entries(self, revision_manager: RevisionManager, sample_project_data: dict) -> None:
        """Should return only superseded entries."""
        sample_project_data["deployments"][0]["services"][0]["config"]["revisions"] = [
            {"generation": 3, "resource": "test_v3", "status": "active"},
            {"generation": 2, "resource": "test_v2", "status": "superseded"},
            {"generation": 1, "resource": "test_v1", "status": "superseded"},
        ]

        superseded = revision_manager.get_superseded_resources(sample_project_data, "staging", "postgresql-database")

        assert len(superseded) == 2
        resources = {r["resource"] for r in superseded}
        assert resources == {"test_v2", "test_v1"}

    def test_returns_empty_when_no_superseded(
        self, revision_manager: RevisionManager, sample_project_data: dict
    ) -> None:
        """Should return empty list when no superseded entries."""
        sample_project_data["deployments"][0]["services"][0]["config"]["revisions"] = [
            {"generation": 1, "resource": "test_v1", "status": "active"},
        ]

        superseded = revision_manager.get_superseded_resources(sample_project_data, "staging", "postgresql-database")

        assert superseded == []
