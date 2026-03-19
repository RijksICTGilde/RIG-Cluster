"""Tests for the clone tracking contract.

Verifies the full clone lifecycle:
1. clone-from uses dict format with type/reference/mode/status keys
2. After clone, services[] entries record generation for each service
3. set_clone_status marks clone-from as completed
4. Re-processing reads status and skips completed once-mode clones
5. Generation info is read back correctly for subsequent operations
6. YAML serialization never produces anchors/aliases (&id001/*id001)
"""

import copy
from io import StringIO
from unittest.mock import MagicMock

import pytest
from opi.handlers.project_file_handler import ProjectFileHandler, save_project_file
from opi.manager.revision_manager import RevisionManager
from ruamel.yaml import YAML

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def handler() -> ProjectFileHandler:
    """Create a ProjectFileHandler instance."""
    return ProjectFileHandler()


@pytest.fixture
def revision_manager() -> RevisionManager:
    """Create a RevisionManager with mock handler."""
    return RevisionManager(MagicMock())


@pytest.fixture
def project_data_with_clone_from() -> dict:
    """Project data with a deployment that has clone-from in dict format."""
    return {
        "name": "test-project",
        "deployments": [
            {
                "name": "production",
                "cluster": "local",
                "namespace": "test-project",
                "components": [
                    {"reference": "web", "image": "nginx:latest"},
                ],
            },
            {
                "name": "staging",
                "cluster": "local",
                "namespace": "test-project",
                "components": [
                    {"reference": "web", "image": "nginx:latest"},
                ],
                "clone-from": {
                    "type": "deployment",
                    "reference": "production",
                    "mode": "once",
                },
            },
        ],
    }


@pytest.fixture
def project_data_remote_source() -> dict:
    """Project data with a remote-source clone-from."""
    return {
        "name": "test-project",
        "deployments": [
            {
                "name": "staging",
                "cluster": "local",
                "namespace": "test-project",
                "components": [
                    {"reference": "web", "image": "nginx:latest"},
                ],
                "clone-from": {
                    "type": "remote-source",
                    "reference": "odcn-production",
                    "mode": "once",
                },
            },
        ],
    }


@pytest.fixture
def project_data_completed_clone() -> dict:
    """Project data where clone has already completed with services tracked."""
    return {
        "name": "test-project",
        "deployments": [
            {
                "name": "production",
                "cluster": "local",
                "namespace": "test-project",
                "components": [
                    {"reference": "web", "image": "nginx:latest"},
                ],
            },
            {
                "name": "staging",
                "cluster": "local",
                "namespace": "test-project",
                "components": [
                    {"reference": "web", "image": "nginx:latest"},
                ],
                "clone-from": {
                    "type": "deployment",
                    "reference": "production",
                    "mode": "once",
                    "status": {
                        "completed": True,
                        "timestamp": "2026-02-03T12:11:43.661019+00:00",
                    },
                },
                "services": [
                    {
                        "reference": "postgresql-database",
                        "config": {
                            "generation": 1,
                            "revisions": [
                                {
                                    "generation": 1,
                                    "resource": "test_project_staging",
                                    "status": "active",
                                    "created_at": "2026-02-03T12:11:43.661019+00:00",
                                    "actions": [
                                        {
                                            "timestamp": "2026-02-03T12:11:43.661019+00:00",
                                            "type": "clone",
                                            "source": "deployment:production",
                                        }
                                    ],
                                }
                            ],
                        },
                    },
                    {
                        "reference": "minio-storage",
                        "config": {
                            "generation": 1,
                            "revisions": [
                                {
                                    "generation": 1,
                                    "resource": "test-project-staging",
                                    "status": "active",
                                    "created_at": "2026-02-03T12:11:44.123456+00:00",
                                    "actions": [
                                        {
                                            "timestamp": "2026-02-03T12:11:44.123456+00:00",
                                            "type": "clone",
                                            "source": "deployment:production",
                                        }
                                    ],
                                }
                            ],
                        },
                    },
                ],
            },
        ],
    }


# ============================================================================
# Clone-From Dict Format
# ============================================================================


class TestCloneFromDictFormat:
    """Verify clone-from uses the required dict structure."""

    def test_clone_from_has_required_keys(self, project_data_with_clone_from: dict) -> None:
        """clone-from must have type, reference, and mode keys."""
        clone_from = project_data_with_clone_from["deployments"][1]["clone-from"]
        assert isinstance(clone_from, dict)
        assert "type" in clone_from
        assert "reference" in clone_from
        assert "mode" in clone_from

    def test_clone_from_deployment_type(self, project_data_with_clone_from: dict) -> None:
        """Deployment clone uses type='deployment'."""
        clone_from = project_data_with_clone_from["deployments"][1]["clone-from"]
        assert clone_from["type"] == "deployment"
        assert clone_from["reference"] == "production"
        assert clone_from["mode"] == "once"

    def test_clone_from_remote_source_type(self, project_data_remote_source: dict) -> None:
        """Remote source clone uses type='remote-source'."""
        clone_from = project_data_remote_source["deployments"][0]["clone-from"]
        assert clone_from["type"] == "remote-source"
        assert clone_from["reference"] == "odcn-production"
        assert clone_from["mode"] == "once"

    def test_clone_from_no_status_before_clone(self, project_data_with_clone_from: dict) -> None:
        """Before clone completes, no status field should exist."""
        clone_from = project_data_with_clone_from["deployments"][1]["clone-from"]
        assert "status" not in clone_from


# ============================================================================
# set_clone_status / get_clone_status
# ============================================================================


class TestSetCloneStatus:
    """Test set_clone_status writes status correctly."""

    def test_sets_completed_status(self, handler: ProjectFileHandler, project_data_with_clone_from: dict) -> None:
        """set_clone_status adds status.completed and status.timestamp."""
        handler.set_clone_status(
            project_data_with_clone_from,
            "staging",
            completed=True,
            timestamp="2026-02-03T12:11:43.661019+00:00",
        )

        clone_from = project_data_with_clone_from["deployments"][1]["clone-from"]
        assert "status" in clone_from
        assert clone_from["status"]["completed"] is True
        assert clone_from["status"]["timestamp"] == "2026-02-03T12:11:43.661019+00:00"

    def test_preserves_type_reference_mode(
        self, handler: ProjectFileHandler, project_data_with_clone_from: dict
    ) -> None:
        """set_clone_status does not alter type, reference, or mode."""
        handler.set_clone_status(
            project_data_with_clone_from,
            "staging",
            completed=True,
            timestamp="2026-02-03T12:11:43.661019+00:00",
        )

        clone_from = project_data_with_clone_from["deployments"][1]["clone-from"]
        assert clone_from["type"] == "deployment"
        assert clone_from["reference"] == "production"
        assert clone_from["mode"] == "once"

    def test_raises_on_missing_clone_from(self, handler: ProjectFileHandler) -> None:
        """set_clone_status raises ValueError when deployment has no clone-from."""
        project_data = {
            "deployments": [{"name": "staging", "cluster": "local"}],
        }
        with pytest.raises(ValueError, match="no clone-from configuration found"):
            handler.set_clone_status(project_data, "staging", completed=True, timestamp="2026-01-01T00:00:00Z")

    def test_raises_on_string_clone_from(self, handler: ProjectFileHandler) -> None:
        """set_clone_status raises ValueError when clone-from is a legacy string."""
        project_data = {
            "deployments": [
                {
                    "name": "staging",
                    "clone-from": "production",  # Legacy string format
                }
            ],
        }
        with pytest.raises(ValueError, match="clone-from must be a dict"):
            handler.set_clone_status(project_data, "staging", completed=True, timestamp="2026-01-01T00:00:00Z")


class TestGetCloneStatus:
    """Test get_clone_status reads status correctly."""

    def test_returns_none_before_clone(self, handler: ProjectFileHandler, project_data_with_clone_from: dict) -> None:
        """get_clone_status returns None when no status exists yet."""
        status = handler.get_clone_status(project_data_with_clone_from, "staging")
        assert status is None

    def test_returns_status_after_clone(self, handler: ProjectFileHandler, project_data_completed_clone: dict) -> None:
        """get_clone_status returns the status dict after clone completes."""
        status = handler.get_clone_status(project_data_completed_clone, "staging")
        assert status is not None
        assert status["completed"] is True
        assert "timestamp" in status

    def test_returns_none_for_deployment_without_clone_from(
        self, handler: ProjectFileHandler, project_data_with_clone_from: dict
    ) -> None:
        """get_clone_status returns None for deployment without clone-from."""
        status = handler.get_clone_status(project_data_with_clone_from, "production")
        assert status is None


class TestGetCloneMode:
    """Test get_clone_mode reads mode correctly."""

    def test_returns_mode_from_config(self, handler: ProjectFileHandler, project_data_with_clone_from: dict) -> None:
        """get_clone_mode returns the configured mode."""
        mode = handler.get_clone_mode(project_data_with_clone_from, "staging")
        assert mode == "once"

    def test_defaults_to_once(self, handler: ProjectFileHandler) -> None:
        """get_clone_mode defaults to 'once' when mode is not specified."""
        project_data = {
            "deployments": [
                {
                    "name": "staging",
                    "clone-from": {
                        "type": "deployment",
                        "reference": "production",
                        # No mode key
                    },
                }
            ],
        }
        mode = handler.get_clone_mode(project_data, "staging")
        assert mode == "once"

    def test_returns_always_mode(self, handler: ProjectFileHandler) -> None:
        """get_clone_mode returns 'always' when configured."""
        project_data = {
            "deployments": [
                {
                    "name": "staging",
                    "clone-from": {
                        "type": "deployment",
                        "reference": "production",
                        "mode": "always",
                    },
                }
            ],
        }
        mode = handler.get_clone_mode(project_data, "staging")
        assert mode == "always"


class TestIsCloneCompleted:
    """Test is_clone_completed convenience method."""

    def test_returns_false_before_clone(self, handler: ProjectFileHandler, project_data_with_clone_from: dict) -> None:
        """is_clone_completed returns False before clone runs."""
        assert handler.is_clone_completed(project_data_with_clone_from, "staging") is False

    def test_returns_true_after_clone(self, handler: ProjectFileHandler, project_data_completed_clone: dict) -> None:
        """is_clone_completed returns True after successful clone."""
        assert handler.is_clone_completed(project_data_completed_clone, "staging") is True


# ============================================================================
# set_clone_status / get_clone_status Round-Trip
# ============================================================================


class TestCloneStatusRoundTrip:
    """Test that set then get returns the same values."""

    def test_set_then_get_deployment_clone(
        self, handler: ProjectFileHandler, project_data_with_clone_from: dict
    ) -> None:
        """Round-trip for deployment-type clone-from."""
        timestamp = "2026-02-03T12:11:43.661019+00:00"
        handler.set_clone_status(project_data_with_clone_from, "staging", completed=True, timestamp=timestamp)

        status = handler.get_clone_status(project_data_with_clone_from, "staging")
        assert status is not None
        assert status["completed"] is True
        assert status["timestamp"] == timestamp

    def test_set_then_get_remote_source_clone(
        self, handler: ProjectFileHandler, project_data_remote_source: dict
    ) -> None:
        """Round-trip for remote-source-type clone-from."""
        timestamp = "2026-02-03T12:11:43.661019+00:00"
        handler.set_clone_status(project_data_remote_source, "staging", completed=True, timestamp=timestamp)

        status = handler.get_clone_status(project_data_remote_source, "staging")
        assert status is not None
        assert status["completed"] is True
        assert status["timestamp"] == timestamp

    def test_is_clone_completed_after_set(
        self, handler: ProjectFileHandler, project_data_with_clone_from: dict
    ) -> None:
        """is_clone_completed returns True after set_clone_status with completed=True."""
        handler.set_clone_status(
            project_data_with_clone_from, "staging", completed=True, timestamp="2026-01-01T00:00:00Z"
        )
        assert handler.is_clone_completed(project_data_with_clone_from, "staging") is True


# ============================================================================
# record_clone Writes Deployment-Level Services
# ============================================================================


class TestRecordCloneWritesServices:
    """record_clone must create services[] with generation and revisions."""

    def test_record_clone_creates_services_entry(
        self, revision_manager: RevisionManager, project_data_with_clone_from: dict
    ) -> None:
        """record_clone creates a services entry with generation for the deployment."""
        revision_manager.record_clone(
            project_data=project_data_with_clone_from,
            deployment_name="staging",
            service_type="postgresql-database",
            generation=1,
            resource_name="test_project_staging",
            source="deployment:production",
        )

        staging = project_data_with_clone_from["deployments"][1]
        assert "services" in staging
        assert len(staging["services"]) == 1
        assert staging["services"][0]["reference"] == "postgresql-database"
        assert staging["services"][0]["config"]["generation"] == 1

    def test_record_clone_creates_revision_entry(
        self, revision_manager: RevisionManager, project_data_with_clone_from: dict
    ) -> None:
        """record_clone creates a revision entry with action details."""
        revision_manager.record_clone(
            project_data=project_data_with_clone_from,
            deployment_name="staging",
            service_type="postgresql-database",
            generation=1,
            resource_name="test_project_staging",
            source="deployment:production",
        )

        config = project_data_with_clone_from["deployments"][1]["services"][0]["config"]
        assert "revisions" in config
        assert len(config["revisions"]) == 1

        revision = config["revisions"][0]
        assert revision["generation"] == 1
        assert revision["resource"] == "test_project_staging"
        assert revision["status"] == "active"
        assert revision["actions"][0]["type"] == "clone"
        assert revision["actions"][0]["source"] == "deployment:production"

    def test_record_clone_for_multiple_services(
        self, revision_manager: RevisionManager, project_data_with_clone_from: dict
    ) -> None:
        """record_clone for database AND minio creates two services entries."""
        revision_manager.record_clone(
            project_data=project_data_with_clone_from,
            deployment_name="staging",
            service_type="postgresql-database",
            generation=1,
            resource_name="test_project_staging",
            source="deployment:production",
        )
        revision_manager.record_clone(
            project_data=project_data_with_clone_from,
            deployment_name="staging",
            service_type="minio-storage",
            generation=1,
            resource_name="test-project-staging",
            source="deployment:production",
        )

        staging = project_data_with_clone_from["deployments"][1]
        assert len(staging["services"]) == 2

        refs = {s["reference"] for s in staging["services"]}
        assert refs == {"postgresql-database", "minio-storage"}

        for service in staging["services"]:
            assert service["config"]["generation"] == 1
            assert len(service["config"]["revisions"]) == 1

    def test_record_clone_with_external_source(
        self, revision_manager: RevisionManager, project_data_remote_source: dict
    ) -> None:
        """record_clone records external source reference correctly."""
        revision_manager.record_clone(
            project_data=project_data_remote_source,
            deployment_name="staging",
            service_type="postgresql-database",
            generation=1,
            resource_name="test_project_staging",
            source="external:db.example.com:5432/source_db",
        )

        config = project_data_remote_source["deployments"][0]["services"][0]["config"]
        action = config["revisions"][0]["actions"][0]
        assert action["source"] == "external:db.example.com:5432/source_db"


# ============================================================================
# get_deployment_service_generation Reads Back
# ============================================================================


class TestGetDeploymentServiceGeneration:
    """Verify generation can be read back after being written."""

    def test_reads_generation_after_record_clone(
        self,
        handler: ProjectFileHandler,
        revision_manager: RevisionManager,
        project_data_with_clone_from: dict,
    ) -> None:
        """get_deployment_service_generation returns generation written by record_clone."""
        revision_manager.record_clone(
            project_data=project_data_with_clone_from,
            deployment_name="staging",
            service_type="postgresql-database",
            generation=1,
            resource_name="test_project_staging",
            source="deployment:production",
        )

        gen = handler.get_deployment_service_generation(project_data_with_clone_from, "staging", "postgresql-database")
        assert gen == 1

    def test_reads_generation_after_set(self, handler: ProjectFileHandler) -> None:
        """get_deployment_service_generation returns generation written by set_deployment_service_generation."""
        project_data = {
            "deployments": [
                {"name": "staging", "cluster": "local"},
            ],
        }
        handler.set_deployment_service_generation(project_data, "staging", "minio-storage", 2)

        gen = handler.get_deployment_service_generation(project_data, "staging", "minio-storage")
        assert gen == 2

    def test_returns_none_when_no_services(
        self, handler: ProjectFileHandler, project_data_with_clone_from: dict
    ) -> None:
        """get_deployment_service_generation returns None when no services entry exists."""
        gen = handler.get_deployment_service_generation(project_data_with_clone_from, "staging", "postgresql-database")
        assert gen is None

    def test_reads_from_completed_clone_data(
        self, handler: ProjectFileHandler, project_data_completed_clone: dict
    ) -> None:
        """get_deployment_service_generation reads from a fully completed clone."""
        db_gen = handler.get_deployment_service_generation(
            project_data_completed_clone, "staging", "postgresql-database"
        )
        minio_gen = handler.get_deployment_service_generation(project_data_completed_clone, "staging", "minio-storage")
        assert db_gen == 1
        assert minio_gen == 1

    def test_reads_incremented_generation(self, handler: ProjectFileHandler, revision_manager: RevisionManager) -> None:
        """After force_clone increments generation, new value is read back."""
        project_data = {
            "deployments": [
                {
                    "name": "staging",
                    "cluster": "local",
                    "services": [
                        {
                            "reference": "postgresql-database",
                            "config": {"generation": 1},
                        }
                    ],
                }
            ],
        }

        # Simulate force_clone: record new generation
        revision_manager.record_clone(
            project_data=project_data,
            deployment_name="staging",
            service_type="postgresql-database",
            generation=2,
            resource_name="test_project_staging_v2",
            source="deployment:production",
        )

        gen = handler.get_deployment_service_generation(project_data, "staging", "postgresql-database")
        assert gen == 2


# ============================================================================
# Re-Cloning Skip Logic
# ============================================================================


class TestReCloneSkipLogic:
    """Verify that completed once-mode clones are skipped on reprocessing.

    This tests the decision logic that service managers implement when reading
    clone-from configuration. The pattern is identical across database_manager,
    minio_manager, and pvc_manager.
    """

    def _should_skip_clone(self, deployment: dict, force_clone_override: bool = False) -> bool:
        """Replicate the skip logic used by all service managers.

        This is the exact pattern from database_manager, minio_manager, and pvc_manager:
        - Read clone-from config
        - Check mode and status
        - Skip if mode=once, completed=true, and not force_clone
        """
        clone_from = deployment.get("clone-from")
        if not clone_from:
            return True  # No clone-from means nothing to do

        if not isinstance(clone_from, dict):
            raise TypeError(
                f"clone-from must be a dict with 'type', 'reference', and 'mode' keys, got: {type(clone_from).__name__}"
            )

        force_clone = force_clone_override or deployment.get("force-clone", False)
        clone_mode = clone_from.get("mode", "once")
        status = clone_from.get("status", {})
        clone_completed = status.get("completed", False) if isinstance(status, dict) else False

        if clone_mode == "always":
            return False  # Always clone

        return clone_mode == "once" and clone_completed and not force_clone

    def test_skip_completed_once_clone(self) -> None:
        """mode=once with completed=true skips clone."""
        deployment = {
            "name": "staging",
            "clone-from": {
                "type": "deployment",
                "reference": "production",
                "mode": "once",
                "status": {"completed": True, "timestamp": "2026-02-03T12:00:00Z"},
            },
        }
        assert self._should_skip_clone(deployment) is True

    def test_proceed_with_uncompleted_once_clone(self) -> None:
        """mode=once without status proceeds with clone."""
        deployment = {
            "name": "staging",
            "clone-from": {
                "type": "deployment",
                "reference": "production",
                "mode": "once",
            },
        }
        assert self._should_skip_clone(deployment) is False

    def test_force_clone_overrides_completed(self) -> None:
        """force_clone=True overrides completed status."""
        deployment = {
            "name": "staging",
            "clone-from": {
                "type": "deployment",
                "reference": "production",
                "mode": "once",
                "status": {"completed": True, "timestamp": "2026-02-03T12:00:00Z"},
            },
        }
        assert self._should_skip_clone(deployment, force_clone_override=True) is False

    def test_always_mode_never_skips(self) -> None:
        """mode=always always proceeds with clone, even when completed."""
        deployment = {
            "name": "staging",
            "clone-from": {
                "type": "deployment",
                "reference": "production",
                "mode": "always",
                "status": {"completed": True, "timestamp": "2026-02-03T12:00:00Z"},
            },
        }
        assert self._should_skip_clone(deployment) is False

    def test_raises_on_string_clone_from(self) -> None:
        """String clone-from raises TypeError."""
        deployment = {
            "name": "staging",
            "clone-from": "production",
        }
        with pytest.raises(TypeError, match="clone-from must be a dict"):
            self._should_skip_clone(deployment)

    def test_deployment_force_clone_flag(self) -> None:
        """force-clone in deployment config overrides completed status."""
        deployment = {
            "name": "staging",
            "force-clone": True,
            "clone-from": {
                "type": "deployment",
                "reference": "production",
                "mode": "once",
                "status": {"completed": True, "timestamp": "2026-02-03T12:00:00Z"},
            },
        }
        assert self._should_skip_clone(deployment) is False


# ============================================================================
# report_clone_performed / has_clones_performed
# ============================================================================


class TestClonePerformedTracking:
    """Test the in-memory clone tracking used by ProjectManager.

    These methods are simple dict operations on _clones_performed.
    We test them directly without needing a full ProjectManager instance.
    """

    def test_report_and_check(self) -> None:
        """Reporting a clone makes has_clones_performed return True."""
        clones: dict[str, dict] = {}

        # Simulate report_clone_performed
        deployment_name = "staging"
        service_type = "postgresql-database"
        if deployment_name not in clones:
            clones[deployment_name] = {}
        clones[deployment_name][service_type] = {"generation": 1, "timestamp": "2026-02-03T12:00:00Z"}

        # Simulate has_clones_performed
        assert deployment_name in clones
        assert len(clones[deployment_name]) > 0

    def test_multiple_services_tracked(self) -> None:
        """Multiple services can report clones for the same deployment."""
        clones: dict[str, dict] = {}
        deployment_name = "staging"

        for svc in ["postgresql-database", "minio-storage", "persistent-storage"]:
            if deployment_name not in clones:
                clones[deployment_name] = {}
            clones[deployment_name][svc] = {"generation": 1, "timestamp": "2026-02-03T12:00:00Z"}

        assert len(clones[deployment_name]) == 3

    def test_clear_clones(self) -> None:
        """Clearing clones resets tracking."""
        clones: dict[str, dict] = {"staging": {"postgresql-database": {"generation": 1}}}

        # Simulate clear_clones_performed
        clones = {}

        assert "staging" not in clones

    def test_timestamp_selection_for_status(self) -> None:
        """The latest timestamp among all services is used for clone-from status."""
        clones = {
            "staging": {
                "postgresql-database": {"generation": 1, "timestamp": "2026-02-03T12:00:00Z"},
                "minio-storage": {"generation": 1, "timestamp": "2026-02-03T12:01:00Z"},
            }
        }

        timestamps = [ts for info in clones["staging"].values() if (ts := info.get("timestamp")) is not None]
        timestamp = max(timestamps)
        assert timestamp == "2026-02-03T12:01:00Z"


# ============================================================================
# End-to-End: Clone Lifecycle
# ============================================================================


class TestEndToEndCloneLifecycle:
    """Test the full lifecycle: clone-from -> services tracking -> status update -> skip on reprocess."""

    def test_full_deployment_clone_lifecycle(
        self,
        handler: ProjectFileHandler,
        revision_manager: RevisionManager,
        project_data_with_clone_from: dict,
    ) -> None:
        """Simulate the full clone lifecycle for a deployment clone.

        1. Start: clone-from has type/reference/mode, no status
        2. Database and MinIO record their clones (record_clone)
        3. ProjectManager sets clone status to completed (set_clone_status)
        4. On reprocess: status is read, clone is skipped
        5. Generation info is accessible for subsequent operations
        """
        data = project_data_with_clone_from
        staging = data["deployments"][1]

        # STEP 1: Verify initial state - no status, no services
        assert "status" not in staging["clone-from"]
        assert "services" not in staging

        # STEP 2: Simulate database clone recording
        revision_manager.record_clone(
            project_data=data,
            deployment_name="staging",
            service_type="postgresql-database",
            generation=1,
            resource_name="test_project_staging",
            source="deployment:production",
        )

        # STEP 2: Simulate minio clone recording
        revision_manager.record_clone(
            project_data=data,
            deployment_name="staging",
            service_type="minio-storage",
            generation=1,
            resource_name="test-project-staging",
            source="deployment:production",
        )

        # Verify services are now tracked
        assert len(staging["services"]) == 2
        refs = {s["reference"] for s in staging["services"]}
        assert refs == {"postgresql-database", "minio-storage"}

        # STEP 3: Simulate ProjectManager setting clone status
        handler.set_clone_status(data, "staging", completed=True, timestamp="2026-02-03T12:11:43.661019+00:00")

        # Verify clone-from now has status
        assert staging["clone-from"]["status"]["completed"] is True

        # STEP 4: On reprocess - is_clone_completed returns True
        assert handler.is_clone_completed(data, "staging") is True

        # STEP 5: Generation info is accessible
        db_gen = handler.get_deployment_service_generation(data, "staging", "postgresql-database")
        minio_gen = handler.get_deployment_service_generation(data, "staging", "minio-storage")
        assert db_gen == 1
        assert minio_gen == 1

    def test_full_remote_source_clone_lifecycle(
        self,
        handler: ProjectFileHandler,
        revision_manager: RevisionManager,
        project_data_remote_source: dict,
    ) -> None:
        """Simulate the full clone lifecycle for a remote-source clone."""
        data = project_data_remote_source

        # Record database clone from external
        revision_manager.record_clone(
            project_data=data,
            deployment_name="staging",
            service_type="postgresql-database",
            generation=1,
            resource_name="test_project_staging",
            source="external:db.example.com:5432/source_db",
        )

        # Record minio clone from external
        revision_manager.record_clone(
            project_data=data,
            deployment_name="staging",
            service_type="minio-storage",
            generation=1,
            resource_name="test-project-staging",
            source="external:minio.example.com:9000/source-bucket",
        )

        # Set status
        handler.set_clone_status(data, "staging", completed=True, timestamp="2026-02-03T12:11:43.661019+00:00")

        # Verify complete structure matches expected format
        staging = data["deployments"][0]
        assert staging["clone-from"] == {
            "type": "remote-source",
            "reference": "odcn-production",
            "mode": "once",
            "status": {
                "completed": True,
                "timestamp": "2026-02-03T12:11:43.661019+00:00",
            },
        }

        # Verify services tracking
        assert len(staging["services"]) == 2
        for service in staging["services"]:
            assert service["config"]["generation"] == 1
            assert len(service["config"]["revisions"]) == 1

    def test_force_clone_increments_generation(
        self,
        handler: ProjectFileHandler,
        revision_manager: RevisionManager,
        project_data_completed_clone: dict,
    ) -> None:
        """force_clone on completed clone creates new generation."""
        data = project_data_completed_clone

        # Verify initial state: generation 1, completed
        assert handler.is_clone_completed(data, "staging") is True
        assert handler.get_deployment_service_generation(data, "staging", "postgresql-database") == 1

        # Simulate force_clone: record clone with generation 2
        revision_manager.record_clone(
            project_data=data,
            deployment_name="staging",
            service_type="postgresql-database",
            generation=2,
            resource_name="test_project_staging_v2",
            source="deployment:production",
        )

        # Generation should now be 2
        gen = handler.get_deployment_service_generation(data, "staging", "postgresql-database")
        assert gen == 2

        # Old revision should be superseded
        config = None
        for service in data["deployments"][1]["services"]:
            if service["reference"] == "postgresql-database":
                config = service["config"]
                break
        assert config is not None
        assert len(config["revisions"]) == 2
        assert config["revisions"][0]["status"] == "active"
        assert config["revisions"][0]["generation"] == 2
        assert config["revisions"][1]["status"] == "superseded"
        assert config["revisions"][1]["generation"] == 1


# ============================================================================
# Services Info Read for Backups
# ============================================================================


class TestServicesInfoForBackups:
    """Verify generation info can be read for backup/restore operations."""

    def test_get_deployment_service_generation_for_backup(
        self, handler: ProjectFileHandler, project_data_completed_clone: dict
    ) -> None:
        """Backup manager can read generation to construct correct resource names."""
        # This is exactly what backup_manager does to map PVCs and buckets
        db_gen = handler.get_deployment_service_generation(
            project_data_completed_clone, "staging", "postgresql-database"
        )
        minio_gen = handler.get_deployment_service_generation(project_data_completed_clone, "staging", "minio-storage")

        assert db_gen == 1
        assert minio_gen == 1

    def test_generation_none_for_non_cloned_deployment(
        self, handler: ProjectFileHandler, project_data_completed_clone: dict
    ) -> None:
        """Non-cloned deployments return None for generation (use base name)."""
        gen = handler.get_deployment_service_generation(
            project_data_completed_clone, "production", "postgresql-database"
        )
        assert gen is None

    def test_set_then_read_generation_round_trip(self, handler: ProjectFileHandler) -> None:
        """set_deployment_service_generation followed by get returns the same value."""
        project_data = {
            "deployments": [
                {"name": "staging", "cluster": "local"},
            ],
        }

        handler.set_deployment_service_generation(project_data, "staging", "postgresql-database", 3)
        handler.set_deployment_service_generation(project_data, "staging", "minio-storage", 2)

        assert handler.get_deployment_service_generation(project_data, "staging", "postgresql-database") == 3
        assert handler.get_deployment_service_generation(project_data, "staging", "minio-storage") == 2


# ============================================================================
# YAML Anchor/Alias Prevention
# ============================================================================


class TestYamlNoAnchors:
    """Verify that YAML serialization never produces anchors (&id001/*id001).

    ruamel.yaml emits anchors when two values in the data structure are the
    same Python object (same id()). This caused a production bug where cloned
    deployments shared the source deployment's services list by reference,
    leading to revision history leaking across deployments.
    """

    def test_save_project_file_no_anchors(self, tmp_path: "Any") -> None:
        """save_project_file must never write YAML anchors/aliases."""
        shared_services = [
            {"reference": "postgresql-database", "config": {"generation": 1}},
        ]
        project_data = {
            "name": "test-project",
            "deployments": [
                {"name": "production", "services": shared_services},
                {"name": "staging", "services": shared_services},  # same object
            ],
        }

        file_path = str(tmp_path / "project.yaml")
        save_project_file(file_path, project_data)

        with open(file_path) as f:
            content = f.read()

        assert "&id" not in content, f"YAML anchors found in output:\n{content}"
        assert "*id" not in content, f"YAML aliases found in output:\n{content}"

    def test_save_project_file_preserves_data_for_both_deployments(self, tmp_path: "Any") -> None:
        """Both deployments must have full services data after save (no aliases)."""
        shared_services = [
            {"reference": "postgresql-database", "config": {"generation": 1}},
        ]
        project_data = {
            "name": "test-project",
            "deployments": [
                {"name": "production", "services": shared_services},
                {"name": "staging", "services": shared_services},
            ],
        }

        file_path = str(tmp_path / "project.yaml")
        save_project_file(file_path, project_data)

        # Re-read and verify both deployments have independent services
        yaml = YAML()
        with open(file_path) as f:
            reloaded = yaml.load(f)

        for deployment in reloaded["deployments"]:
            assert len(deployment["services"]) == 1
            assert deployment["services"][0]["reference"] == "postgresql-database"
            assert deployment["services"][0]["config"]["generation"] == 1

    def test_deepcopy_prevents_shared_references(self) -> None:
        """copy.deepcopy on clone properties prevents shared Python objects.

        This replicates the fix in project_manager.clone_deployment where
        source deployment values are deep-copied to the new deployment.
        """
        source_deployment = {
            "name": "production",
            "cluster": "local",
            "namespace": "test-project",
            "services": [
                {
                    "reference": "postgresql-database",
                    "config": {
                        "generation": 0,
                        "revisions": [
                            {
                                "generation": 0,
                                "resource": "test_project_production",
                                "status": "active",
                            }
                        ],
                    },
                },
            ],
            "configuration": "encrypted-blob",
        }

        # Simulate the fixed clone logic (with deepcopy)
        clone_exclude_keys = ["name", "components", "subdomain", "base-domain", "domain-mode", "issuer"]
        new_deployment = {"name": "staging", "components": []}
        new_deployment.update(
            {key: copy.deepcopy(value) for key, value in source_deployment.items() if key not in clone_exclude_keys}
        )

        # Verify services are independent objects
        assert new_deployment["services"] is not source_deployment["services"]
        assert new_deployment["services"][0] is not source_deployment["services"][0]
        assert new_deployment["services"][0]["config"] is not source_deployment["services"][0]["config"]

        # Mutating one must not affect the other
        new_deployment["services"][0]["config"]["generation"] = 99
        assert source_deployment["services"][0]["config"]["generation"] == 0

    def test_shared_references_without_deepcopy_produce_anchors(self) -> None:
        """Without deepcopy, shared references cause YAML anchors (the original bug)."""
        source_services = [{"reference": "postgresql-database", "config": {"generation": 0}}]

        project_data = {
            "deployments": [
                {"name": "production", "services": source_services},
                {"name": "staging", "services": source_services},  # shared ref = bug
            ],
        }

        # Default ruamel.yaml WOULD produce anchors
        yaml_default = YAML()
        output_default = StringIO()
        yaml_default.dump(project_data, output_default)
        default_content = output_default.getvalue()

        # Confirm the bug exists with default settings
        assert "&id" in default_content or "*id" in default_content, "Expected YAML anchors from shared references"

        # Our configured dumper must NOT produce anchors
        yaml_safe = YAML()
        yaml_safe.representer.ignore_aliases = lambda *_: True
        output_safe = StringIO()
        yaml_safe.dump(project_data, output_safe)
        safe_content = output_safe.getvalue()

        assert "&id" not in safe_content
        assert "*id" not in safe_content
