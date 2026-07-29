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


def test_valid_health_check_component_config_passes():
    # health-check uses the config: wrapper (like publish-on-web/attachments).
    data = {
        "name": "p",
        "components": [
            {
                "name": "dirmgr",
                "services": [
                    {
                        "name": "health-check",
                        "config": {
                            "scheme": "http",
                            "port": 8080,
                            "liveness-path": "/health/live",
                            "readiness-path": "/health/ready",
                        },
                    }
                ],
            }
        ],
    }
    validate_service_configs(data)  # must not raise


def test_invalid_health_check_scheme_rejected():
    # scheme is a closed set; an unknown value fails HealthCheckConfig and names the component.
    data = {
        "name": "p",
        "components": [{"name": "dirmgr", "services": [{"name": "health-check", "config": {"scheme": "grpc"}}]}],
    }
    with pytest.raises(ProjectIntegrityError, match="component 'dirmgr'"):
        validate_service_configs(data)


def test_unknown_health_check_field_rejected():
    # extra="forbid": an unknown key is rejected rather than silently ignored.
    data = {
        "name": "p",
        "components": [{"name": "dirmgr", "services": [{"name": "health-check", "config": {"timeout": 5}}]}],
    }
    with pytest.raises(ProjectIntegrityError, match="health-check"):
        validate_service_configs(data)


def test_health_check_path_with_yaml_injection_rejected():
    # The paths reach the pod spec interpolated unquoted, so they carry the same
    # absolute-path pattern every other manifest-bound string in project_v2.json has.
    # A newline + a forged sibling key must not pass validation.
    data = {
        "name": "p",
        "components": [
            {
                "name": "dirmgr",
                "services": [
                    {
                        "name": "health-check",
                        "config": {"scheme": "http", "liveness-path": "/health/live\n              privileged: true"},
                    }
                ],
            }
        ],
    }
    with pytest.raises(ProjectIntegrityError, match="health-check"):
        validate_service_configs(data)


def test_health_check_readiness_path_without_leading_slash_rejected():
    # The pattern requires a leading slash; a relative path is rejected.
    data = {
        "name": "p",
        "components": [
            {"name": "dirmgr", "services": [{"name": "health-check", "config": {"readiness-path": "health/ready"}}]}
        ],
    }
    with pytest.raises(ProjectIntegrityError, match="health-check"):
        validate_service_configs(data)


def test_health_check_port_out_of_range_rejected():
    # port carries a 1-65535 bound so an out-of-range value fails early with a clear error.
    data = {
        "name": "p",
        "components": [{"name": "dirmgr", "services": [{"name": "health-check", "config": {"port": 70000}}]}],
    }
    with pytest.raises(ProjectIntegrityError, match="health-check"):
        validate_service_configs(data)


def test_entry_schema_version_is_threaded_to_validate_config(monkeypatch):
    """The entry's stamped schema-version must reach the provider's validate_config.

    Without threading, a config block stored at an older version would be validated
    against the current model without being migrated forward first. This captures the
    ``from_version`` passed to the provider for both a project-level and a
    component-level entry.
    """
    from opi.services.registry import get_service
    from opi.services.services_enums import ServiceType

    captured: list[str | None] = []
    keycloak = get_service(ServiceType.KEYCLOAK)
    original = keycloak.validate_config

    def spy(raw_config=None, from_version=None):
        captured.append(from_version)
        return original(raw_config, from_version=from_version)

    monkeypatch.setattr(keycloak, "validate_config", spy)

    data = {
        "name": "p",
        "services": [{"name": "keycloak", "config": {"template": "sso-only"}, "schema-version": "2.0"}],
    }
    validate_service_configs(data)
    assert captured == ["2.0"]
