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
from opi.services.catalog.base import ConfigLayer
from opi.services.catalog.namespace_postgres.config_model import NamespacePostgresConfig
from opi.services.config_schema import (
    fragment_path,
    layer_fragment_paths,
    render_model_schema,
    render_service_config_schema,
)
from opi.services.registry import SERVICES, get_service
from opi.services.services_enums import ServiceType
from pydantic import ValidationError

_PROVIDERS_WITH_CONFIG = [p for p in SERVICES.values() if p.config_model is not None]


class TestSchemaFragmentDriftLock:
    def test_at_least_one_service_is_modeled(self) -> None:
        # Guards the drift test below from silently passing on an empty list.
        assert _PROVIDERS_WITH_CONFIG, "expected at least one provider with a config_model"

    @pytest.mark.parametrize(
        "provider", _PROVIDERS_WITH_CONFIG, ids=[p.service_type.value for p in _PROVIDERS_WITH_CONFIG]
    )
    def test_committed_fragment_matches_model(self, provider) -> None:
        path = fragment_path(provider)
        assert path.exists(), (
            f"Missing committed schema fragment {path.name}. "
            f"Regenerate with `uv run python -m opi.services.config_schema`."
        )
        expected = render_service_config_schema(provider)
        assert path.read_text(encoding="utf-8") == expected, (
            f"Committed schema fragment for '{provider.service_type.value}' is stale. "
            f"Regenerate with `uv run python -m opi.services.config_schema` and review the diff."
        )


_LAYERED_PROVIDERS = [p for p in _PROVIDERS_WITH_CONFIG if layer_fragment_paths(p)]


class TestLayerFragmentDriftLock:
    """A service whose layers hold different shapes commits a fragment per layer (RC-60).

    ``config_model`` names one shape, and for most of the catalog that is the whole story.
    For a service where the layers answer different questions it is not: the single fragment
    then documents one of them and silently omits the rest, so a client reading it gets a
    field list that does not apply to the target it is writing to. The API already answers
    per layer; these files are the committed record of the same thing, under the same drift
    lock as the main fragment.
    """

    def test_a_service_with_per_layer_models_is_present(self) -> None:
        # Guards the parametrized test below from passing on an empty list.
        names = {p.service_type.value for p in _LAYERED_PROVIDERS}
        assert "publish-on-web" in names, f"expected publish-on-web to carry per-layer models, got {names}"

    @pytest.mark.parametrize("provider", _LAYERED_PROVIDERS, ids=[p.service_type.value for p in _LAYERED_PROVIDERS])
    def test_every_layer_fragment_matches_its_model(self, provider) -> None:
        for layer, path in layer_fragment_paths(provider).items():
            assert path.exists(), (
                f"Missing committed layer fragment {path.name}. "
                f"Regenerate with `uv run python -m opi.services.config_schema`."
            )
            model = provider.config_model_for(layer)
            assert model is not None
            assert path.read_text(encoding="utf-8") == render_model_schema(model), (
                f"Committed fragment for '{provider.service_type.value}' at layer '{layer.value}' is stale. "
                f"Regenerate with `uv run python -m opi.services.config_schema` and review the diff."
            )

    def test_publish_on_web_keeps_its_layers_apart(self) -> None:
        # The reason the split exists: one bag of ten optional fields would let `tls` land
        # on a deployment and `subdomain` on a component, and validate both times.
        service = get_service(ServiceType.PUBLISH_ON_WEB)

        def fields(layer: ConfigLayer) -> set[str]:
            model = service.config_model_for(layer)
            assert model is not None
            return {f.alias or name for name, f in model.model_fields.items()}

        assert "subdomain" in fields(ConfigLayer.DEPLOYMENT)
        assert "tls" not in fields(ConfigLayer.DEPLOYMENT)
        assert "tls" in fields(ConfigLayer.COMPONENT)
        assert "subdomain" not in fields(ConfigLayer.COMPONENT)
        assert "domains" in fields(ConfigLayer.PROJECT)
        # The project layer keeps tls: it is level 3 of the certificate cascade.
        assert "tls" in fields(ConfigLayer.PROJECT)
        assert "subdomain" not in fields(ConfigLayer.PROJECT)


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


class TestAuthorizationWallConfigModel:
    def _provider(self):
        return get_service(ServiceType.AUTHORIZATION_WALL)

    def test_banner_validates(self) -> None:
        assert self._provider().validate_config({"banner": "Beperkte toegang"}).banner == "Beperkte toegang"

    def test_empty_config_defaults_to_none_banner(self) -> None:
        assert self._provider().validate_config({}).banner is None

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            self._provider().validate_config({"bannre": "typo"})


class TestMetricsScraperConfigModel:
    def _provider(self):
        return get_service(ServiceType.METRICS_SCRAPER)

    def test_port_and_path_validate(self) -> None:
        model = self._provider().validate_config({"port": 8000, "path": "/metrics"})
        assert model.port == 8000
        assert model.path == "/metrics"

    def test_empty_config_defaults_to_none(self) -> None:
        model = self._provider().validate_config({})
        assert model.port is None
        assert model.path is None

    def test_non_int_port_rejected(self) -> None:
        with pytest.raises(ValidationError):
            self._provider().validate_config({"port": "not-a-number"})


class TestKeycloakConfigModel:
    """Keycloak is polymorphic (internal template vs external connection) with
    advanced pass-through; v1.0 must guardrail the common fields without rejecting
    any real-world shape."""

    def _provider(self):
        return get_service(ServiceType.KEYCLOAK)

    def test_internal_config_with_restrict_access_and_clients(self) -> None:
        # mb-docs-helmfile.yaml shape.
        model = self._provider().validate_config(
            {
                "template": "sso-support",
                "additional_redirect_uris": [],
                "additional-clients": [
                    {"name": "mb-grist-helmfile-production", "redirect-uris": ["https://grist.rijksapp.nl/*"]}
                ],
                "restrict-access": {"enabled": True, "realm-role": "allowed-user"},
            }
        )
        assert model.template == "sso-support"
        assert model.restrict_access is not None
        assert model.restrict_access.realm_role == "allowed-user"
        assert model.additional_clients[0].name == "mb-grist-helmfile-production"
        # advanced pass-through preserved (the hard 10% left hand-authored)
        assert model.additional_clients[0].model_extra["redirect-uris"] == ["https://grist.rijksapp.nl/*"]

    def test_external_polymorphic_config_is_accepted(self) -> None:
        # mb-grist-helmfile.yaml type: external -> different key set must not be rejected.
        model = self._provider().validate_config(
            {"host": "https://kc.example", "realm": "r", "client-id": "x", "client-secret": "y"}
        )
        assert model.model_extra["host"] == "https://kc.example"

    def test_account_link_enum(self) -> None:
        model = self._provider().validate_config({"account-link": "automatic"})
        assert model.account_link is not None
        assert model.account_link.value == "automatic"

    def test_bad_account_link_rejected(self) -> None:
        with pytest.raises(ValidationError):
            self._provider().validate_config({"account-link": "nope"})

    def test_restrict_access_unknown_field_rejected(self) -> None:
        # restrict-access keys are well-defined -> extras forbidden (catches typos).
        with pytest.raises(ValidationError):
            self._provider().validate_config({"restrict-access": {"enabled": True, "realmrole": "x"}})


class TestStorageConfigModel:
    """persistent-storage and temp-storage share StorageConfig (a list of mounts)."""

    def test_persistent_storage_list_validates(self) -> None:
        model = get_service(ServiceType.PERSISTENT_STORAGE).validate_config(
            [{"name": "data", "size": "500Mi", "mount-path": "/data"}]
        )
        assert model.root[0].name == "data"
        assert model.root[0].mount_path == "/data"

    def test_temp_storage_shares_the_same_model(self) -> None:
        p_model = get_service(ServiceType.PERSISTENT_STORAGE).config_model
        t_model = get_service(ServiceType.TEMP_STORAGE).config_model
        assert p_model is t_model

    def test_missing_required_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            get_service(ServiceType.PERSISTENT_STORAGE).validate_config([{"name": "data", "size": "500Mi"}])

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            get_service(ServiceType.TEMP_STORAGE).validate_config(
                [{"name": "t", "size": "1Gi", "mount-path": "/tmp", "extra": 1}]
            )


class TestProviderValidateConfig:
    def _provider(self):
        return get_service(ServiceType.NAMESPACE_POSTGRESQL_DATABASE)

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
        # A service that takes no config must not silently accept one. publish-on-web used
        # to be the example here, until it got a config model; namespace-redis and platform
        # are the two behaviour-only services left.
        with pytest.raises(TypeError):
            get_service(ServiceType.NAMESPACE_REDIS).validate_config({"anything": 1})

    def test_provider_with_config_model_rejects_a_stray_key(self) -> None:
        # The other side: a modelled service fails validation instead of raising TypeError.
        with pytest.raises(ValidationError):
            get_service(ServiceType.PUBLISH_ON_WEB).validate_config({"anything": 1})


class TestConfigModelAndVersionArePaired:
    """``config_schema_version`` used to default to ``"1.0"`` on the base class.

    Every behaviour-only service therefore advertised a versioned config contract it did
    not have, which is misleading in exactly the place a reader goes looking for one. The
    two must travel together: a service either declares both or neither.
    """

    def test_a_modelled_service_declares_a_version(self) -> None:
        missing = [t.value for t, s in SERVICES.items() if s.config_model is not None and not s.config_schema_version]
        assert not missing, f"config_model without config_schema_version: {missing}"

    def test_a_service_without_a_model_declares_no_version(self) -> None:
        stray = [t.value for t, s in SERVICES.items() if s.config_model is None and s.config_schema_version]
        assert not stray, f"config_schema_version without a config_model: {stray}"
