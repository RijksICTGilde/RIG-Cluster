"""Typed service config + committed schema-fragment guards (RC-5 Phase 2).

Covers:
* the per-service schema drift lock -- committed fragment must equal what the
  provider's model renders, so model and schema can't diverge;
* the ``namespace-postgresql-database`` config model faithfully reproducing today's
  DatabaseManager behaviour (defaults, required fields, privilege allow-list, the
  resources deep-merge) while adding value guardrails;
* the provider ``validate_config`` migrate-then-validate path.
"""

import pytest
from opi.services.config_models.namespace_postgres import NamespacePostgresConfig
from opi.services.config_schema import fragment_path, render_service_config_schema
from opi.services.registry import SERVICE_PROVIDERS, get_provider
from opi.services.services_enums import ServiceType
from pydantic import ValidationError

_PROVIDERS_WITH_CONFIG = [p for p in SERVICE_PROVIDERS.values() if p.config_model is not None]


class TestSchemaFragmentDriftLock:
    def test_at_least_one_service_is_modeled(self) -> None:
        # Guards the drift test below from silently passing on an empty list.
        assert _PROVIDERS_WITH_CONFIG, "expected at least one provider with a config_model"

    @pytest.mark.parametrize(
        "provider", _PROVIDERS_WITH_CONFIG, ids=[p.service_type.value for p in _PROVIDERS_WITH_CONFIG]
    )
    def test_committed_fragment_matches_model(self, provider) -> None:
        path = fragment_path(provider.service_type.value, provider.config_schema_version)
        assert path.exists(), (
            f"Missing committed schema fragment {path.name}. "
            f"Regenerate with `uv run python -m opi.services.config_schema`."
        )
        expected = render_service_config_schema(provider)
        assert path.read_text(encoding="utf-8") == expected, (
            f"Committed schema fragment for '{provider.service_type.value}' is stale. "
            f"Regenerate with `uv run python -m opi.services.config_schema` and review the diff."
        )


class TestNamespacePostgresConfigModel:
    def test_defaults_reproduce_default_config(self) -> None:
        # These are exactly DatabaseManager.DEFAULT_CONFIG's values.
        m = NamespacePostgresConfig()
        assert m.image == "ghcr.io/cloudnative-pg/postgresql:17"
        assert m.instances == 1
        assert m.storage == "10Gi"
        assert m.privileges == []
        assert m.postInitSQL == []
        assert m.registry is None
        assert m.resources.requests.memory == "256Mi"
        assert m.resources.requests.cpu == "100m"
        assert m.resources.limits.memory == "512Mi"
        assert m.resources.limits.cpu == "500m"

    def test_real_production_config_validates(self) -> None:
        real = {
            "image": "ghcr.io/rijksictgilde/algoritmeregister/postgresql-with-dictionaries:2024.11.19",
            "registry": "github-registry",
            "instances": 1,
            "storage": "1Gi",
            "privileges": ["SUPERUSER"],
        }
        m = NamespacePostgresConfig.model_validate(real)
        assert m.privileges[0].value == "SUPERUSER"
        assert m.registry == "github-registry"

    def test_partial_resources_override_keeps_per_side_defaults(self) -> None:
        # Old deep-merge behaviour: overriding only requests.memory keeps other values.
        m = NamespacePostgresConfig.model_validate({"resources": {"requests": {"memory": "1Gi"}}})
        assert m.resources.requests.memory == "1Gi"
        assert m.resources.requests.cpu == "100m"
        assert m.resources.limits.memory == "512Mi"

    def test_invalid_privilege_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            NamespacePostgresConfig.model_validate({"privileges": ["ROOT"]})

    def test_instances_below_one_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            NamespacePostgresConfig.model_validate({"instances": 0})

    def test_unknown_field_is_rejected(self) -> None:
        # extra="forbid" catches config typos like "instaces".
        with pytest.raises(ValidationError):
            NamespacePostgresConfig.model_validate({"instaces": 2})


class TestProviderValidateConfig:
    def _provider(self):
        return get_provider(ServiceType.NAMESPACE_POSTGRESQL_DATABASE)

    def test_schema_version_is_declared(self) -> None:
        assert self._provider().config_schema_version == "1.0"

    def test_validate_config_returns_typed_model(self) -> None:
        model = self._provider().validate_config({"instances": 2, "storage": "5Gi"})
        assert isinstance(model, NamespacePostgresConfig)
        assert model.instances == 2
        assert model.storage == "5Gi"

    def test_validate_config_none_yields_defaults(self) -> None:
        model = self._provider().validate_config(None)
        assert isinstance(model, NamespacePostgresConfig)
        assert model.instances == 1

    def test_validate_config_rejects_bad_values(self) -> None:
        with pytest.raises(ValidationError):
            self._provider().validate_config({"instances": -1})

    def test_migrate_config_default_is_identity(self) -> None:
        cfg = {"instances": 3}
        assert self._provider().migrate_config(cfg, "1.0") == cfg

    def test_provider_without_config_model_raises(self) -> None:
        # A service that takes no config must not silently accept one.
        with pytest.raises(TypeError):
            get_provider(ServiceType.PUBLISH_ON_WEB).validate_config({"anything": 1})
