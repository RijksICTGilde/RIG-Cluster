"""Tests for backup wizard: service detection, backupable labels, and template rendering.

Covers:
- ServiceAdapter.get_backupable_labels() returns correct labels
- ServiceAdapter.get_service_types_for_backup_label() returns correct service types
- extract_service_names_from_component() handles v1 (uses-services) and v2 (services) formats
- deployment_uses_service() correctly detects backupable services
- Backup wizard template renders correctly with jinja-roos-components
"""

import pytest
from opi.handlers.project_file_handler import (
    create_project_file_handler,
    extract_service_names_from_component,
)
from opi.services.services import ServiceAdapter
from opi.services.services_enums import ServiceType

# ---------------------------------------------------------------------------
# ServiceAdapter backup label tests
# ---------------------------------------------------------------------------


class TestBackupableLabels:
    """Tests for ServiceAdapter.get_backupable_labels."""

    def test_returns_list(self) -> None:
        result = ServiceAdapter.get_backupable_labels()
        assert isinstance(result, list)

    def test_contains_expected_labels(self) -> None:
        labels = {bl["label"] for bl in ServiceAdapter.get_backupable_labels()}
        assert "pvc" in labels
        assert "database" in labels
        assert "minio" in labels

    def test_each_entry_has_required_keys(self) -> None:
        for bl in ServiceAdapter.get_backupable_labels():
            assert "label" in bl
            assert "name" in bl
            assert "color" in bl

    def test_no_duplicate_labels(self) -> None:
        labels = [bl["label"] for bl in ServiceAdapter.get_backupable_labels()]
        assert len(labels) == len(set(labels))

    def test_non_backupable_services_excluded(self) -> None:
        labels = {bl["label"] for bl in ServiceAdapter.get_backupable_labels()}
        # redis, keycloak, etc. should not appear
        assert "redis" not in labels
        assert "keycloak" not in labels
        assert "publish-on-web" not in labels


class TestServiceTypesForBackupLabel:
    """Tests for ServiceAdapter.get_service_types_for_backup_label."""

    def test_pvc_returns_persistent_storage(self) -> None:
        result = ServiceAdapter.get_service_types_for_backup_label("pvc")
        assert ServiceType.PERSISTENT_STORAGE.value in result

    def test_database_returns_both_db_types(self) -> None:
        result = ServiceAdapter.get_service_types_for_backup_label("database")
        assert ServiceType.POSTGRESQL_DATABASE.value in result
        assert ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value in result

    def test_minio_returns_minio_storage(self) -> None:
        result = ServiceAdapter.get_service_types_for_backup_label("minio")
        assert ServiceType.MINIO_STORAGE.value in result

    def test_unknown_label_returns_empty(self) -> None:
        result = ServiceAdapter.get_service_types_for_backup_label("nonexistent")
        assert result == []


class TestBackupLabelOnDefinitions:
    """Verify backup_label is set correctly on service definitions."""

    def test_persistent_storage_is_backupable(self) -> None:
        defn = ServiceAdapter.get_service_definition(ServiceType.PERSISTENT_STORAGE)
        assert defn.backup_label == "pvc"

    def test_postgresql_is_backupable(self) -> None:
        defn = ServiceAdapter.get_service_definition(ServiceType.POSTGRESQL_DATABASE)
        assert defn.backup_label == "database"

    def test_namespace_postgresql_is_backupable(self) -> None:
        defn = ServiceAdapter.get_service_definition(ServiceType.NAMESPACE_POSTGRESQL_DATABASE)
        assert defn.backup_label == "database"

    def test_minio_is_backupable(self) -> None:
        defn = ServiceAdapter.get_service_definition(ServiceType.MINIO_STORAGE)
        assert defn.backup_label == "minio"

    def test_redis_not_backupable(self) -> None:
        defn = ServiceAdapter.get_service_definition(ServiceType.REDIS)
        assert defn.backup_label is None

    def test_keycloak_not_backupable(self) -> None:
        defn = ServiceAdapter.get_service_definition(ServiceType.KEYCLOAK)
        assert defn.backup_label is None

    def test_temp_storage_not_backupable(self) -> None:
        defn = ServiceAdapter.get_service_definition(ServiceType.TEMP_STORAGE)
        assert defn.backup_label is None


# ---------------------------------------------------------------------------
# extract_service_names_from_component tests
# ---------------------------------------------------------------------------


class TestExtractServiceNamesFromComponent:
    """Tests for extract_service_names_from_component with v1 and v2 formats."""

    def test_v2_services_string_format(self) -> None:
        component = {"name": "app", "services": ["persistent-storage", "postgresql-database"]}
        result = extract_service_names_from_component(component)
        assert "persistent-storage" in result
        assert "postgresql-database" in result

    def test_v2_services_dict_format(self) -> None:
        component = {
            "name": "app",
            "services": [
                {"persistent-storage": {"config": [{"name": "data", "size": "1Gi"}]}},
                "postgresql-database",
            ],
        }
        result = extract_service_names_from_component(component)
        assert "persistent-storage" in result
        assert "postgresql-database" in result

    def test_v1_uses_services(self) -> None:
        component = {"name": "app", "uses-services": ["persistent-storage", "keycloak"]}
        result = extract_service_names_from_component(component)
        assert "persistent-storage" in result
        assert "keycloak" in result

    def test_v1_uses_services_with_storage_block(self) -> None:
        """v1 format: uses-services + separate storage block."""
        component = {
            "name": "app",
            "uses-services": ["persistent-storage", "publish-on-web"],
            "storage": [{"type": "persistent", "size": "10Gi", "mount-path": "/data"}],
        }
        result = extract_service_names_from_component(component)
        assert "persistent-storage" in result
        assert "publish-on-web" in result

    def test_both_formats_merged(self) -> None:
        """When both v1 and v2 keys exist, merge without duplicates."""
        component = {
            "name": "app",
            "services": ["persistent-storage"],
            "uses-services": ["persistent-storage", "keycloak"],
        }
        result = extract_service_names_from_component(component)
        assert result.count("persistent-storage") == 1
        assert "keycloak" in result

    def test_empty_component(self) -> None:
        result = extract_service_names_from_component({"name": "app"})
        assert result == []

    def test_no_services_keys(self) -> None:
        component = {"name": "app", "image": "nginx:latest"}
        result = extract_service_names_from_component(component)
        assert result == []


# ---------------------------------------------------------------------------
# deployment_uses_service integration tests
# ---------------------------------------------------------------------------


class TestDeploymentUsesServiceForBackup:
    """Tests for deployment_uses_service with backup-relevant service types."""

    @pytest.fixture
    def handler(self):
        return create_project_file_handler()

    def _make_project(
        self,
        component_services: list[str] | None = None,
        component_uses_services: list[str] | None = None,
    ) -> dict:
        """Build a minimal project data dict."""
        component: dict = {"name": "app"}
        if component_services is not None:
            component["services"] = component_services
        if component_uses_services is not None:
            component["uses-services"] = component_uses_services

        return {
            "name": "test-project",
            "components": [component],
            "deployments": [
                {
                    "name": "production",
                    "cluster": "local",
                    "namespace": "test",
                    "components": [{"reference": "app"}],
                }
            ],
        }

    def test_v2_persistent_storage_detected(self, handler) -> None:
        project = self._make_project(component_services=["persistent-storage", "publish-on-web"])
        assert handler.deployment_uses_service(project, "production", ["persistent-storage"])

    def test_v2_database_detected(self, handler) -> None:
        project = self._make_project(component_services=["postgresql-database"])
        assert handler.deployment_uses_service(project, "production", ["postgresql-database"])

    def test_v2_minio_detected(self, handler) -> None:
        project = self._make_project(component_services=["minio-storage"])
        assert handler.deployment_uses_service(project, "production", ["minio-storage"])

    def test_v1_persistent_storage_detected(self, handler) -> None:
        project = self._make_project(component_uses_services=["persistent-storage", "publish-on-web"])
        assert handler.deployment_uses_service(project, "production", ["persistent-storage"])

    def test_v1_database_detected(self, handler) -> None:
        project = self._make_project(component_uses_services=["postgresql-database"])
        assert handler.deployment_uses_service(project, "production", ["postgresql-database"])

    def test_no_services_not_detected(self, handler) -> None:
        project = self._make_project()
        assert not handler.deployment_uses_service(project, "production", ["persistent-storage"])

    def test_only_non_backupable_services(self, handler) -> None:
        project = self._make_project(component_services=["publish-on-web", "keycloak"])
        assert not handler.deployment_uses_service(project, "production", ["persistent-storage"])
        assert not handler.deployment_uses_service(
            project, "production", ["postgresql-database", "namespace-postgresql-database"]
        )
        assert not handler.deployment_uses_service(project, "production", ["minio-storage"])

    def test_backupable_labels_integration(self, handler) -> None:
        """Full integration: loop over backupable labels like the wizard does."""
        project = self._make_project(component_services=["persistent-storage", "minio-storage"])
        found_labels = []
        for bl in ServiceAdapter.get_backupable_labels():
            svc_types = ServiceAdapter.get_service_types_for_backup_label(bl["label"])
            if handler.deployment_uses_service(project, "production", svc_types):
                found_labels.append(bl["label"])
        assert "pvc" in found_labels
        assert "minio" in found_labels
        assert "database" not in found_labels

    def test_backupable_labels_v1_integration(self, handler) -> None:
        """Full integration with v1 format."""
        project = self._make_project(component_uses_services=["persistent-storage", "postgresql-database"])
        found_labels = []
        for bl in ServiceAdapter.get_backupable_labels():
            svc_types = ServiceAdapter.get_service_types_for_backup_label(bl["label"])
            if handler.deployment_uses_service(project, "production", svc_types):
                found_labels.append(bl["label"])
        assert "pvc" in found_labels
        assert "database" in found_labels
        assert "minio" not in found_labels

    def test_no_backupable_services_empty_list(self, handler) -> None:
        """Project with only non-backupable services produces empty list."""
        project = self._make_project(component_services=["publish-on-web", "keycloak"])
        found_labels = []
        for bl in ServiceAdapter.get_backupable_labels():
            svc_types = ServiceAdapter.get_service_types_for_backup_label(bl["label"])
            if handler.deployment_uses_service(project, "production", svc_types):
                found_labels.append(bl["label"])
        assert found_labels == []


# ---------------------------------------------------------------------------
# Template rendering tests
# ---------------------------------------------------------------------------


class TestBackupWizardTemplate:
    """Tests for backup wizard template rendering with jinja-roos-components."""

    @pytest.fixture
    def templates(self):
        from opi.core.templates import get_templates

        return get_templates()

    def _render(self, templates, context: dict) -> str:
        template = templates.get_template("wizard/partials/backup_select_deployment.html.j2")
        rendered = template.render(context)
        process_components = templates.env.filters.get("process_components")
        if process_components:
            rendered = str(process_components(rendered))
        return rendered

    def test_renders_with_deployments(self, templates) -> None:
        context = {
            "_cluster_deployments": [
                {"name": "production", "namespace": "rig-myapp", "resource_types": ["pvc", "database"]},
            ],
            "_backupable_labels": [
                {"label": "pvc", "name": "Permanente opslag", "color": "grijs-600"},
                {"label": "database", "name": "PostgreSQL Database", "color": "donkerblauw"},
            ],
            "_current_cluster": "local",
            "_project_name": "test-project",
        }
        html = self._render(templates, context)
        assert "production" in html
        assert "rig-myapp" in html
        assert "<select" in html
        assert 'name="deployment_name"' in html
        assert 'name="resource_types"' in html

    def test_renders_empty_state(self, templates) -> None:
        context = {
            "_cluster_deployments": [],
            "_backupable_labels": [],
            "_current_cluster": "local",
            "_project_name": "test-project",
        }
        html = self._render(templates, context)
        assert "Geen deployments met backup-mogelijkheden" in html
        assert "<select" not in html

    def test_renders_resource_types_for_selected_deployment(self, templates) -> None:
        context = {
            "_cluster_deployments": [
                {"name": "staging", "namespace": "rig-staging", "resource_types": ["pvc"]},
                {"name": "production", "namespace": "rig-prod", "resource_types": ["pvc", "database", "minio"]},
            ],
            "_backupable_labels": [
                {"label": "pvc", "name": "Permanente opslag", "color": "grijs-600"},
                {"label": "database", "name": "PostgreSQL Database", "color": "donkerblauw"},
                {"label": "minio", "name": "MinIO Object Storage", "color": "rood"},
            ],
            "_current_cluster": "local",
            "_project_name": "test-project",
            "_selected_deployment": "production",
        }
        html = self._render(templates, context)
        # All three resource types should appear for production
        assert 'value="pvc"' in html
        assert 'value="database"' in html
        assert 'value="minio"' in html

    def test_renders_only_available_resource_types(self, templates) -> None:
        context = {
            "_cluster_deployments": [
                {"name": "staging", "namespace": "rig-staging", "resource_types": ["pvc"]},
            ],
            "_backupable_labels": [
                {"label": "pvc", "name": "Permanente opslag", "color": "grijs-600"},
                {"label": "database", "name": "PostgreSQL Database", "color": "donkerblauw"},
            ],
            "_current_cluster": "local",
            "_project_name": "test-project",
        }
        html = self._render(templates, context)
        assert 'value="pvc"' in html
        assert 'value="database"' not in html

    def test_htmx_attributes_present(self, templates) -> None:
        context = {
            "_cluster_deployments": [
                {"name": "prod", "namespace": "rig-prod", "resource_types": ["pvc"]},
            ],
            "_backupable_labels": [
                {"label": "pvc", "name": "Permanente opslag", "color": "grijs-600"},
            ],
            "_current_cluster": "local",
            "_project_name": "my-project",
        }
        html = self._render(templates, context)
        assert "hx-get" in html
        assert "/projects/my-project/modal-wizard/modal-backup/select-deployment" in html
        assert 'hx-target="#edit-section-inner"' in html

    def test_dropdown_not_radio(self, templates) -> None:
        """Verify deployment selection uses dropdown, not radio buttons."""
        context = {
            "_cluster_deployments": [
                {"name": "a", "namespace": "ns-a", "resource_types": ["pvc"]},
                {"name": "b", "namespace": "ns-b", "resource_types": ["database"]},
            ],
            "_backupable_labels": [
                {"label": "pvc", "name": "Permanente opslag", "color": "grijs-600"},
                {"label": "database", "name": "PostgreSQL Database", "color": "donkerblauw"},
            ],
            "_current_cluster": "local",
            "_project_name": "test",
        }
        html = self._render(templates, context)
        assert "<select" in html
        assert "<option" in html
        assert 'type="radio"' not in html
