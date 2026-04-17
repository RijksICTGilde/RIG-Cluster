"""Tests for backup wizard: service detection, backupable labels, template rendering, and restore.

Covers:
- ServiceAdapter.get_backupable_labels() returns correct labels
- ServiceAdapter.get_service_types_for_backup_label() returns correct service types
- extract_service_names_from_component() handles v1 (uses-services) and v2 (services) formats
- deployment_uses_service() correctly detects backupable services
- Backup wizard template renders correctly with jinja-roos-components
- CloneFromType enum values and manager compatibility
- RestoreMode enum values
- _restore_target_summary handles both existing and new modes
"""

from typing import Any

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

    def test_dict_with_none_value_skipped(self) -> None:
        """Empty placeholder entries like {"persistent-storage": null} are skipped."""
        component = {
            "name": "app",
            "services": [
                {"persistent-storage": None},
                "publish-on-web",
            ],
        }
        result = extract_service_names_from_component(component)
        assert "persistent-storage" not in result
        assert "publish-on-web" in result

    def test_dict_with_empty_dict_value_skipped(self) -> None:
        component = {
            "name": "app",
            "services": [{"persistent-storage": {}}, "keycloak"],
        }
        result = extract_service_names_from_component(component)
        assert "persistent-storage" not in result
        assert "keycloak" in result

    def test_dict_with_empty_list_value_skipped(self) -> None:
        component = {
            "name": "app",
            "services": [{"persistent-storage": []}, "keycloak"],
        }
        result = extract_service_names_from_component(component)
        assert "persistent-storage" not in result

    def test_dict_with_config_kept(self) -> None:
        """Dict entries with actual config are kept."""
        component = {
            "name": "app",
            "services": [
                {"persistent-storage": {"config": [{"name": "data", "size": "1Gi", "mount-path": "/data"}]}},
            ],
        }
        result = extract_service_names_from_component(component)
        assert "persistent-storage" in result

    def test_mixed_empty_and_configured(self) -> None:
        """Only configured services are extracted."""
        component = {
            "name": "app",
            "services": [
                {"persistent-storage": None},
                {"temp-storage": None},
                {"minio-storage": {"config": {"bucket": "test"}}},
                "publish-on-web",
            ],
        }
        result = extract_service_names_from_component(component)
        assert "persistent-storage" not in result
        assert "temp-storage" not in result
        assert "minio-storage" in result
        assert "publish-on-web" in result


# ---------------------------------------------------------------------------
# _filter_set_terminal tests (path system - prevents empty service dicts)
# ---------------------------------------------------------------------------


class TestFilterSetTerminalNoneHandling:
    """Tests that _filter_set_terminal does not create empty service entries."""

    def _set(self, lst: list, filt: str, value: Any) -> None:
        from opi.forms.editables.path import _filter_set_terminal

        _filter_set_terminal(lst, filt, value)

    def test_none_value_not_appended(self) -> None:
        """Setting None on a missing filter key should not create a new entry."""
        lst: list = ["publish-on-web", "keycloak"]
        self._set(lst, "persistent-storage", None)
        assert len(lst) == 2
        assert "persistent-storage" not in lst

    def test_none_clears_existing_dict_to_string(self) -> None:
        """Setting None on an existing dict entry reverts it to a plain string."""
        lst: list = ["publish-on-web", {"persistent-storage": {"config": [{"name": "data"}]}}]
        self._set(lst, "persistent-storage", None)
        assert lst == ["publish-on-web", "persistent-storage"]

    def test_none_on_string_entry_is_noop(self) -> None:
        """Setting None on an existing string entry does nothing."""
        lst: list = ["publish-on-web", "persistent-storage"]
        self._set(lst, "persistent-storage", None)
        assert lst == ["publish-on-web", "persistent-storage"]

    def test_value_creates_dict_entry(self) -> None:
        """Setting a real value on a missing key creates the dict entry."""
        lst: list = ["publish-on-web"]
        self._set(lst, "persistent-storage", {"config": [{"name": "data"}]})
        assert lst == ["publish-on-web", {"persistent-storage": {"config": [{"name": "data"}]}}]

    def test_value_promotes_string_to_dict(self) -> None:
        """Setting a real value on a string entry promotes it to a dict."""
        lst: list = ["persistent-storage"]
        self._set(lst, "persistent-storage", {"config": [{"name": "data"}]})
        assert lst == [{"persistent-storage": {"config": [{"name": "data"}]}}]

    def test_value_updates_existing_dict(self) -> None:
        """Setting a real value on an existing dict entry updates it."""
        lst: list = [{"persistent-storage": {"config": [{"name": "old"}]}}]
        self._set(lst, "persistent-storage", {"config": [{"name": "new"}]})
        assert lst == [{"persistent-storage": {"config": [{"name": "new"}]}}]


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


# ---------------------------------------------------------------------------
# CloneFromType enum tests
# ---------------------------------------------------------------------------


class TestCloneFromType:
    """Tests for CloneFromType enum values."""

    def test_deployment_value(self) -> None:
        from opi.services import CloneFromType

        assert CloneFromType.DEPLOYMENT.value == "deployment"

    def test_remote_source_value(self) -> None:
        from opi.services import CloneFromType

        assert CloneFromType.REMOTE_SOURCE.value == "remote-source"

    def test_backup_value(self) -> None:
        from opi.services import CloneFromType

        assert CloneFromType.BACKUP.value == "backup"

    def test_all_values_unique(self) -> None:
        from opi.services import CloneFromType

        values = [ct.value for ct in CloneFromType]
        assert len(values) == len(set(values))


# ---------------------------------------------------------------------------
# RestoreMode enum tests
# ---------------------------------------------------------------------------


class TestRestoreMode:
    """Tests for RestoreMode enum values."""

    def test_existing_value(self) -> None:
        from opi.services import RestoreMode

        assert RestoreMode.EXISTING.value == "existing"

    def test_new_value(self) -> None:
        from opi.services import RestoreMode

        assert RestoreMode.NEW.value == "new"


# ---------------------------------------------------------------------------
# _restore_target_summary tests
# ---------------------------------------------------------------------------


class TestRestoreTargetSummary:
    """Tests for _restore_target_summary with existing and new modes."""

    def test_existing_mode_default(self) -> None:
        from opi.forms.visualizers.wizard_sections import _restore_target_summary

        data = {"target_deployment": "production"}
        result = _restore_target_summary(data)
        assert "production" in result
        assert "Doel deployment" in result

    def test_existing_mode_explicit(self) -> None:
        from opi.forms.visualizers.wizard_sections import _restore_target_summary

        data = {"restore_mode": "existing", "target_deployment": "staging"}
        result = _restore_target_summary(data)
        assert "staging" in result
        assert "Doel deployment" in result

    def test_new_mode(self) -> None:
        from opi.forms.visualizers.wizard_sections import _restore_target_summary

        data = {"restore_mode": "new", "new_deployment_name": "test-copy"}
        result = _restore_target_summary(data)
        assert "Nieuwe deployment" in result
        assert "Modus" in result

    def test_new_mode_missing_name(self) -> None:
        from opi.forms.visualizers.wizard_sections import _restore_target_summary

        data = {"restore_mode": "new"}
        result = _restore_target_summary(data)
        assert "Nieuwe deployment" in result
        assert "Modus" in result

    def test_no_data_defaults_to_existing(self) -> None:
        from opi.forms.visualizers.wizard_sections import _restore_target_summary

        data: dict = {}
        result = _restore_target_summary(data)
        assert "Doel deployment" in result


# ---------------------------------------------------------------------------
# Deployment creation helper tests
# ---------------------------------------------------------------------------


class TestCreateDeploymentFromSource:
    """Tests for _create_deployment_from_source structure logic."""

    def test_clone_exclude_keys(self) -> None:
        """Verify the correct keys are excluded when copying deployment config."""
        import copy

        from opi.services import CloneFromType

        source_dep = {
            "name": "production",
            "cluster": "local",
            "namespace": "myapp",
            "repository": "main",
            "subdomain": "production",
            "base-domain": "example.com",
            "domain-mode": "custom",
            "issuer": "letsencrypt",
            "components": [{"reference": "app"}],
            "extra-config": "preserved",
        }

        clone_exclude_keys = [
            "name",
            "components",
            "subdomain",
            "base-domain",
            "domain-mode",
            "issuer",
        ]

        new_deployment: dict = {"name": "test-copy"}
        new_deployment.update({k: copy.deepcopy(v) for k, v in source_dep.items() if k not in clone_exclude_keys})
        new_deployment["components"] = copy.deepcopy(source_dep.get("components", []))
        new_deployment["clone-from"] = {
            "type": CloneFromType.BACKUP.value,
            "reference": "production",
            "mode": "once",
        }

        # Verify structure
        assert new_deployment["name"] == "test-copy"
        assert new_deployment["cluster"] == "local"
        assert new_deployment["namespace"] == "myapp"
        assert new_deployment["extra-config"] == "preserved"
        assert new_deployment["components"] == [{"reference": "app"}]
        assert new_deployment["clone-from"]["type"] == "backup"
        assert new_deployment["clone-from"]["reference"] == "production"

        # Excluded keys should not be copied from source
        assert "base-domain" not in new_deployment
        assert "domain-mode" not in new_deployment
        assert "issuer" not in new_deployment

    def test_subdomain_matches_source_name(self) -> None:
        """When source subdomain == source name, new subdomain = target name."""
        source_dep = {
            "name": "production",
            "subdomain": "production",
        }
        target_name = "staging"

        source_subdomain = source_dep.get("subdomain")
        source_name = source_dep.get("name")
        new_subdomain = None
        if source_subdomain and source_subdomain == source_name:
            new_subdomain = target_name

        assert new_subdomain == "staging"

    def test_subdomain_custom_not_copied(self) -> None:
        """When source subdomain differs from source name, no subdomain is set."""
        source_dep = {
            "name": "production",
            "subdomain": "custom-sub",
        }
        target_name = "staging"

        source_subdomain = source_dep.get("subdomain")
        source_name = source_dep.get("name")
        new_subdomain = None
        if source_subdomain and source_subdomain == source_name:
            new_subdomain = target_name

        assert new_subdomain is None


# ---------------------------------------------------------------------------
# Restore target template rendering tests
# ---------------------------------------------------------------------------


class TestRestoreTargetTemplate:
    """Tests for restore target template rendering."""

    @pytest.fixture
    def templates(self):
        from opi.core.templates import get_templates

        return get_templates()

    def _render(self, templates, context: dict) -> str:
        template = templates.get_template("wizard/partials/restore_select_target.html.j2")
        rendered = template.render(context)
        process_components = templates.env.filters.get("process_components")
        if process_components:
            rendered = str(process_components(rendered))
        return rendered

    def test_existing_mode_default(self, templates) -> None:
        context = {
            "_cluster_deployments": [
                {"name": "production", "namespace": "rig-myapp"},
            ],
            "_current_cluster": "local",
            "_project_name": "test-project",
        }
        html = self._render(templates, context)
        assert "Bestaande deployment" in html
        assert "Nieuwe deployment" in html
        assert 'name="target_deployment"' in html
        assert "production" in html

    def test_new_mode_shows_info_card(self, templates) -> None:
        context = {
            "_cluster_deployments": [
                {"name": "production", "namespace": "rig-myapp"},
            ],
            "_current_cluster": "local",
            "_project_name": "test-project",
            "_restore_mode": "new",
            "_source_deployment": "production",
        }
        html = self._render(templates, context)
        assert "Nieuwe deployment aanmaken" in html
        assert "volgende stap" in html

    def test_htmx_attributes_on_mode_toggle(self, templates) -> None:
        context = {
            "_cluster_deployments": [],
            "_current_cluster": "local",
            "_project_name": "my-project",
        }
        html = self._render(templates, context)
        assert "hx-get" in html
        assert "select-restore-mode" in html
        assert "my-project" in html


# ---------------------------------------------------------------------------
# PVC pre-restore naming tests
# ---------------------------------------------------------------------------


class TestPreRestorePvcNaming:
    """Tests for PVC name calculation used in _pre_restore_pvcs."""

    def test_source_pvc_name_gen0(self) -> None:
        """Source PVC name with generation 0 has no version suffix."""
        from opi.utils.naming import generate_pvc_name, generate_unique_name

        unique = generate_unique_name("production", "webapp")
        pvc_name = generate_pvc_name(unique, "data", 0)
        assert pvc_name == "production-webapp-data-pvc"

    def test_source_pvc_name_gen2(self) -> None:
        """Source PVC name with generation > 0 has version suffix."""
        from opi.utils.naming import generate_pvc_name, generate_unique_name

        unique = generate_unique_name("production", "webapp")
        pvc_name = generate_pvc_name(unique, "data", 2)
        assert pvc_name == "production-webapp-data-pvc-v2"

    def test_target_pvc_name_gen0_for_new_deployment(self) -> None:
        """Target PVC for new deployment always uses generation 0 (no suffix)."""
        from opi.utils.naming import generate_pvc_name, generate_unique_name

        unique = generate_unique_name("staging-copy", "webapp")
        pvc_name = generate_pvc_name(unique, "data", 0)
        assert pvc_name == "staging-copy-webapp-data-pvc"

    def test_source_and_target_differ_by_deployment_name(self) -> None:
        """Source and target PVC names differ only in deployment prefix."""
        from opi.utils.naming import generate_pvc_name, generate_unique_name

        source_unique = generate_unique_name("production", "app")
        source_pvc = generate_pvc_name(source_unique, "data", 0)

        target_unique = generate_unique_name("test-copy", "app")
        target_pvc = generate_pvc_name(target_unique, "data", 0)

        assert source_pvc == "production-app-data-pvc"
        assert target_pvc == "test-copy-app-data-pvc"

    def test_source_gen_preserved_from_backup_item(self) -> None:
        """Source generation from backup item is used for Kopia snapshot lookup."""
        from opi.utils.naming import generate_pvc_name, generate_unique_name

        backup_item = {
            "resource_type": "pvc",
            "component_name": "webapp",
            "storage_name": "data",
            "generation": 3,
        }
        source_gen = backup_item.get("generation", 0)
        source_unique = generate_unique_name("production", backup_item["component_name"])
        source_pvc = generate_pvc_name(source_unique, backup_item["storage_name"], source_gen)

        target_unique = generate_unique_name("new-dep", backup_item["component_name"])
        target_pvc = generate_pvc_name(target_unique, backup_item["storage_name"], 0)

        assert source_pvc == "production-webapp-data-pvc-v3"
        assert target_pvc == "new-dep-webapp-data-pvc"


class TestBackupItemsSplitting:
    """Tests for splitting backup_items into PVC and non-PVC groups."""

    def test_split_pvc_items(self) -> None:
        items = [
            {"resource_type": "pvc", "component_name": "app", "storage_name": "data"},
            {"resource_type": "database", "component_name": "app", "reference_name": "main"},
            {"resource_type": "pvc", "component_name": "worker", "storage_name": "queue"},
            {"resource_type": "bucket", "component_name": "app", "reference_name": "files"},
        ]
        pvc_items = [i for i in items if i.get("resource_type") == "pvc"]
        non_pvc_items = [i for i in items if i.get("resource_type") != "pvc"]

        assert len(pvc_items) == 2
        assert len(non_pvc_items) == 2
        assert all(i["resource_type"] == "pvc" for i in pvc_items)
        assert all(i["resource_type"] != "pvc" for i in non_pvc_items)

    def test_no_pvc_items(self) -> None:
        items = [
            {"resource_type": "database", "component_name": "app"},
            {"resource_type": "bucket", "component_name": "app"},
        ]
        pvc_items = [i for i in items if i.get("resource_type") == "pvc"]
        non_pvc_items = [i for i in items if i.get("resource_type") != "pvc"]

        assert len(pvc_items) == 0
        assert len(non_pvc_items) == 2

    def test_only_pvc_items(self) -> None:
        items = [
            {"resource_type": "pvc", "component_name": "app", "storage_name": "data"},
            {"resource_type": "pvc", "component_name": "worker", "storage_name": "logs"},
        ]
        pvc_items = [i for i in items if i.get("resource_type") == "pvc"]
        non_pvc_items = [i for i in items if i.get("resource_type") != "pvc"]

        assert len(pvc_items) == 2
        assert len(non_pvc_items) == 0

    def test_empty_items(self) -> None:
        items: list[dict] = []
        pvc_items = [i for i in items if i.get("resource_type") == "pvc"]
        non_pvc_items = [i for i in items if i.get("resource_type") != "pvc"]

        assert len(pvc_items) == 0
        assert len(non_pvc_items) == 0


# ---------------------------------------------------------------------------
# build_restore_new_deployment_sections tests
# ---------------------------------------------------------------------------


class TestRestoreNewDeploymentSections:
    """Tests for build_restore_new_deployment_sections (info + components + domain)."""

    def test_returns_three_sections(self) -> None:
        from opi.forms.visualizers.wizard_sections import build_restore_new_deployment_sections

        sections = build_restore_new_deployment_sections(0)
        assert len(sections) == 3

    def test_each_section_visibility_new_mode(self) -> None:
        from opi.forms.visualizers.wizard_sections import build_restore_new_deployment_sections

        for section in build_restore_new_deployment_sections(0):
            assert callable(section.visible)
            assert section.visible({"restore_mode": "new"}) is True  # type: ignore[operator]

    def test_each_section_visibility_existing_mode(self) -> None:
        from opi.forms.visualizers.wizard_sections import build_restore_new_deployment_sections

        for section in build_restore_new_deployment_sections(0):
            assert callable(section.visible)
            assert section.visible({"restore_mode": "existing"}) is False  # type: ignore[operator]

    def test_each_section_visibility_no_mode(self) -> None:
        from opi.forms.visualizers.wizard_sections import build_restore_new_deployment_sections

        for section in build_restore_new_deployment_sections(0):
            assert callable(section.visible)
            assert section.visible({}) is False  # type: ignore[operator]

    def test_editables_have_materialized_paths(self) -> None:
        """Editables should use [0] not [*] in their paths."""
        from opi.forms.visualizers.wizard_sections import build_restore_new_deployment_sections

        for section in build_restore_new_deployment_sections(0):
            for vis in section.editables:
                path = vis.editable.yaml_path
                assert "[*]" not in path, f"Editable {path} still has wildcard"
                assert "[0]" in path, f"Editable {path} not materialized to [0]"

    def test_info_section_excludes_clone_from(self) -> None:
        """Restore flow's info section should not include the clone-from field."""
        from opi.forms.visualizers.wizard_sections import build_restore_new_deployment_sections

        info_section = build_restore_new_deployment_sections(0)[0]
        paths = [vis.editable.yaml_path for vis in info_section.editables]
        assert not any("clone-from" in p for p in paths)

    def test_sections_in_restore_flow(self) -> None:
        from opi.forms.visualizers.flows import build_restore_flow

        flow = build_restore_flow(0)
        section_ids = [s.section_id for s in flow.sections]
        # Expected order: restore-select, restore-target, then new-deployment sections
        assert section_ids[0] == "restore-select"
        assert section_ids[1] == "restore-target"
        assert any("add-deployment-info" in sid for sid in section_ids[2:])
        assert any("add-deployment-components" in sid for sid in section_ids[2:])


class TestRestoreNewDeploymentSummary:
    """Tests for _new_deployment_summary."""

    def test_with_deployment_name(self) -> None:
        from opi.forms.visualizers.wizard_sections import _new_deployment_summary

        data: dict[str, Any] = {"deployments": [{"name": "my-staging"}]}
        result = _new_deployment_summary(data)
        assert "my-staging" in result

    def test_without_deployments(self) -> None:
        from opi.forms.visualizers.wizard_sections import _new_deployment_summary

        data: dict[str, Any] = {}
        result = _new_deployment_summary(data)
        assert "-" in result

    def test_empty_deployments_list(self) -> None:
        from opi.forms.visualizers.wizard_sections import _new_deployment_summary

        data: dict[str, Any] = {"deployments": []}
        result = _new_deployment_summary(data)
        assert "-" in result
