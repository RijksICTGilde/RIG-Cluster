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
        assert "persistent-storage" in persistent
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
        assert dep["root-component"] == "frontend"
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
        assert dep["root-component"] == "landing"
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
        assert "root-component" not in dep

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
        assert "root-component" not in dep

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
        assert result["deployments"][0]["root-component"] == "frontend"
        assert result["deployments"][1]["root-component"] == "api"
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
        assert dep["root-component"] == "frontend"
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
