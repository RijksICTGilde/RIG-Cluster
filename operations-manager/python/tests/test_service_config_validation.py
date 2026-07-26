"""Tests for the per-service config-validation chokepoint (RC-5 A).

validate_service_configs runs inside validate_project_structure (the fail-closed
gate both ProjectManager and ProjectStore use), enforcing each service's config
against its provider's typed model -- at BOTH the project level (service
definitions) and the component level (storage mounts, metrics port/path).
"""

import pytest
from opi.core.project_schema import ProjectIntegrityError
from opi.manager.project_validation import validate_service_configs


def test_valid_configs_pass():
    data = {
        "name": "p",
        "services": [
            "publish-on-web",
            "persistent-storage",  # bare, no project-level config -> skipped
            {"keycloak": {"config": {"template": "sso-only", "restrict-access": {"enabled": True, "realm-role": "x"}}}},
            {
                "namespace-postgresql-database": {
                    "config": {"instances": 1, "storage": "1Gi", "privileges": ["SUPERUSER"]}
                }
            },
        ],
    }
    validate_service_configs(data)  # must not raise


def test_bare_service_without_config_is_skipped():
    validate_service_configs({"name": "p", "services": ["keycloak", "persistent-storage"]})


def test_invalid_namespace_postgres_rejected():
    data = {"name": "p", "services": [{"namespace-postgresql-database": {"config": {"instances": -1}}}]}
    with pytest.raises(ProjectIntegrityError, match="namespace-postgresql-database"):
        validate_service_configs(data)


def test_invalid_keycloak_rejected():
    data = {"name": "p", "services": [{"keycloak": {"config": {"account-link": "nope"}}}]}
    with pytest.raises(ProjectIntegrityError, match="keycloak"):
        validate_service_configs(data)


def test_unknown_service_name_is_skipped():
    # Unknown names are handled by other validation, not here.
    validate_service_configs({"name": "p", "services": [{"not-a-service": {"config": {"x": 1}}}]})


def test_service_without_config_model_with_config_is_skipped():
    # minio takes no typed config; a stray config block is ignored here (not rejected).
    validate_service_configs({"name": "p", "services": [{"minio-storage": {"config": {"anything": 1}}}]})


# --- component-level config validation (RC-5 A: storage mounts, metrics port/path) ---


def test_valid_component_configs_pass():
    data = {
        "name": "p",
        "components": [
            {
                "name": "c1",
                "services": [
                    "publish-on-web",  # bare reference -> skipped
                    {"name": "persistent-storage", "config": [{"name": "data", "size": "1Gi", "mount-path": "/data"}]},
                    {"metrics-scraper": {"port": 8000, "path": "/metrics"}},  # inline shape
                ],
            }
        ],
    }
    validate_service_configs(data)  # must not raise


def test_invalid_component_storage_rejected():
    # A storage mount missing the required mount-path is rejected, naming the component.
    data = {
        "name": "p",
        "components": [
            {"name": "web", "services": [{"name": "temp-storage", "config": [{"name": "tmp", "size": "100Mi"}]}]}
        ],
    }
    with pytest.raises(ProjectIntegrityError, match="component 'web'"):
        validate_service_configs(data)


def test_invalid_metrics_component_rejected_with_accepted_fields_hint():
    # A non-int port fails MetricsScraperConfig; the message lists the accepted fields.
    data = {
        "name": "p",
        "components": [{"name": "api", "services": [{"metrics-scraper": {"port": "not-an-int"}}]}],
    }
    with pytest.raises(ProjectIntegrityError, match="Geaccepteerde velden: port, path"):
        validate_service_configs(data)


def test_bare_component_service_reference_is_skipped():
    # A component that only references services (no config) has nothing to validate.
    validate_service_configs(
        {"name": "p", "components": [{"name": "c", "services": ["keycloak", "persistent-storage"]}]}
    )
