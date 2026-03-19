"""Tests for schema migration framework."""

import copy

from opi.services.schema_migration import (
    LATEST_SCHEMA_VERSION,
    detect_schema_version,
    migrate_to_latest,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _v1_project_simple() -> dict:
    """V1 project with plain uses-services, no storage."""
    return {
        "name": "simple-project",
        "services": ["publish-on-web", "keycloak", "postgresql-database"],
        "components": [
            {
                "name": "frontend",
                "type": "deployment",
                "uses-services": ["publish-on-web", "keycloak", "postgresql-database"],
            }
        ],
        "deployments": [
            {
                "name": "staging",
                "components": [{"reference": "frontend", "image": "app:latest"}],
            }
        ],
    }


def _v1_project_with_storage() -> dict:
    """V1 project with persistent and ephemeral storage."""
    return {
        "name": "storage-project",
        "services": [
            "publish-on-web",
            "persistent-storage",
            "temp-storage",
            "postgresql-database",
        ],
        "components": [
            {
                "name": "frontend",
                "type": "deployment",
                "uses-services": [
                    "publish-on-web",
                    "persistent-storage",
                    "temp-storage",
                    "postgresql-database",
                ],
                "storage": [
                    {
                        "name": "data",
                        "type": "persistent",
                        "size": "250Mi",
                        "mount-path": "/data",
                    },
                    {
                        "name": "temp",
                        "type": "ephemeral",
                        "size": "250Mi",
                        "mount-path": "/tmp",
                    },
                ],
            }
        ],
    }


def _v1_project_with_dict_service() -> dict:
    """V1 project with a dict entry in uses-services (hwmaw-ovh pattern)."""
    return {
        "name": "dict-service-project",
        "services": [
            "publish-on-web",
            "keycloak",
            {"authorization-wall": {"config": {"banner": "Welcome!"}}},
        ],
        "components": [
            {
                "name": "component-1",
                "type": "single",
                "uses-services": [
                    "publish-on-web",
                    "keycloak",
                    {"authorization-wall": {"config": {"banner": "Welcome!"}}},
                ],
            }
        ],
    }


def _v1_project_with_helm_chart() -> dict:
    """V1 project with a helm-chart using uses-services."""
    return {
        "name": "helm-project",
        "services": ["publish-on-web", "postgresql-database"],
        "helm-charts": [
            {
                "name": "my-chart",
                "uses-services": ["publish-on-web", "postgresql-database"],
                "chart": "my-chart",
            }
        ],
        "components": [],
    }


def _v1_project_with_helmfile() -> dict:
    """V1 project with a helmfile using uses-services."""
    return {
        "name": "helmfile-project",
        "services": ["publish-on-web", "redis"],
        "helmfile": [
            {
                "name": "my-helmfile",
                "uses-services": ["publish-on-web", "redis"],
            }
        ],
        "components": [],
    }


def _v1_project_empty_storage() -> dict:
    """V1 project with empty storage: [] on component."""
    return {
        "name": "empty-storage-project",
        "services": ["publish-on-web"],
        "components": [
            {
                "name": "frontend",
                "type": "deployment",
                "uses-services": ["publish-on-web"],
                "storage": [],
            }
        ],
    }


def _v1_project_persistent_only() -> dict:
    """V1 project with only persistent storage (no temp)."""
    return {
        "name": "persistent-only",
        "services": ["publish-on-web", "persistent-storage"],
        "components": [
            {
                "name": "upload",
                "type": "deployment",
                "uses-services": ["publish-on-web", "persistent-storage"],
                "storage": [
                    {
                        "name": "data",
                        "type": "persistent",
                        "size": "500Mi",
                        "mount-path": "/data",
                    },
                ],
            }
        ],
    }


def _v2_project() -> dict:
    """Already-migrated v2 project."""
    return {
        "schema-version": 2,
        "name": "v2-project",
        "services": ["publish-on-web", "keycloak"],
        "components": [
            {
                "name": "frontend",
                "type": "deployment",
                "services": ["publish-on-web", "keycloak"],
            }
        ],
    }


# ---------------------------------------------------------------------------
# detect_schema_version
# ---------------------------------------------------------------------------


class TestDetectSchemaVersion:
    def test_detect_from_field(self):
        assert detect_schema_version({"schema-version": 2}) == 2

    def test_detect_from_field_higher(self):
        assert detect_schema_version({"schema-version": 5}) == 5

    def test_detect_v1_from_component_uses_services(self):
        data = _v1_project_simple()
        assert detect_schema_version(data) == 1

    def test_detect_v1_from_helm_chart(self):
        data = _v1_project_with_helm_chart()
        assert detect_schema_version(data) == 1

    def test_detect_v1_from_helmfile(self):
        data = _v1_project_with_helmfile()
        assert detect_schema_version(data) == 1

    def test_detect_v2_no_indicators(self):
        """Project with no uses-services and no schema-version defaults to latest (v2)."""
        assert detect_schema_version({"name": "bare", "components": []}) == LATEST_SCHEMA_VERSION

    def test_detect_v2_from_field(self):
        data = _v2_project()
        assert detect_schema_version(data) == 2


# ---------------------------------------------------------------------------
# migrate_to_latest - no-op cases
# ---------------------------------------------------------------------------


class TestMigrateToLatestNoOp:
    def test_already_v2(self):
        data = _v2_project()
        original = copy.deepcopy(data)
        result, was_migrated = migrate_to_latest(data)
        assert was_migrated is False
        assert result == original

    def test_higher_version_no_op(self):
        data = {"schema-version": 99, "name": "future"}
        result, was_migrated = migrate_to_latest(data)
        assert was_migrated is False


# ---------------------------------------------------------------------------
# migrate_to_latest - v1 → v2
# ---------------------------------------------------------------------------


class TestMigrateV1ToV2:
    def test_simple_services_rename(self):
        data = _v1_project_simple()
        result, was_migrated = migrate_to_latest(data)

        assert was_migrated is True
        assert result["schema-version"] == LATEST_SCHEMA_VERSION

        comp = result["components"][0]
        assert "uses-services" not in comp
        assert "storage" not in comp
        assert comp["services"] == ["publish-on-web", "keycloak", "postgresql-database"]

    def test_storage_merged_into_services(self):
        data = _v1_project_with_storage()
        result, was_migrated = migrate_to_latest(data)

        assert was_migrated is True
        comp = result["components"][0]
        assert "uses-services" not in comp
        assert "storage" not in comp

        services = comp["services"]
        assert services[0] == "publish-on-web"

        # persistent-storage should be a dict with config
        persistent = services[1]
        assert isinstance(persistent, dict)
        assert "persistent-storage" in persistent
        config = persistent["persistent-storage"]["config"]
        assert len(config) == 1
        assert config[0]["name"] == "data"
        assert config[0]["size"] == "250Mi"
        assert config[0]["mount-path"] == "/data"
        assert "type" not in config[0]  # type field dropped

        # temp-storage should be a dict with config
        temp = services[2]
        assert isinstance(temp, dict)
        assert "temp-storage" in temp
        config = temp["temp-storage"]["config"]
        assert len(config) == 1
        assert config[0]["name"] == "temp"
        assert config[0]["size"] == "250Mi"
        assert config[0]["mount-path"] == "/tmp"
        assert "type" not in config[0]

        # postgresql-database stays as string
        assert services[3] == "postgresql-database"

    def test_dict_services_preserved(self):
        data = _v1_project_with_dict_service()
        result, was_migrated = migrate_to_latest(data)

        assert was_migrated is True
        comp = result["components"][0]
        services = comp["services"]

        assert services[0] == "publish-on-web"
        assert services[1] == "keycloak"
        # Dict entry preserved
        assert isinstance(services[2], dict)
        assert "authorization-wall" in services[2]
        assert services[2]["authorization-wall"]["config"]["banner"] == "Welcome!"

    def test_helm_chart_rename(self):
        data = _v1_project_with_helm_chart()
        result, was_migrated = migrate_to_latest(data)

        assert was_migrated is True
        chart = result["helm-charts"][0]
        assert "uses-services" not in chart
        assert chart["services"] == ["publish-on-web", "postgresql-database"]

    def test_helmfile_rename(self):
        data = _v1_project_with_helmfile()
        result, was_migrated = migrate_to_latest(data)

        assert was_migrated is True
        hf = result["helmfile"][0]
        assert "uses-services" not in hf
        assert hf["services"] == ["publish-on-web", "redis"]

    def test_empty_storage_removed(self):
        data = _v1_project_empty_storage()
        result, was_migrated = migrate_to_latest(data)

        assert was_migrated is True
        comp = result["components"][0]
        assert "storage" not in comp
        assert "uses-services" not in comp
        assert comp["services"] == ["publish-on-web"]

    def test_persistent_only_storage(self):
        data = _v1_project_persistent_only()
        result, was_migrated = migrate_to_latest(data)

        comp = result["components"][0]
        services = comp["services"]

        assert services[0] == "publish-on-web"

        persistent = services[1]
        assert isinstance(persistent, dict)
        assert "persistent-storage" in persistent
        config = persistent["persistent-storage"]["config"]
        assert len(config) == 1
        assert config[0]["name"] == "data"
        assert config[0]["size"] == "500Mi"

    def test_component_without_uses_services_preserved(self):
        """Component without uses-services should not have services wiped."""
        data = {
            "name": "bare",
            "components": [{"name": "comp", "type": "deployment", "services": ["publish-on-web"]}],
        }
        result, was_migrated = migrate_to_latest(data)

        # No v1 keys → detected as v2 → no migration
        assert was_migrated is False
        comp = result["components"][0]
        assert comp["services"] == ["publish-on-web"]

    def test_component_without_any_services_no_v1_keys(self):
        """Component with no services and no v1 keys should not be touched."""
        data = {
            "name": "bare",
            "components": [{"name": "comp", "type": "deployment"}],
        }
        result, was_migrated = migrate_to_latest(data)

        assert was_migrated is False
        comp = result["components"][0]
        assert "services" not in comp

    def test_deployments_unchanged(self):
        """Deployment structure should not be touched by migration."""
        data = _v1_project_with_storage()
        data["deployments"] = [
            {
                "name": "staging",
                "services": [{"reference": "postgresql-database", "config": {"generation": 1}}],
                "components": [
                    {
                        "reference": "frontend",
                        "services": {"persistent-storage": [{"reference": "data", "config": {"generation": 4}}]},
                    }
                ],
            }
        ]

        original_deployments = copy.deepcopy(data["deployments"])
        result, _ = migrate_to_latest(data)

        assert result["deployments"] == original_deployments

    def test_schema_version_set(self):
        data = _v1_project_simple()
        result, _ = migrate_to_latest(data)
        assert result["schema-version"] == 2

    def test_no_components_no_v1_keys_no_migration(self):
        """Project with no components and no v1 keys is detected as v2."""
        data = {"name": "empty", "services": [], "components": []}
        result, was_migrated = migrate_to_latest(data)

        assert was_migrated is False

    def test_multiple_components(self):
        """Multiple components should each be migrated independently."""
        data = {
            "name": "multi",
            "services": ["publish-on-web", "persistent-storage", "postgresql-database"],
            "components": [
                {
                    "name": "editor",
                    "uses-services": ["publish-on-web", "postgresql-database"],
                },
                {
                    "name": "upload",
                    "uses-services": ["publish-on-web", "persistent-storage", "postgresql-database"],
                    "storage": [
                        {
                            "name": "data",
                            "type": "persistent",
                            "size": "500Mi",
                            "mount-path": "/data",
                        }
                    ],
                },
            ],
        }

        result, _ = migrate_to_latest(data)

        editor = result["components"][0]
        assert editor["services"] == ["publish-on-web", "postgresql-database"]
        assert "storage" not in editor

        upload = result["components"][1]
        assert upload["services"][0] == "publish-on-web"
        persistent = upload["services"][1]
        assert isinstance(persistent, dict)
        assert "persistent-storage" in persistent
        assert upload["services"][2] == "postgresql-database"
