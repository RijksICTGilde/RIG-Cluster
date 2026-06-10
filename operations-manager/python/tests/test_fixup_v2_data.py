"""Tests for v2 data fixup in schema migration."""

from opi.services.schema_migration import _fixup_v2_data, migrate_to_latest

# ---------------------------------------------------------------------------
# _fixup_v2_data: literal services{...} keys
# ---------------------------------------------------------------------------


class TestFixupServicesFilterKeys:
    def test_removes_literal_services_brace_keys_from_component(self):
        data = {
            "schema-version": 2,
            "components": [
                {
                    "name": "worker",
                    "services{persistent-storage}": {"config": [""]},
                    "services{metrics-scraper}": {"port": 8080, "path": "/metrics"},
                    "services": ["persistent-storage", "metrics-scraper"],
                }
            ],
        }
        assert _fixup_v2_data(data) is True
        comp = data["components"][0]
        assert "services{persistent-storage}" not in comp
        assert "services{metrics-scraper}" not in comp
        # Real services list untouched
        assert comp["services"] == ["persistent-storage", "metrics-scraper"]

    def test_removes_literal_services_brace_keys_from_deployment_component(self):
        data = {
            "schema-version": 2,
            "components": [],
            "deployments": [
                {
                    "name": "prod",
                    "components": [
                        {
                            "reference": "worker",
                            "services{temp-storage}": {"config": [""]},
                        }
                    ],
                }
            ],
        }
        assert _fixup_v2_data(data) is True
        comp = data["deployments"][0]["components"][0]
        assert "services{temp-storage}" not in comp
        assert comp["reference"] == "worker"

    def test_no_change_when_no_stale_keys(self):
        data = {
            "schema-version": 2,
            "components": [{"name": "web", "services": ["publish-on-web"]}],
        }
        assert _fixup_v2_data(data) is False


# ---------------------------------------------------------------------------
# _fixup_v2_data: publish-on-web root key
# ---------------------------------------------------------------------------


class TestFixupPublishOnWeb:
    def test_removes_publish_on_web_true(self):
        data = {
            "schema-version": 2,
            "components": [
                {
                    "name": "upload",
                    "publish-on-web": True,
                    "services": ["publish-on-web"],
                }
            ],
        }
        assert _fixup_v2_data(data) is True
        assert "publish-on-web" not in data["components"][0]

    def test_keeps_publish_on_web_false(self):
        """publish-on-web: false is not the same bug pattern, leave it."""
        data = {
            "schema-version": 2,
            "components": [{"name": "internal", "publish-on-web": False}],
        }
        assert _fixup_v2_data(data) is False


# ---------------------------------------------------------------------------
# _fixup_v2_data: flat resource format migration
# ---------------------------------------------------------------------------


class TestFixupFlatResources:
    def test_migrates_flat_cpu_dict(self):
        """cpu: {request: "50m", limit: "1"} → requests/limits."""
        data = {
            "schema-version": 2,
            "components": [
                {
                    "name": "worker",
                    "resources": {
                        "cpu": {"request": "50m", "limit": "1"},
                        "requests": {"memory": "256Mi"},
                        "limits": {"memory": "512Mi"},
                    },
                }
            ],
        }
        assert _fixup_v2_data(data) is True
        res = data["components"][0]["resources"]
        assert "cpu" not in res
        assert res["requests"]["cpu"] == "50m"
        assert res["limits"]["cpu"] == "1"
        # Existing memory values untouched
        assert res["requests"]["memory"] == "256Mi"
        assert res["limits"]["memory"] == "512Mi"

    def test_migrates_flat_cpu_string(self):
        """cpu: "1" → limits.cpu: "1"."""
        data = {
            "schema-version": 2,
            "components": [{"name": "web", "resources": {"cpu": "1"}}],
        }
        assert _fixup_v2_data(data) is True
        res = data["components"][0]["resources"]
        assert "cpu" not in res
        assert res["limits"]["cpu"] == "1"

    def test_migrates_flat_memory_string(self):
        """memory: "256Mi" → requests/limits."""
        data = {
            "schema-version": 2,
            "components": [{"name": "web", "resources": {"memory": "256Mi"}}],
        }
        assert _fixup_v2_data(data) is True
        res = data["components"][0]["resources"]
        assert "memory" not in res
        assert res["limits"]["memory"] == "256Mi"
        assert res["requests"]["memory"] == "256Mi"

    def test_does_not_overwrite_existing_nested_values(self):
        """If requests/limits already have cpu/memory, don't overwrite."""
        data = {
            "schema-version": 2,
            "components": [
                {
                    "name": "web",
                    "resources": {
                        "cpu": {"request": "50m", "limit": "500m"},
                        "memory": "128Mi",
                        "requests": {"cpu": "100m", "memory": "256Mi"},
                        "limits": {"cpu": "1", "memory": "512Mi"},
                    },
                }
            ],
        }
        assert _fixup_v2_data(data) is True
        res = data["components"][0]["resources"]
        # Existing nested values preserved
        assert res["requests"]["cpu"] == "100m"
        assert res["limits"]["cpu"] == "1"
        assert res["requests"]["memory"] == "256Mi"
        assert res["limits"]["memory"] == "512Mi"

    def test_no_change_when_already_nested(self):
        data = {
            "schema-version": 2,
            "components": [
                {
                    "name": "web",
                    "resources": {
                        "requests": {"cpu": "50m", "memory": "256Mi"},
                        "limits": {"cpu": "1", "memory": "512Mi"},
                    },
                }
            ],
        }
        assert _fixup_v2_data(data) is False

    def test_handles_deployment_component_resources(self):
        data = {
            "schema-version": 2,
            "components": [],
            "deployments": [
                {
                    "name": "prod",
                    "components": [
                        {
                            "reference": "worker",
                            "resources": {"cpu": {"request": "100m", "limit": "2"}, "memory": "1Gi"},
                        }
                    ],
                }
            ],
        }
        assert _fixup_v2_data(data) is True
        res = data["deployments"][0]["components"][0]["resources"]
        assert "cpu" not in res
        assert "memory" not in res
        assert res["requests"]["cpu"] == "100m"
        assert res["limits"]["cpu"] == "2"
        assert res["limits"]["memory"] == "1Gi"


# ---------------------------------------------------------------------------
# migrate_to_latest runs fixup on v2 files
# ---------------------------------------------------------------------------


class TestMigrateToLatestRunsFixup:
    def test_fixup_runs_on_v2_project(self):
        data = {
            "schema-version": 2,
            "components": [
                {
                    "name": "worker",
                    "services{persistent-storage}": {"config": [""]},
                    "resources": {"cpu": "1", "memory": "256Mi"},
                    "publish-on-web": True,
                    "services": ["publish-on-web"],
                }
            ],
        }
        result, migrated = migrate_to_latest(data)
        assert migrated is True
        comp = result["components"][0]
        assert "services{persistent-storage}" not in comp
        assert "publish-on-web" not in comp
        assert "cpu" not in comp["resources"]
        assert "memory" not in comp["resources"]
        assert comp["resources"]["limits"]["cpu"] == "1"
        assert comp["resources"]["limits"]["memory"] == "256Mi"


# ---------------------------------------------------------------------------
# Real-world: regel-k4c enrichworker pattern
# ---------------------------------------------------------------------------


class TestRegelK4cPattern:
    """Reproduce the exact corruption seen in regel-k4c.yaml."""

    def test_enrichworker_cleanup(self):
        data = {
            "schema-version": 2,
            "components": [
                {
                    "name": "enrichworker",
                    "resources": {
                        "cpu": {"request": "50m", "limit": "1"},
                        "requests": {"memory": "4096Mi"},
                        "limits": {"memory": "4096Mi"},
                        "history": [{"timestamp": "2026-03-24", "source": "oom-watcher"}],
                    },
                    "services{persistent-storage}": {"config": [""]},
                    "services{temp-storage}": {"config": [""]},
                    "services{metrics-scraper}": {"port": 8080, "path": "/metrics"},
                    "services": [
                        {"persistent-storage": {"config": [{"name": "data", "size": "50Mi", "mount-path": "/data"}]}},
                        "postgresql-database",
                    ],
                }
            ],
        }
        result, migrated = migrate_to_latest(data)
        assert migrated is True
        comp = result["components"][0]

        # Stale services{} keys removed
        stale = [k for k in comp if "{" in str(k)]
        assert stale == []

        # Flat cpu migrated, existing nested values preserved
        assert "cpu" not in comp["resources"]
        assert comp["resources"]["requests"]["cpu"] == "50m"
        assert comp["resources"]["limits"]["cpu"] == "1"
        assert comp["resources"]["requests"]["memory"] == "4096Mi"
        assert comp["resources"]["limits"]["memory"] == "4096Mi"

        # History preserved
        assert len(comp["resources"]["history"]) == 1

        # Services list untouched
        assert len(comp["services"]) == 2

    def test_upload_cleanup(self):
        data = {
            "schema-version": 2,
            "components": [
                {
                    "name": "upload",
                    "publish-on-web": True,
                    "resources": {"cpu": "1", "memory": "256Mi"},
                    "services": ["publish-on-web"],
                }
            ],
        }
        result, migrated = migrate_to_latest(data)
        assert migrated is True
        comp = result["components"][0]
        assert "publish-on-web" not in comp
        assert comp["resources"]["limits"]["cpu"] == "1"
        assert comp["resources"]["limits"]["memory"] == "256Mi"
        assert comp["resources"]["requests"]["memory"] == "256Mi"


# ---------------------------------------------------------------------------
# _fixup_catalog_root: stale root flag on the top-level component catalog
# ---------------------------------------------------------------------------


class TestFixupCatalogRoot:
    def test_strips_catalog_root_and_lifts_to_deployment(self):
        """A catalog component marked root: true lifts to root-component on the
        referencing deployment, and the stale catalog key is removed."""
        data = {
            "schema-version": 2.2,
            "components": [{"name": "component-1", "root": True}],
            "deployments": [
                {"name": "main", "components": [{"reference": "component-1"}]},
            ],
        }
        assert _fixup_v2_data(data) is True
        assert "root" not in data["components"][0]
        assert data["deployments"][0]["root-component"] == "component-1"

    def test_strips_catalog_root_when_deployment_already_has_root_component(self):
        """If the deployment already has root-component, it takes precedence and
        the stale catalog flag is simply removed."""
        data = {
            "schema-version": 2.2,
            "components": [{"name": "component-1", "root": True}],
            "deployments": [
                {
                    "name": "main",
                    "components": [{"reference": "component-1"}],
                    "root-component": "component-1",
                },
            ],
        }
        assert _fixup_v2_data(data) is True
        assert "root" not in data["components"][0]
        assert data["deployments"][0]["root-component"] == "component-1"

    def test_drops_root_false_without_lifting(self):
        data = {
            "schema-version": 2.2,
            "components": [{"name": "component-1", "root": False}],
            "deployments": [
                {"name": "main", "components": [{"reference": "component-1"}]},
            ],
        }
        assert _fixup_v2_data(data) is True
        assert "root" not in data["components"][0]
        assert "root-component" not in data["deployments"][0]

    def test_runs_on_already_latest_version_via_migrate_to_latest(self):
        """The bouwm-6gn case: a file already stamped 2.2 with a catalog root key
        is still repaired (the version-gated _migrate_v2_to_v2_1 would skip it)."""
        data = {
            "schema-version": 2.2,
            "components": [{"name": "component-1", "root": True}],
            "deployments": [
                {
                    "name": "main",
                    "components": [{"reference": "component-1"}],
                    "root-component": "component-1",
                },
            ],
        }
        result, migrated = migrate_to_latest(data)
        assert migrated is True
        assert "root" not in result["components"][0]

    def test_no_change_when_catalog_has_no_root(self):
        data = {
            "schema-version": 2.2,
            "components": [{"name": "component-1"}],
            "deployments": [
                {"name": "main", "components": [{"reference": "component-1"}]},
            ],
        }
        assert _fixup_v2_data(data) is False
