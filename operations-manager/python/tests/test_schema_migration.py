"""Tests for schema migration framework."""

import copy

from opi.connectors.subdomain import get_domains_config
from opi.core.project_schema import validate_declared_project_schema, validate_project_schema
from opi.manager.project_validation import STORED_PROJECT_CONTEXT
from opi.services.catalog.publish_on_web.domain_config import DomainSetting, get_domain_setting
from opi.services.schema_migration import (
    LATEST_SCHEMA_VERSION,
    detect_schema_version,
    migrate_to_latest,
    normalize_domains_location,
    normalize_service_entries,
    relocate_domain_settings_to_service,
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


def _v1_project_nameless_storage() -> dict:
    """V1 project whose storage entries carry NO name.

    Older real project files predate the ``name`` field on storage mounts. The
    v2 storage config (StorageEntry) requires a name, so the migration must
    synthesize one from the mount path.
    """
    return {
        "name": "nameless-storage",
        "services": ["persistent-storage", "temp-storage"],
        "components": [
            {
                "name": "app",
                "type": "deployment",
                "uses-services": ["persistent-storage", "temp-storage"],
                "storage": [
                    {"type": "persistent", "size": "10Gi", "mount-path": "/app/data"},
                    {"type": "ephemeral", "size": "2Gi", "mount-path": "/tmp"},
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
        assert persistent["reference"] == "persistent-storage"
        config = persistent["config"]
        assert len(config) == 1
        assert config[0]["name"] == "data"
        assert config[0]["size"] == "250Mi"
        assert config[0]["mount-path"] == "/data"
        assert "type" not in config[0]  # type field dropped

        # temp-storage should be a dict with config
        temp = services[2]
        assert isinstance(temp, dict)
        assert temp["reference"] == "temp-storage"
        config = temp["config"]
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
        # Dict entry normalized to the component reference record.
        assert isinstance(services[2], dict)
        assert services[2]["reference"] == "authorization-wall"
        assert services[2]["config"]["banner"] == "Welcome!"

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
        assert persistent["reference"] == "persistent-storage"
        config = persistent["config"]
        assert len(config) == 1
        assert config[0]["name"] == "data"
        assert config[0]["size"] == "500Mi"

    def test_nameless_v1_storage_gets_synthesized_name(self):
        """v1 storage without a name must migrate to a valid, named v2 config.

        The v2 storage config requires a name per mount; a nameless legacy entry
        would otherwise fail the config-validation gate. The synthesized name must
        match what the renderer derives from the mount path (generate_storage_name),
        and the resulting config must validate against the service's config_model.
        """
        from opi.services.registry import get_service
        from opi.services.services_enums import ServiceType

        result, was_migrated = migrate_to_latest(_v1_project_nameless_storage())

        assert was_migrated is True
        services = result["components"][0]["services"]

        persistent = next(s for s in services if isinstance(s, dict) and s["reference"] == "persistent-storage")
        assert persistent["config"][0]["name"] == "appdata"  # from /app/data
        assert persistent["config"][0]["mount-path"] == "/app/data"

        temp = next(s for s in services if isinstance(s, dict) and s["reference"] == "temp-storage")
        assert temp["config"][0]["name"] == "tmp"  # from /tmp

        # The migrated config must pass the per-service typed-config gate. With the same
        # context production uses there: this is a file that already exists, so the size
        # ceiling (which applies to what a client submits) does not judge its mounts --
        # this fixture carries the 10Gi a v1-era project really could have.
        get_service(ServiceType.PERSISTENT_STORAGE).validate_config(
            persistent["config"], context=STORED_PROJECT_CONTEXT
        )
        get_service(ServiceType.TEMP_STORAGE).validate_config(temp["config"], context=STORED_PROJECT_CONTEXT)

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
        assert result["schema-version"] == LATEST_SCHEMA_VERSION

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
        assert persistent["reference"] == "persistent-storage"
        assert upload["services"][2] == "postgresql-database"


# ---------------------------------------------------------------------------
# migrate_to_latest - v2 → v2.1 (root: true → root-component)
# ---------------------------------------------------------------------------


class TestMigrateV2ToV2_1:
    """Tests for lifting root: true from components to root-component on deployments."""

    def test_root_true_migrated_to_root_component(self):
        """Component root: true is lifted to deployment root-component."""
        data = {
            "schema-version": 2,
            "name": "test-project",
            "deployments": [
                {
                    "name": "prod",
                    "cluster": "odcn-production",
                    "namespace": "test",
                    "components": [
                        {"reference": "frontend", "image": "app:latest", "root": True},
                        {"reference": "backend", "image": "api:latest"},
                    ],
                }
            ],
        }
        result, was_migrated = migrate_to_latest(data)

        assert was_migrated is True
        assert result["schema-version"] == LATEST_SCHEMA_VERSION
        dep = result["deployments"][0]
        # v2.7 relocated it under the service, so ask the service (RC-60).
        assert get_domain_setting(dep, DomainSetting.ROOT_COMPONENT) == "frontend"
        # root: true must be removed from the component
        assert "root" not in dep["components"][0]
        assert "root" not in dep["components"][1]

    def test_existing_root_component_takes_precedence(self):
        """When both root-component and root: true exist, root-component wins."""
        data = {
            "schema-version": 2,
            "name": "test-project",
            "deployments": [
                {
                    "name": "prod",
                    "cluster": "odcn-production",
                    "namespace": "test",
                    "root-component": "landing",
                    "components": [
                        {"reference": "editor", "image": "app:latest", "root": True},
                        {"reference": "landing", "image": "landing:latest"},
                    ],
                }
            ],
        }
        result, was_migrated = migrate_to_latest(data)

        assert was_migrated is True
        dep = result["deployments"][0]
        # root-component should remain as "landing" (not overwritten by editor's root: true)
        assert get_domain_setting(dep, DomainSetting.ROOT_COMPONENT) == "landing"
        assert "root" not in dep["components"][0]

    def test_no_root_no_migration(self):
        """Deployment without root flags or root-component is untouched."""
        data = {
            "schema-version": 2,
            "name": "test-project",
            "deployments": [
                {
                    "name": "prod",
                    "cluster": "local",
                    "namespace": "test",
                    "components": [
                        {"reference": "frontend", "image": "app:latest"},
                    ],
                }
            ],
        }
        result, was_migrated = migrate_to_latest(data)

        # No root flags to clean up — nothing to migrate
        assert was_migrated is False
        dep = result["deployments"][0]
        assert get_domain_setting(dep, DomainSetting.ROOT_COMPONENT) is None

    def test_root_false_cleaned_up(self):
        """Explicit root: false on components is cleaned up."""
        data = {
            "schema-version": 2,
            "name": "test-project",
            "deployments": [
                {
                    "name": "prod",
                    "cluster": "local",
                    "namespace": "test",
                    "components": [
                        {"reference": "frontend", "image": "app:latest", "root": False},
                    ],
                }
            ],
        }
        result, was_migrated = migrate_to_latest(data)

        assert was_migrated is True
        dep = result["deployments"][0]
        assert "root" not in dep["components"][0]
        assert get_domain_setting(dep, DomainSetting.ROOT_COMPONENT) is None

    def test_already_v2_1_gets_v2_2_migration(self):
        """Files at v2.1 with string path get v2.2 migration."""
        data = {
            "schema-version": 2.1,
            "name": "test-project",
            "components": [
                {"name": "frontend", "path": "/"},
            ],
            "deployments": [
                {
                    "name": "prod",
                    "cluster": "local",
                    "namespace": "test",
                    "root-component": "frontend",
                    "components": [
                        {"reference": "frontend", "image": "app:latest"},
                    ],
                }
            ],
        }
        result, was_migrated = migrate_to_latest(data)

        assert was_migrated is True
        assert result["schema-version"] == LATEST_SCHEMA_VERSION
        assert result["components"][0]["path"] == [{"match": "/"}]

    def test_multiple_deployments(self):
        """Each deployment is migrated independently."""
        data = {
            "schema-version": 2,
            "name": "test-project",
            "deployments": [
                {
                    "name": "prod",
                    "cluster": "odcn-production",
                    "namespace": "test",
                    "components": [
                        {"reference": "frontend", "image": "app:latest", "root": True},
                        {"reference": "backend", "image": "api:latest"},
                    ],
                },
                {
                    "name": "staging",
                    "cluster": "local",
                    "namespace": "test-staging",
                    "components": [
                        {"reference": "api", "image": "api:latest", "root": True},
                    ],
                },
            ],
        }
        result, was_migrated = migrate_to_latest(data)

        assert was_migrated is True
        assert get_domain_setting(result["deployments"][0], DomainSetting.ROOT_COMPONENT) == "frontend"
        assert get_domain_setting(result["deployments"][1], DomainSetting.ROOT_COMPONENT) == "api"
        # All root flags removed
        for dep in result["deployments"]:
            for comp in dep["components"]:
                assert "root" not in comp

    def test_v1_project_also_gets_v2_1_migration(self):
        """V1 projects go through both v1→v2 and v2→v2.1."""
        data = {
            "name": "old-project",
            "components": [
                {
                    "name": "frontend",
                    "uses-services": ["publish-on-web"],
                }
            ],
            "deployments": [
                {
                    "name": "prod",
                    "cluster": "local",
                    "namespace": "old",
                    "components": [
                        {"reference": "frontend", "image": "app:latest", "root": True},
                    ],
                }
            ],
        }
        result, was_migrated = migrate_to_latest(data)

        assert was_migrated is True
        assert result["schema-version"] == LATEST_SCHEMA_VERSION
        dep = result["deployments"][0]
        # v2.7 relocated it under the service, so ask the service (RC-60).
        assert get_domain_setting(dep, DomainSetting.ROOT_COMPONENT) == "frontend"
        assert "root" not in dep["components"][0]


# ---------------------------------------------------------------------------
# migrate_to_latest - v2.1 → v2.2 (path string → list-of-dicts)
# ---------------------------------------------------------------------------


class TestMigrateV2_1ToV2_2:
    """Tests for normalizing component path to list-of-dicts format."""

    def test_string_path_converted_to_list(self):
        """Simple string path is converted to list with match key."""
        data = {
            "schema-version": 2.1,
            "name": "test-project",
            "components": [
                {"name": "frontend", "path": "/"},
            ],
            "deployments": [],
        }
        result, was_migrated = migrate_to_latest(data)

        assert was_migrated is True
        assert result["schema-version"] == LATEST_SCHEMA_VERSION
        assert result["components"][0]["path"] == [{"match": "/"}]

    def test_string_path_with_rewrite_merged(self):
        """String path + rewrite-path are merged into a single list entry."""
        data = {
            "schema-version": 2.1,
            "name": "test-project",
            "components": [
                {"name": "typesense", "path": "/typesense/", "rewrite-path": "/"},
            ],
            "deployments": [],
        }
        result, was_migrated = migrate_to_latest(data)

        assert was_migrated is True
        comp = result["components"][0]
        assert comp["path"] == [{"match": "/typesense/", "rewrite": "/"}]
        assert "rewrite-path" not in comp

    def test_already_list_format_untouched(self):
        """Path already in list-of-dicts format is not changed."""
        data = {
            "schema-version": 2.1,
            "name": "test-project",
            "components": [
                {"name": "api", "path": [{"match": "/api", "rewrite": "/"}]},
            ],
            "deployments": [],
        }
        result, was_migrated = migrate_to_latest(data)

        # No path migration needed (already list), but version bump still happens
        assert result["components"][0]["path"] == [{"match": "/api", "rewrite": "/"}]

    def test_deployment_component_string_path_converted(self):
        """Deployment-level component string path is also converted."""
        data = {
            "schema-version": 2.1,
            "name": "test-project",
            "components": [{"name": "api", "path": "/api"}],
            "deployments": [
                {
                    "name": "prod",
                    "cluster": "local",
                    "namespace": "test",
                    "components": [
                        {"reference": "api", "image": "api:latest", "path": "/v2"},
                    ],
                }
            ],
        }
        result, was_migrated = migrate_to_latest(data)

        assert was_migrated is True
        assert result["components"][0]["path"] == [{"match": "/api"}]
        assert result["deployments"][0]["components"][0]["path"] == [{"match": "/v2"}]

    def test_deployment_component_paths_plural_renamed(self):
        """Legacy plural 'paths' key on deployment components is renamed to 'path'."""
        data = {
            "schema-version": 2.1,
            "name": "test-project",
            "components": [{"name": "api"}],
            "deployments": [
                {
                    "name": "prod",
                    "cluster": "local",
                    "namespace": "test",
                    "components": [
                        {
                            "reference": "api",
                            "image": "api:latest",
                            "paths": [{"match": "/api", "rewrite": "/"}],
                        },
                    ],
                }
            ],
        }
        result, was_migrated = migrate_to_latest(data)

        assert was_migrated is True
        dep_comp = result["deployments"][0]["components"][0]
        assert "paths" not in dep_comp
        assert dep_comp["path"] == [{"match": "/api", "rewrite": "/"}]

    def test_no_path_no_migration(self):
        """Component without path field is not migrated."""
        data = {
            "schema-version": 2.1,
            "name": "test-project",
            "components": [{"name": "worker"}],
            "deployments": [],
        }
        result, was_migrated = migrate_to_latest(data)

        assert was_migrated is False
        assert "path" not in result["components"][0]

    def test_v2_2_not_migrated_again(self):
        """Files already at v2.2 are not migrated."""
        data = {
            "schema-version": 2.2,
            "name": "test-project",
            "components": [
                {"name": "api", "path": [{"match": "/api"}]},
            ],
            "deployments": [],
        }
        result, was_migrated = migrate_to_latest(data)

        assert was_migrated is False
        assert result["schema-version"] == 2.2


# ---------------------------------------------------------------------------
# v2.2 -> v2.3: relocate config.keycloak -> keycloak service config.realms (RC-5 B)
# ---------------------------------------------------------------------------


def _v22_with_keycloak_connections() -> dict:
    return {
        "schema-version": 2.2,
        "name": "wies",
        "services": [{"keycloak": {"config": {"template": "sso-only"}}}],
        "config": {
            "age-public-key": "age1x",
            "api-key": "k",
            "keycloak": [
                {
                    "host": "https://keycloak.rijksapp.nl",
                    "realm": "wies-odcn-production",
                    "username": "wies_odcn_production_admin",
                    "password": "ENCRYPTED",
                }
            ],
        },
    }


def test_v23_relocates_keycloak_connections_to_service_config():
    out, migrated = migrate_to_latest(_v22_with_keycloak_connections())
    assert migrated is True
    assert out["schema-version"] == LATEST_SCHEMA_VERSION
    # moved verbatim under the keycloak service, project-level config.keycloak gone.
    # The keycloak entry is now the normalized {name, config} record (v2.4 also runs).
    assert "keycloak" not in out["config"]
    entry = out["services"][0]
    assert entry["name"] == "keycloak"
    assert entry["config"]["realms"] == [
        {
            "host": "https://keycloak.rijksapp.nl",
            "realm": "wies-odcn-production",
            "username": "wies_odcn_production_admin",
            "password": "ENCRYPTED",
        }
    ]
    # existing keycloak config (template) preserved
    assert entry["config"]["template"] == "sso-only"


def test_v23_is_idempotent():
    once, _ = migrate_to_latest(_v22_with_keycloak_connections())
    twice, migrated_again = migrate_to_latest(copy.deepcopy(once))
    assert migrated_again is False
    assert twice["services"] == once["services"]
    assert "keycloak" not in twice["config"]


def test_v23_promotes_bare_string_keycloak_service():
    data = {
        "schema-version": 2.2,
        "name": "x",
        "services": ["keycloak"],
        "config": {"keycloak": [{"realm": "x-prod", "host": "h", "username": "u", "password": "p"}]},
    }
    out, _ = migrate_to_latest(data)
    # relocated + normalized to the {name, config} record (v2.4 also runs).
    assert out["services"] == [
        {"name": "keycloak", "config": {"realms": [{"realm": "x-prod", "host": "h", "username": "u", "password": "p"}]}}
    ]


# ---------------------------------------------------------------------------
# v2.3 -> v2.4: normalize project-level service definitions to {name, config} (RC-5 A)
# ---------------------------------------------------------------------------


def test_v24_normalizes_project_services_to_name_records():
    data = {
        "schema-version": 2.3,
        "name": "p",
        "services": [
            "publish-on-web",  # bare string stays bare
            {"keycloak": {"config": {"template": "sso-only"}}},
            {"namespace-postgresql-database": {"config": {"instances": 1}}},
        ],
    }
    out, migrated = migrate_to_latest(data)
    assert migrated is True
    assert out["schema-version"] == LATEST_SCHEMA_VERSION
    assert out["services"] == [
        "publish-on-web",
        {"name": "keycloak", "config": {"template": "sso-only"}},
        {"name": "namespace-postgresql-database", "config": {"instances": 1}},
    ]


def test_v24_is_idempotent():
    data = {"schema-version": 2.3, "name": "p", "services": [{"keycloak": {"config": {"template": "x"}}}]}
    once, _ = migrate_to_latest(data)
    twice, again = migrate_to_latest(copy.deepcopy(once))
    assert again is False
    assert twice["services"] == once["services"]


def test_v24_leaves_already_normalized_and_bare():
    data = {
        "schema-version": 2.4,
        "name": "p",
        "services": ["publish-on-web", {"name": "keycloak", "config": {"template": "x"}}],
    }
    out, _ = migrate_to_latest(copy.deepcopy(data))
    assert out["services"] == data["services"]


# ---------------------------------------------------------------------------
# v2.4: component-level services -> {reference, config} (RC-5 A2b)
# ---------------------------------------------------------------------------


def test_v24_normalizes_component_services_to_reference_records():
    data = {
        "schema-version": 2.3,
        "name": "p",
        "components": [
            {
                "name": "api",
                "services": [
                    "publish-on-web",
                    {"persistent-storage": {"config": [{"name": "data", "size": "1Gi", "mount-path": "/data"}]}},
                    {"metrics-scraper": {"port": 8000, "path": "/metrics"}},  # inline config -> wrapped
                ],
            }
        ],
    }
    out, migrated = migrate_to_latest(data)
    assert migrated is True
    assert out["components"][0]["services"] == [
        "publish-on-web",
        {"reference": "persistent-storage", "config": [{"name": "data", "size": "1Gi", "mount-path": "/data"}]},
        {"reference": "metrics-scraper", "config": {"port": 8000, "path": "/metrics"}},
    ]


def test_normalize_service_entries_standalone():
    """The public normalizer canonicalizes project + component (incl. inline metrics)
    services to the uniform record form, version-independently - this is what the
    create/wizard save path calls so new files are born current."""
    data = {
        "name": "p",
        "services": [{"keycloak": {"config": {"template": "sso-only"}}}, "publish-on-web"],
        "components": [
            {
                "name": "web",
                "services": [
                    {"persistent-storage": {"config": [{"name": "data", "mount-path": "/data"}]}},
                    {"metrics-scraper": {"port": 8080, "path": "/metrics"}},
                ],
            }
        ],
    }
    changed = normalize_service_entries(data)
    assert changed is True
    assert data["services"][0] == {"name": "keycloak", "config": {"template": "sso-only"}}
    assert data["services"][1] == "publish-on-web"
    assert data["components"][0]["services"] == [
        {"reference": "persistent-storage", "config": [{"name": "data", "mount-path": "/data"}]},
        {"reference": "metrics-scraper", "config": {"port": 8080, "path": "/metrics"}},
    ]
    # Idempotent: a second pass changes nothing.
    assert normalize_service_entries(data) is False


# ---------------------------------------------------------------------------
# v2.4 -> v2.5: relocate root domains: block under the publish-on-web service (RC-5)
# ---------------------------------------------------------------------------


def test_v25_relocates_root_domains_under_publish_on_web_service():
    """The root domains: approval block moves under the publish-on-web service config;
    the root key is removed and a bare service string is promoted to a record."""
    data = {
        "schema-version": 2.4,
        "name": "p",
        "services": ["publish-on-web"],
        "domains": {
            "allowed-domains": [{"domain": "mijn-app.nl", "status": "approved"}],
            "allowed-subdomains": [{"domain": "sandbox.dev", "subdomains": [{"name": "a", "status": "requested"}]}],
        },
    }
    out, migrated = migrate_to_latest(data)
    assert migrated is True
    assert out["schema-version"] == LATEST_SCHEMA_VERSION
    # root block is gone, relocated under the promoted publish-on-web service record
    assert "domains" not in out
    assert out["services"] == [
        {
            "name": "publish-on-web",
            "config": {
                "domains": {
                    "allowed-domains": [{"domain": "mijn-app.nl", "status": "approved"}],
                    "allowed-subdomains": [
                        {"domain": "sandbox.dev", "subdomains": [{"name": "a", "status": "requested"}]}
                    ],
                }
            },
        }
    ]
    # and the runtime resolver reads it back from the new home
    assert get_domains_config(out)["allowed-domains"][0]["domain"] == "mijn-app.nl"


def test_v25_is_idempotent_and_noop_without_domains():
    """A file with no root domains block is untouched; a second pass changes nothing."""
    assert normalize_domains_location({"name": "p", "services": ["publish-on-web"]}) is False

    data = {"schema-version": 2.4, "name": "p", "domains": {"allowed-domains": []}}
    once, _ = migrate_to_latest(data)
    twice, again = migrate_to_latest(copy.deepcopy(once))
    assert again is False
    assert get_domains_config(twice) == {"allowed-domains": []}


def test_v24_leaves_attachments_legacy():
    # attachments is the deferred hard case (project 'data' catalog + own $defs).
    data = {
        "schema-version": 2.3,
        "name": "p",
        "services": [{"attachments": {"data": [{"id": "x", "filename": "f", "content": "c"}]}}],
        "components": [
            {
                "name": "api",
                "services": [{"attachments": {"config": [{"reference": "x", "provide-as": "file", "path": "/x"}]}}],
            }
        ],
    }
    out, _ = migrate_to_latest(data)
    assert out["services"][0] == {"attachments": {"data": [{"id": "x", "filename": "f", "content": "c"}]}}
    assert out["components"][0]["services"][0] == {
        "attachments": {"config": [{"reference": "x", "provide-as": "file", "path": "/x"}]}
    }


# ---------------------------------------------------------------------------
# Invite relocation (v2.5 -> v2.6, RC-13)
# ---------------------------------------------------------------------------


def _project_with_top_level_invites(version: float = 2.5) -> dict:
    """A v2.5 project in the exact shape of the four production invite files."""
    return {
        "schema-version": version,
        "name": "asses-k2n",
        "services": ["publish-on-web", "keycloak"],
        "invites": {
            "settings": {"default_language": "nl"},
            "active": [
                {
                    "key": "invulhulpen",
                    "realm_roles": ["allowed-user"],
                    "application_url": "https://app.example.nl",
                    "contact_email": "help@example.nl",
                    "message": {"nl": "Welkom", "en": "Welcome"},
                    "success_title": {"nl": "Klaar", "en": "Done"},
                    "success_button": {"nl": "Ga", "en": "Go"},
                }
            ],
        },
    }


def test_relocate_invites_moves_the_block_and_validates():
    from opi.core.project_schema import validate_project_schema

    out, was_migrated = migrate_to_latest(_project_with_top_level_invites())
    assert was_migrated is True
    assert "invites" not in out
    assert out["schema-version"] == LATEST_SCHEMA_VERSION
    invite_service = next(s for s in out["services"] if isinstance(s, dict) and s.get("name") == "invite")
    config = invite_service["config"]
    assert config["default-language"] == "nl"
    assert config["active"][0]["key"] == "invulhulpen"
    assert config["active"][0]["realm-roles"] == ["allowed-user"]  # hyphenated on disk
    # The migrated file passes the JSON schema gate (validate raises on failure).
    validate_project_schema(out)


def test_relocate_invites_is_idempotent():
    out, _ = migrate_to_latest(_project_with_top_level_invites())
    out2, was_migrated = migrate_to_latest(copy.deepcopy(out))
    assert was_migrated is False
    assert out == out2


def test_unconditional_fixup_relocates_a_stamped_file():
    # A file already stamped at the latest version but still carrying a top-level invites block
    # (e.g. written by an old pod mid-rollout) is repaired by the always-run fixup.
    data = _project_with_top_level_invites(version=LATEST_SCHEMA_VERSION)
    out, was_migrated = migrate_to_latest(data)
    assert was_migrated is True
    assert "invites" not in out
    assert any(isinstance(s, dict) and s.get("name") == "invite" for s in out["services"])


def test_empty_invites_block_is_removed_without_a_service():
    data = {"schema-version": 2.5, "name": "x", "services": ["keycloak"], "invites": {}}
    out, _ = migrate_to_latest(data)
    assert "invites" not in out
    assert not any(isinstance(s, dict) and s.get("name") == "invite" for s in out["services"])


# ---------------------------------------------------------------------------
# Duplicate service entries (found on real production files, 3 August)
# ---------------------------------------------------------------------------


def test_duplicate_bare_service_entry_is_collapsed():
    """The shape found on dsm1j2-2ws: the same service twice as a bare string.

    ``_validate_services_listed_once`` rejects this, so such a file fails every
    reprocess with nothing the user sees: no deploys, no auto-tune, no error. That is
    the dp-bn7 failure mode, so the repair belongs in the unconditional fixup.
    """
    data = {
        "schema-version": LATEST_SCHEMA_VERSION,
        "name": "dup",
        "services": ["publish-on-web", "publish-on-web", "keycloak"],
    }
    out, was_migrated = migrate_to_latest(data)

    assert was_migrated is True
    assert out["services"] == ["publish-on-web", "keycloak"]


def test_duplicate_collapse_keeps_the_entry_carrying_config():
    """The shape found on ug-zxt after migration: one record with config, one bare.

    The bare entry says nothing the record does not already say, so the record must
    survive regardless of which came first.
    """
    record = {"name": "publish-on-web", "config": {"domains": {"allowed-domains": []}}}
    data = {
        "schema-version": LATEST_SCHEMA_VERSION,
        "name": "dup",
        "services": ["publish-on-web", record],
    }
    out, _ = migrate_to_latest(data)

    assert out["services"] == [record]


def test_two_entries_that_both_carry_config_are_left_for_the_validator():
    """Never silently pick a winner: two configs can contradict each other.

    Collapsing here could drop a user's settings without a trace, so this case stays
    untouched and ``_validate_services_listed_once`` rejects the file loudly.
    """
    first = {"name": "keycloak", "config": {"template": "sso-only"}}
    second = {"name": "keycloak", "config": {"template": "sso-support"}}
    data = {"schema-version": LATEST_SCHEMA_VERSION, "name": "dup", "services": [first, second]}

    out, _ = migrate_to_latest(data)

    assert out["services"] == [first, second]


def test_collapse_is_idempotent_and_leaves_clean_files_alone():
    clean = {"schema-version": LATEST_SCHEMA_VERSION, "name": "ok", "services": ["publish-on-web", "keycloak"]}
    out, was_migrated = migrate_to_latest(copy.deepcopy(clean))

    assert was_migrated is False
    assert out == clean


class TestRelocateDomainSettingsToService:
    """v2.6 -> v2.7: the web address moves under publish-on-web (RC-60).

    Measured migrate-then-validate, in that order, because that is the order the loader
    uses and the order dp-bn7 showed matters: a file that migrates into a shape the schema
    rejects fails silently on the next reprocess and the deploy simply stops happening.
    """

    def _hwt_nqi_shaped(self) -> dict:
        """The real split the plan describes: tls under the service, domain-format loose.

        Modelled on ``hwt-nqi.yaml`` -- the file that made this visible -- rather than on a
        minimal deployment, so the migration is measured against a project that already has
        half of publish-on-web relocated.
        """
        return {
            "schema-version": 2.6,
            "name": "hwt-nqi",
            "users": [{"email": "admin@rijksoverheid.nl", "role": "admin"}],
            "clusters": ["odcn-production"],
            "services": ["publish-on-web"],
            "components": [
                {
                    "name": "component1",
                    "type": "deployment",
                    "ports": {"inbound": [8080], "outbound": [443]},
                    "services": [{"reference": "publish-on-web", "config": {"tls": "standard"}}],
                }
            ],
            "deployments": [
                {
                    "name": "test",
                    "cluster": "odcn-production",
                    "namespace": "hwt-nqi",
                    "domain-format": "component-deployment-project",
                    "components": [{"reference": "component1", "image": "nginx:latest"}],
                }
            ],
        }

    def test_the_deployment_settings_move_under_the_service(self) -> None:
        result, was_migrated = migrate_to_latest(self._hwt_nqi_shaped())

        assert was_migrated is True
        assert result["schema-version"] == LATEST_SCHEMA_VERSION
        deployment = result["deployments"][0]
        assert "domain-format" not in deployment
        assert get_domain_setting(deployment, DomainSetting.DOMAIN_FORMAT) == "component-deployment-project"

    def test_the_component_tls_config_is_left_alone(self) -> None:
        # tls belongs to the COMPONENT layer and stays there; the relocation is about the
        # deployment layer only.
        result, _ = migrate_to_latest(self._hwt_nqi_shaped())
        component_services = result["components"][0]["services"]
        assert component_services == [{"reference": "publish-on-web", "config": {"tls": "standard"}}]

    def test_the_migrated_file_validates(self) -> None:
        # Migrate first, validate second: the order the loader uses.
        result, _ = migrate_to_latest(self._hwt_nqi_shaped())
        validate_project_schema(result)

    def test_all_seven_settings_move_together(self) -> None:
        data = self._hwt_nqi_shaped()
        data["deployments"][0].update(
            {
                "base-domain": "rijksapp.nl",
                "subdomain": "wies",
                "domain-mode": "nice-url",
                "issuer": "letsencrypt",
                "root-component": "component1",
                "expose-component-on-bare-domain": "component1",
            }
        )
        result, _ = migrate_to_latest(data)

        deployment = result["deployments"][0]
        config = deployment["services"][0]["config"]
        assert config == {
            "base-domain": "rijksapp.nl",
            "subdomain": "wies",
            "domain-mode": "nice-url",
            "domain-format": "component-deployment-project",
            "issuer": "letsencrypt",
            "root-component": "component1",
            "expose-component-on-bare-domain": "component1",
        }
        validate_project_schema(result)

    def test_it_is_idempotent(self) -> None:
        once, _ = migrate_to_latest(self._hwt_nqi_shaped())
        snapshot = copy.deepcopy(once)
        assert relocate_domain_settings_to_service(once) is False
        assert once == snapshot

    def test_a_deployment_without_a_web_address_grows_no_service_entry(self) -> None:
        # Otherwise every deployment on the platform's own cluster domain would suddenly
        # list publish-on-web, which reads as "this deployment uses the service".
        data = self._hwt_nqi_shaped()
        del data["deployments"][0]["domain-format"]
        result, _ = migrate_to_latest(data)
        assert "services" not in result["deployments"][0]

    def test_an_existing_deployment_service_entry_is_reused(self) -> None:
        # A deployment already carrying clone state must not end up with two publish-on-web
        # entries, and the clone state must survive untouched.
        data = self._hwt_nqi_shaped()
        data["deployments"][0]["services"] = [{"reference": "postgresql-database", "config": {"generation": 1}}]
        result, _ = migrate_to_latest(data)

        services = result["deployments"][0]["services"]
        assert len(services) == 2
        assert services[0] == {"reference": "postgresql-database", "config": {"generation": 1}}
        assert get_domain_setting(result["deployments"][0], DomainSetting.DOMAIN_FORMAT) == (
            "component-deployment-project"
        )
        validate_project_schema(result)

    def test_an_unmigrated_file_still_validates(self) -> None:
        # A file stamped 2.6 keeps its settings at the deployment root until it is loaded
        # again; the schema of THAT version must still accept them.
        data = self._hwt_nqi_shaped()
        validate_declared_project_schema(data)
