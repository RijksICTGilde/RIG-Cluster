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


class TestRevisionManagerRecordComponentClone:
    """Tests for record_component_clone method (component-level PVC tracking)."""

    @pytest.fixture
    def project_data_with_components(self) -> dict:
        """Create project data with component-level persistent-storage."""
        return {
            "name": "test-project",
            "deployments": [
                {
                    "name": "pr1",
                    "cluster": "local",
                    "components": [
                        {
                            "reference": "my-app",
                            "image": "my-app:latest",
                            "services": {
                                "persistent-storage": [
                                    {"reference": "data", "config": {"generation": 0}},
                                ]
                            },
                        }
                    ],
                },
                {
                    "name": "pr2",
                    "cluster": "local",
                    "clone-from": {
                        "type": "deployment",
                        "reference": "pr1",
                        "mode": "once",
                    },
                    "components": [
                        {
                            "reference": "my-app",
                            "image": "my-app:latest",
                        }
                    ],
                },
            ],
        }

    def test_records_clone_at_component_level(
        self, revision_manager: RevisionManager, project_data_with_components: dict
    ) -> None:
        """Should write revision info to component.services.persistent-storage[].config."""
        result = revision_manager.record_component_clone(
            project_data=project_data_with_components,
            deployment_name="pr2",
            component_name="my-app",
            service_type="persistent-storage",
            reference_name="data",
            generation=1,
            resource_name="test-project-pr2-my-app-data-v1",
            source="deployment:pr1",
        )

        # Navigate to component-level config
        pr2 = next(d for d in result["deployments"] if d["name"] == "pr2")
        component = next(c for c in pr2["components"] if c["reference"] == "my-app")
        storage_items = component["services"]["persistent-storage"]
        data_item = next(i for i in storage_items if i["reference"] == "data")
        config = data_item["config"]

        assert config["generation"] == 1
        assert len(config["revisions"]) == 1
        assert config["revisions"][0]["generation"] == 1
        assert config["revisions"][0]["resource"] == "test-project-pr2-my-app-data-v1"
        assert config["revisions"][0]["status"] == "active"
        assert config["revisions"][0]["actions"][0]["type"] == "clone"
        assert config["revisions"][0]["actions"][0]["source"] == "deployment:pr1"

    def test_creates_services_structure_if_missing(
        self, revision_manager: RevisionManager, project_data_with_components: dict
    ) -> None:
        """Should create the services.persistent-storage structure if component has none."""
        result = revision_manager.record_component_clone(
            project_data=project_data_with_components,
            deployment_name="pr2",
            component_name="my-app",
            service_type="persistent-storage",
            reference_name="data",
            generation=1,
            resource_name="test-project-pr2-my-app-data-v1",
            source="deployment:pr1",
        )

        pr2 = next(d for d in result["deployments"] if d["name"] == "pr2")
        component = next(c for c in pr2["components"] if c["reference"] == "my-app")

        # Services structure should have been created
        assert "services" in component
        assert "persistent-storage" in component["services"]
        storage_items = component["services"]["persistent-storage"]
        assert len(storage_items) == 1
        assert storage_items[0]["reference"] == "data"

    def test_returns_unchanged_for_unknown_deployment(
        self, revision_manager: RevisionManager, project_data_with_components: dict
    ) -> None:
        """Should return project_data unchanged if deployment not found."""
        result = revision_manager.record_component_clone(
            project_data=project_data_with_components,
            deployment_name="nonexistent",
            component_name="my-app",
            service_type="persistent-storage",
            reference_name="data",
            generation=1,
            resource_name="pvc-name",
            source="deployment:pr1",
        )

        assert result == project_data_with_components

    def test_returns_unchanged_for_unknown_component(
        self, revision_manager: RevisionManager, project_data_with_components: dict
    ) -> None:
        """Should return project_data unchanged if component not found."""
        result = revision_manager.record_component_clone(
            project_data=project_data_with_components,
            deployment_name="pr2",
            component_name="nonexistent-component",
            service_type="persistent-storage",
            reference_name="data",
            generation=1,
            resource_name="pvc-name",
            source="deployment:pr1",
        )

        # pr2 should not have services created for wrong component
        pr2 = next(d for d in result["deployments"] if d["name"] == "pr2")
        component = next(c for c in pr2["components"] if c["reference"] == "my-app")
        assert "services" not in component

    def test_updates_existing_config(
        self, revision_manager: RevisionManager, project_data_with_components: dict
    ) -> None:
        """Should update existing config in source deployment's component."""
        result = revision_manager.record_component_clone(
            project_data=project_data_with_components,
            deployment_name="pr1",
            component_name="my-app",
            service_type="persistent-storage",
            reference_name="data",
            generation=2,
            resource_name="test-project-pr1-my-app-data-v2",
            source="deployment:external",
        )

        pr1 = next(d for d in result["deployments"] if d["name"] == "pr1")
        component = next(c for c in pr1["components"] if c["reference"] == "my-app")
        data_item = next(i for i in component["services"]["persistent-storage"] if i["reference"] == "data")
        config = data_item["config"]

        # Generation updated from 0 to 2
        assert config["generation"] == 2
        assert len(config["revisions"]) == 1
        assert config["revisions"][0]["resource"] == "test-project-pr1-my-app-data-v2"

    def test_not_written_to_deployment_level(
        self, revision_manager: RevisionManager, project_data_with_components: dict
    ) -> None:
        """Should NOT create deployment-level services entry (that's for DB/MinIO)."""
        result = revision_manager.record_component_clone(
            project_data=project_data_with_components,
            deployment_name="pr2",
            component_name="my-app",
            service_type="persistent-storage",
            reference_name="data",
            generation=1,
            resource_name="pvc-name",
            source="deployment:pr1",
        )

        pr2 = next(d for d in result["deployments"] if d["name"] == "pr2")
        # Deployment-level services should NOT exist
        assert "services" not in pr2


class TestDeploymentServiceLookupIsFormatAgnostic:
    """A services list holds four entry forms; clone state must be found in all of them.

    ``_get_service_config`` and ``_ensure_service_entry`` used a JSONPath keyed on
    ``@.reference``, so anything written as a ``name`` record, a bare string or a legacy
    single-key dict was invisible. Production happens to write ``reference`` everywhere,
    so it worked on convention rather than on contract. The damage from a miss is not a
    crash: ``_ensure_service_entry`` appends a second entry for the same service, and a
    services list is a selection set, so ``validate_project_structure`` then rejects the
    whole file over a duplicate.
    """

    SERVICE = "postgresql-database"

    def _project(self, entry) -> dict:
        return {"name": "p", "deployments": [{"name": "productie", "services": [entry]}]}

    @pytest.mark.parametrize(
        ("label", "entry"),
        [
            ("reference record", {"reference": SERVICE, "config": {"generation": 3}}),
            ("name record", {"name": SERVICE, "config": {"generation": 3}}),
            ("legacy single-key", {SERVICE: {"config": {"generation": 3}}}),
        ],
    )
    def test_reads_config_from_every_record_form(self, revision_manager, label, entry):
        data = self._project(entry)
        config = revision_manager._get_service_config(data, "productie", self.SERVICE)
        assert config is not None, f"{label} not found"
        assert config["generation"] == 3

    @pytest.mark.parametrize(
        ("label", "entry"),
        [
            ("reference record", {"reference": SERVICE, "config": {}}),
            ("name record", {"name": SERVICE, "config": {}}),
            ("legacy single-key", {SERVICE: {"config": {}}}),
        ],
    )
    def test_ensure_reuses_an_existing_entry_instead_of_duplicating(self, revision_manager, label, entry):
        data = self._project(entry)
        config = revision_manager._ensure_service_entry(data, "productie", self.SERVICE)
        assert config is not None
        config["generation"] = 1

        services = data["deployments"][0]["services"]
        assert len(services) == 1, f"{label}: a second entry was appended, {services}"

    def test_ensure_promotes_a_bare_string_rather_than_duplicating(self, revision_manager):
        data = self._project(self.SERVICE)
        config = revision_manager._ensure_service_entry(data, "productie", self.SERVICE)
        assert config is not None
        config["generation"] = 1

        services = data["deployments"][0]["services"]
        assert len(services) == 1, f"a second entry was appended next to the bare string: {services}"

    def test_ensure_still_creates_an_entry_when_the_service_is_absent(self, revision_manager):
        data = {"name": "p", "deployments": [{"name": "productie", "services": []}]}
        config = revision_manager._ensure_service_entry(data, "productie", self.SERVICE)
        assert config is not None
        assert len(data["deployments"][0]["services"]) == 1


class TestRevisionQueriesAreFormatAgnostic:
    """The three revision queries kept their own hardcoded ``@.reference`` JSONPath.

    They are all "find the config, then filter its revisions", so they inherit the same
    blind spot: on a ``name`` record or a legacy entry they quietly answer "no revisions"
    instead of failing, which reads as a resource that was never cloned.
    """

    SERVICE = "postgresql-database"

    def _project(self) -> dict:
        return {
            "name": "p",
            "deployments": [
                {
                    "name": "productie",
                    "services": [
                        {
                            "name": self.SERVICE,  # name record, not reference
                            "config": {
                                "generation": 2,
                                "revisions": [
                                    {"generation": 1, "resource": "db_v1", "status": "superseded", "actions": []},
                                    {"generation": 2, "resource": "db_v2", "status": "active", "actions": []},
                                ],
                            },
                        }
                    ],
                }
            ],
        }

    def test_get_active_revision(self, revision_manager):
        active = revision_manager.get_active_revision(self._project(), "productie", self.SERVICE)
        assert active is not None
        assert active["resource"] == "db_v2"

    def test_get_superseded_resources(self, revision_manager):
        superseded = revision_manager.get_superseded_resources(self._project(), "productie", self.SERVICE)
        assert [r["resource"] for r in superseded] == ["db_v1"]

    def test_add_action_finds_the_revision(self, revision_manager):
        data = self._project()
        revision_manager.add_action(data, "productie", self.SERVICE, 2, "backup", source="nightly")
        revisions = revision_manager.get_revisions(data, "productie", self.SERVICE)
        active = next(r for r in revisions if r["generation"] == 2)
        assert [a["type"] for a in active["actions"]] == ["backup"]
