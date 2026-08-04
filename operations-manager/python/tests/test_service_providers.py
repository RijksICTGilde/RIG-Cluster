"""Provider-coverage guard (RC-5 Phase 1).

This is the guardrail that keeps ``SERVICES`` the single source of truth
for services: adding a ``ServiceType`` without a provider fails CI here, so the
registry can never silently fall behind the enum. It also asserts each provider
carries the same ``ServiceDefinition`` object the rest of the app already reads,
so the provider abstraction composes with today's design instead of forking it.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from opi.services.catalog.base import (
    ConfigLayer,
    ManifestContext,
    ProvisionContext,
    RemovalContext,
    Service,
    config_path,
)
from opi.services.registry import (
    SERVICES,
    get_service,
    manifest_secret_services,
    manifest_services,
    provisioning_services,
)
from opi.services.services import ServiceAdapter
from opi.services.services_enums import ManagerKey, ServiceType
from opi.utils.secrets import DatabaseSecret, MinIOSecret, RedisSecret


def test_every_service_type_has_a_provider() -> None:
    """Every ServiceType must be registered -- this is the coverage guard."""
    missing = [t for t in ServiceType if t not in SERVICES]
    assert not missing, (
        f"ServiceType(s) without a provider: {[t.value for t in missing]}. "
        f"Add a Service subclass and register it in opi/services/registry.py."
    )


def test_no_extra_providers() -> None:
    """The registry must not carry providers for non-existent service types."""
    extra = [t for t in SERVICES if t not in set(ServiceType)]
    assert not extra, f"Registry has providers for unknown types: {extra}"


def test_provider_definition_is_the_shared_service_definition() -> None:
    """Each provider's definition is the exact object in SERVICE_DEFINITIONS.

    The provider composes with the existing metadata registry; it does not clone
    it. Identity (``is``) guarantees no drift.
    """
    for t in ServiceType:
        provider = get_service(t)
        assert provider.definition is ServiceAdapter.SERVICE_DEFINITIONS[t], (
            f"Provider for {t.value} does not carry the shared ServiceDefinition"
        )


def test_provider_service_type_matches_registry_key() -> None:
    for t, provider in SERVICES.items():
        assert provider.service_type == t, (
            f"Provider registered under {t.value} reports service_type {provider.service_type.value}"
        )


def test_providers_are_service_provider_instances() -> None:
    for provider in SERVICES.values():
        assert isinstance(provider, Service)


def test_get_provider_returns_registered_instance() -> None:
    for t in ServiceType:
        assert get_service(t) is SERVICES[t]


# ---------------------------------------------------------------------------
# Phase 4: generic provisioning dispatch
# ---------------------------------------------------------------------------


def test_provisioning_providers_order_matches_legacy_sequence():
    # Must reproduce today's fixed db -> minio -> keycloak -> redis order.
    assert [p.service_type.value for p in provisioning_services()] == [
        "postgresql-database",
        "minio-storage",
        "keycloak",
        "redis",
    ]


def test_namespace_variants_do_not_double_provision():
    # namespace-postgres and namespace-redis share a manager with their primary and
    # must NOT override provision (else the manager would be called twice).
    provisioning = {p.service_type for p in provisioning_services()}
    assert ServiceType.NAMESPACE_POSTGRESQL_DATABASE not in provisioning
    assert ServiceType.NAMESPACE_REDIS not in provisioning


def test_provision_delegates_to_the_right_manager():
    db, minio, keycloak, redis = (AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock())
    ctx = ProvisionContext(
        project_data={"name": "p"},
        deployment={"name": "d"},
        force_clone=True,
        database_manager=SimpleNamespace(create_resources_for_deployment=db),
        minio_manager=SimpleNamespace(create_resources_for_deployment=minio),
        keycloak_manager=SimpleNamespace(create_resources_for_deployment=keycloak),
        redis_manager=SimpleNamespace(create_resources_for_deployment=redis),
    )

    async def run():
        for provider in provisioning_services():
            await provider.provision(ctx)

    asyncio.run(run())
    # same calls + args as the old fixed sequence
    db.assert_awaited_once_with({"name": "p"}, {"name": "d"}, True)
    minio.assert_awaited_once_with({"name": "p"}, {"name": "d"}, True)
    keycloak.assert_awaited_once_with({"name": "p"}, {"name": "d"})
    redis.assert_awaited_once_with({"name": "p"}, {"name": "d"})


def test_default_provision_is_noop():
    async def run():
        await get_service(ServiceType.PUBLISH_ON_WEB).provision(None)  # type: ignore[arg-type]

    asyncio.run(run())  # must not raise


# ---------------------------------------------------------------------------
# Phase 5: generic cleanup dispatch
# ---------------------------------------------------------------------------

# Frozen copy of the old DeleteProjectManager._SERVICE_TYPE_MANAGER_ATTR map. The
# provider registry now owns this mapping via cleanup_manager_key; this guard fails
# if a provider's key ever drifts from the dispatch the map used to perform.
_LEGACY_SERVICE_MANAGER_KEYS = {
    ServiceType.POSTGRESQL_DATABASE: ManagerKey.DATABASE,
    ServiceType.NAMESPACE_POSTGRESQL_DATABASE: ManagerKey.DATABASE,
    ServiceType.MINIO_STORAGE: ManagerKey.MINIO,
    ServiceType.REDIS: ManagerKey.REDIS,
    ServiceType.NAMESPACE_REDIS: ManagerKey.REDIS,
    ServiceType.KEYCLOAK: ManagerKey.KEYCLOAK,
    ServiceType.PERSISTENT_STORAGE: ManagerKey.PVC,
}


def test_cleanup_manager_key_matches_legacy_map():
    actual = {t: p.cleanup_manager_key for t, p in SERVICES.items() if p.cleanup_manager_key is not None}
    assert actual == _LEGACY_SERVICE_MANAGER_KEYS


def test_services_without_cleanup_have_no_manager_key():
    # Anything not in the legacy map must have cleanup_manager_key None (no server-side
    # resources), so the generic dispatch skips it exactly as the old `.get() is None`
    # guard did.
    for t in ServiceType:
        if t not in _LEGACY_SERVICE_MANAGER_KEYS:
            assert get_service(t).cleanup_manager_key is None, t.value


def test_handle_service_removal_delegates_to_resolved_manager():

    manager = AsyncMock()
    manager.handle_service_removal.return_value = {"errors": []}
    resolved_keys = []

    async def get_manager(key):
        resolved_keys.append(key)
        return manager

    ctx = RemovalContext(
        project_name="p",
        deployment_name="d",
        deployment_data={"name": "d"},
        project_data={"name": "p"},
        marked_for_deletion_service=None,
        get_manager=get_manager,
    )

    async def run():
        return await get_service(ServiceType.MINIO_STORAGE).handle_service_removal(ctx)

    result = asyncio.run(run())
    assert result == {"errors": []}
    assert resolved_keys == [ManagerKey.MINIO]
    manager.handle_service_removal.assert_awaited_once_with(
        project_name="p",
        deployment_name="d",
        deployment_data={"name": "d"},
        project_data={"name": "p"},
        marked_for_deletion_service=None,
    )


def test_handle_service_removal_noop_without_manager_key():

    called = False

    async def get_manager(key):
        nonlocal called
        called = True
        return None

    ctx = RemovalContext(
        project_name="p",
        deployment_name="d",
        deployment_data=None,
        project_data={"name": "p"},
        marked_for_deletion_service=None,
        get_manager=get_manager,
    )

    async def run():
        return await get_service(ServiceType.PUBLISH_ON_WEB).handle_service_removal(ctx)

    assert asyncio.run(run()) == {}
    assert called is False  # a keyless service never resolves a manager


# ---------------------------------------------------------------------------
# Phase 6a: generic envFrom-secret manifest contribution
# ---------------------------------------------------------------------------

# Frozen contract: the exact envFrom secret-name sequence the hand-written append
# block produced for deployment "mydep", in order. This is the byte-identity the
# golden-manifest harness protects at the render layer; this test pins it at the
# provider layer so a drift is caught here too.
_EXPECTED_ENVFROM_ORDER = [
    ("postgresql-database", "mydep-database"),
    ("minio-storage", "mydep-minio"),
    ("keycloak", "mydep-keycloak"),
    ("redis", "mydep-redis"),
    ("metrics-scraper", "mydep-metrics-auth"),
]


def _manifest_ctx(
    *,
    deployment_name="mydep",
    project_data=None,
    unique_name="mydep-web",
    cluster="sandboxed-local",
    get_secret=None,
    component_def=None,
):
    return ManifestContext(
        deployment_name=deployment_name,
        project_data=project_data if project_data is not None else {},
        unique_name=unique_name,
        cluster=cluster,
        get_secret=get_secret if get_secret is not None else (lambda *a, **k: None),
        component_def=component_def,
    )


def test_manifest_secret_providers_order_and_names():
    ctx = _manifest_ctx()
    actual = [
        (p.service_type.value, p.contribute_manifest_context(ctx).env_from_secrets[0])
        for p in manifest_secret_services()
    ]
    assert actual == _EXPECTED_ENVFROM_ORDER


def test_shared_providers_activate_for_namespace_variant():
    # postgres and redis contribute their single envFrom secret for BOTH the shared
    # and the namespace variant, so exactly one secret is added per manager.
    pg = get_service(ServiceType.POSTGRESQL_DATABASE)
    assert set(pg.manifest_activation_types()) == {
        ServiceType.POSTGRESQL_DATABASE,
        ServiceType.NAMESPACE_POSTGRESQL_DATABASE,
    }
    redis = get_service(ServiceType.REDIS)
    assert set(redis.manifest_activation_types()) == {ServiceType.REDIS, ServiceType.NAMESPACE_REDIS}


def test_default_manifest_activation_is_own_service_type():
    kc = get_service(ServiceType.KEYCLOAK)
    assert kc.manifest_activation_types() == (ServiceType.KEYCLOAK,)


def test_non_secret_services_contribute_no_envfrom():
    ctx = _manifest_ctx()
    for t in ServiceType:
        provider = get_service(t)
        if provider.manifest_secret_class is None:
            assert provider.contribute_manifest_context(ctx).env_from_secrets == [], t.value


def test_namespace_variants_are_not_separate_contributors():
    # The shared provider owns the contribution; the namespace variants must not carry
    # their own manifest_secret_class (else the secret would be added twice).
    assert get_service(ServiceType.NAMESPACE_POSTGRESQL_DATABASE).manifest_secret_class is None
    assert get_service(ServiceType.NAMESPACE_REDIS).manifest_secret_class is None
    contributors = {p.service_type for p in manifest_secret_services()}
    assert ServiceType.NAMESPACE_POSTGRESQL_DATABASE not in contributors
    assert ServiceType.NAMESPACE_REDIS not in contributors


# ---------------------------------------------------------------------------
# Phase 6b: auth-wall sidecar + service_port override contribution
# ---------------------------------------------------------------------------


def test_manifest_providers_includes_auth_wall_after_secrets():
    # The override providers (health-check, auth-wall) join the contributor set after
    # the envFrom-secret services, ordered by manifest_order.
    order = [p.service_type.value for p in manifest_services()]
    assert order == [
        "postgresql-database",
        "minio-storage",
        "keycloak",
        "redis",
        "metrics-scraper",
        "health-check",
        "authorization-wall",
    ]


def test_auth_wall_contributes_sidecar_and_port_override():
    keycloak_secret = SimpleNamespace(
        discovery_url="https://kc.example/realms/r/.well-known/openid-configuration",
        client_id="my-client",
    )
    resolved = []

    def get_secret(deployment_name, secret_type, secret_class):
        resolved.append((deployment_name, secret_type))
        return keycloak_secret

    ctx = _manifest_ctx(
        deployment_name="mydep",
        unique_name="mydep-web",
        project_data={"services": [{"reference": "authorization-wall", "config": {"banner": "Restricted"}}]},
        get_secret=get_secret,
    )
    contribution = get_service(ServiceType.AUTHORIZATION_WALL).contribute_manifest_context(ctx)

    # Byte-identical to the old manager block: sidecar added, service_port overridden,
    # authorization_wall dict built from the resolved keycloak secret + project banner.
    assert contribution.sidecars == ["authorization-wall"]
    assert contribution.template_vars["service_port"] == 4180
    assert contribution.template_vars["authorization_wall"] == {
        "issuer_url": "https://kc.example/realms/r",
        "client_id": "my-client",
        "keycloak_secret_name": "mydep-keycloak",
        "cookie_secret_name": "mydep-web-oauth2-cookie",
        "banner": "Restricted",
    }
    assert resolved == [("mydep", "keycloak")]


def test_auth_wall_contributes_nothing_without_keycloak_secret():
    # Component asks for an auth wall but no keycloak secret is provisioned: the
    # provider contributes nothing (the manager logs the warning), matching the old
    # warn-and-skip branch -- no sidecar, no port override.
    ctx = _manifest_ctx(get_secret=lambda *a, **k: None)
    contribution = get_service(ServiceType.AUTHORIZATION_WALL).contribute_manifest_context(ctx)
    assert contribution.sidecars == []
    assert contribution.template_vars == {}
    assert contribution.env_from_secrets == []


# ---------------------------------------------------------------------------
# Phase 6c: credential secret-file contribution
# ---------------------------------------------------------------------------


def _fake_creds():
    # Superset of every credential field the cred providers read.
    return SimpleNamespace(
        host="db.host",
        port=5432,
        username="u",
        password="p",
        database="d",
        schema="s",
        extra_schemas=[],
        access_key="ak",
        secret_key="sk",
        bucket_name="bucket",
        region="eu",
        key_prefix="kp",
    )


def test_postgres_build_secret_file():
    creds = _fake_creds()
    ctx = _manifest_ctx(get_secret=lambda dep, t, cls: creds if t == "database" else None)
    specs = get_service(ServiceType.POSTGRESQL_DATABASE).build_secret_files(ctx)
    assert len(specs) == 1
    spec = specs[0]
    assert spec.secret_name == "mydep-database"
    assert spec.secret_type == "database"
    assert spec.resolve_aliases is True
    # postgres is the only service that re-registers its secret in the deployment map.
    assert isinstance(spec.register_secret, DatabaseSecret)
    expected = DatabaseSecret(
        host="db.host", port=5432, username="u", password="p", database="d", schema="s"
    ).to_k8s_secret_data()
    assert spec.secret_pairs == expected


def test_minio_build_secret_file_uses_cluster_host():
    creds = _fake_creds()
    ctx = _manifest_ctx(cluster="sandboxed-local", get_secret=lambda dep, t, cls: creds if t == "minio" else None)
    specs = get_service(ServiceType.MINIO_STORAGE).build_secret_files(ctx)
    assert len(specs) == 1
    spec = specs[0]
    assert spec.secret_name == "mydep-minio"
    assert spec.secret_type == "minio"
    assert spec.resolve_aliases is True
    assert spec.register_secret is None
    # host/port come from cluster config, not the creds.
    from opi.core.cluster_config import get_minio_host, get_minio_port

    expected = MinIOSecret(
        host=get_minio_host("sandboxed-local"),
        port=get_minio_port("sandboxed-local"),
        access_key="ak",
        secret_key="sk",
        bucket_name="bucket",
        region="eu",
    ).to_k8s_secret_data()
    assert spec.secret_pairs == expected


def test_redis_build_secret_file():
    creds = _fake_creds()
    ctx = _manifest_ctx(get_secret=lambda dep, t, cls: creds if t == "redis" else None)
    specs = get_service(ServiceType.REDIS).build_secret_files(ctx)
    assert len(specs) == 1
    spec = specs[0]
    assert spec.secret_name == "mydep-redis"
    assert spec.secret_type == "redis"
    assert spec.resolve_aliases is True
    assert spec.register_secret is None
    expected = RedisSecret(host="db.host", port=5432, username="u", password="p", key_prefix="kp").to_k8s_secret_data()
    assert spec.secret_pairs == expected


def test_credential_providers_no_creds_no_secret_files():
    ctx = _manifest_ctx(get_secret=lambda *a, **k: None)
    for t in (ServiceType.POSTGRESQL_DATABASE, ServiceType.MINIO_STORAGE, ServiceType.REDIS):
        assert get_service(t).build_secret_files(ctx) == [], t.value


def test_metrics_secret_file_gated_on_token(monkeypatch):
    from opi.services.catalog import metrics_scraper as metrics_module

    ctx = _manifest_ctx()
    monkeypatch.setattr(metrics_module.settings, "PROMETHEUS_METRICS_AUTH_TOKEN", None)
    assert get_service(ServiceType.METRICS_SCRAPER).build_secret_files(ctx) == []

    monkeypatch.setattr(metrics_module.settings, "PROMETHEUS_METRICS_AUTH_TOKEN", "tok")
    specs = get_service(ServiceType.METRICS_SCRAPER).build_secret_files(ctx)
    assert len(specs) == 1
    assert specs[0].secret_name == "mydep-metrics-auth"
    assert specs[0].resolve_aliases is False
    assert specs[0].register_secret is None


def test_auth_wall_contributes_cookie_secret_file():
    keycloak_secret = SimpleNamespace(
        discovery_url="https://kc/realms/r/.well-known/openid-configuration", client_id="c"
    )
    ctx = _manifest_ctx(unique_name="mydep-web", get_secret=lambda *a, **k: keycloak_secret)
    contribution = get_service(ServiceType.AUTHORIZATION_WALL).contribute_manifest_context(ctx)
    assert len(contribution.secret_files) == 1
    spec = contribution.secret_files[0]
    assert spec.secret_name == "mydep-web-oauth2-cookie"
    assert set(spec.secret_pairs) == {"cookie-secret"}
    assert spec.secret_pairs["cookie-secret"]  # a non-empty random token
    assert spec.resolve_aliases is False


def test_non_secret_file_services_declare_none():
    ctx = _manifest_ctx()
    for t in (ServiceType.KEYCLOAK, ServiceType.PLATFORM, ServiceType.PUBLISH_ON_WEB, ServiceType.PERSISTENT_STORAGE):
        assert get_service(t).build_secret_files(ctx) == [], t.value


# ---------------------------------------------------------------------------
# Metrics scraper: scrape port/path reach the manifest (bug fix, not byte-identical)
# ---------------------------------------------------------------------------


def test_metrics_contributes_configured_port_and_path():
    # The component's configured scrape port/path must reach the deployment template
    # (previously they were silently dropped and fell back to the application port).
    component_def = {"services": [{"reference": "metrics-scraper", "config": {"port": 9090, "path": "/m"}}]}
    ctx = _manifest_ctx(component_def=component_def)
    contribution = get_service(ServiceType.METRICS_SCRAPER).contribute_manifest_context(ctx)
    assert contribution.template_vars["metrics_config"] == {"port": 9090, "path": "/m"}
    # still contributes its envFrom auth secret (6a) alongside the template var.
    assert contribution.env_from_secrets == ["mydep-metrics-auth"]


def test_metrics_contributes_nulls_when_unconfigured():
    # No component config -> {port: None, path: None}; the template then falls back to
    # the application port / "/metrics" (same end result as before, now explicit).
    ctx = _manifest_ctx(component_def={"services": [{"reference": "metrics-scraper"}]})
    contribution = get_service(ServiceType.METRICS_SCRAPER).contribute_manifest_context(ctx)
    assert contribution.template_vars["metrics_config"] == {"port": None, "path": None}


# --- health-check service (component-level probe override) -----------------------


def test_health_check_overrides_only_the_keys_the_user_set():
    # A full config: scheme + a separate monitoring port + both paths must all reach the
    # deployment template as probe_* overrides. The service adds no secret (behaviour-only).
    component_def = {
        "ports": {"inbound": [8443]},
        "services": [
            {
                "reference": "health-check",
                "config": {
                    "scheme": "http",
                    "port": 8080,
                    "liveness-path": "/health/live",
                    "readiness-path": "/health/ready",
                },
            }
        ],
    }
    ctx = _manifest_ctx(component_def=component_def)
    contribution = get_service(ServiceType.HEALTH_CHECK).contribute_manifest_context(ctx)
    assert contribution.template_vars == {
        "probe_scheme": "http",
        "probe_port": 8080,
        "probe_liveness_path": "/health/live",
        "probe_readiness_path": "/health/ready",
    }
    assert contribution.env_from_secrets == []


def test_health_check_partial_config_sets_only_present_keys():
    # dirui-style: paths but no explicit port. Only the keys the user filled in are
    # overridden; the rest fall back to the generic base / template defaults.
    component_def = {
        "ports": {"inbound": [8080]},
        "services": [
            {
                "reference": "health-check",
                "config": {"scheme": "http", "liveness-path": "/health/live", "readiness-path": "/health/ready"},
            }
        ],
    }
    ctx = _manifest_ctx(component_def=component_def)
    contribution = get_service(ServiceType.HEALTH_CHECK).contribute_manifest_context(ctx)
    assert "probe_port" not in contribution.template_vars
    assert contribution.template_vars == {
        "probe_scheme": "http",
        "probe_liveness_path": "/health/live",
        "probe_readiness_path": "/health/ready",
    }


def test_health_check_disables_probes_with_scheme_none():
    # Disabling probes is done by ADDING the service with scheme: none.
    component_def = {
        "ports": {"inbound": [8443]},
        "services": [{"reference": "health-check", "config": {"scheme": "none"}}],
    }
    ctx = _manifest_ctx(component_def=component_def)
    contribution = get_service(ServiceType.HEALTH_CHECK).contribute_manifest_context(ctx)
    assert contribution.template_vars == {"probe_scheme": "none"}


def test_health_check_no_inbound_port_contributes_nothing():
    # A component without an inbound port cannot be probed by the kubelet; the service
    # must not resurrect a probe even when it is selected (generic code forces none).
    component_def = {
        "ports": {"inbound": []},
        "services": [{"reference": "health-check", "config": {"scheme": "http", "port": 8080}}],
    }
    ctx = _manifest_ctx(component_def=component_def)
    contribution = get_service(ServiceType.HEALTH_CHECK).contribute_manifest_context(ctx)
    assert contribution.template_vars == {}


def test_health_check_hooks_into_component_form():
    """health-check is a component-level service: no standalone wizard section, it HOOKS
    its probe fieldset into the per-component form via config_component_layout()."""
    provider = get_service(ServiceType.HEALTH_CHECK)

    assert provider.config_form_section(ConfigLayer.PROJECT) is None
    nodes = provider.config_component_layout()
    assert len(nodes) == 1
    assert nodes[0].legend == "Health check configuratie"
    assert nodes[0].children == [
        "services{health-check}/config/scheme",
        "services{health-check}/config/port",
        "services{health-check}/config/liveness-path",
        "services{health-check}/config/readiness-path",
    ]
    # component-level api fields come from the config model (alias-aware).
    assert provider.config_api_fields(ConfigLayer.COMPONENT) == provider.config_model_field_names()
    assert set(provider.config_api_fields(ConfigLayer.COMPONENT)) == {
        "scheme",
        "port",
        "liveness-path",
        "readiness-path",
    }
    assert [e.yaml_path for e in provider.config_editables(ConfigLayer.COMPONENT)] == [
        "components[*]/services{health-check}/config/scheme",
        "components[*]/services{health-check}/config/port",
        "components[*]/services{health-check}/config/liveness-path",
        "components[*]/services{health-check}/config/readiness-path",
    ]


# --- config field ownership (RC-5 prototype: auth-wall owns its fields) ----------


def test_auth_wall_owns_its_config_section():
    """The authorization-wall provider builds its own project-level config section
    (the wizard/edit layer sources it from here). Visibility is derived from the
    service_type, and the section object is cached so identity holds for consumers."""
    provider = get_service(ServiceType.AUTHORIZATION_WALL)

    section = provider.config_form_section(ConfigLayer.PROJECT)
    assert section is not None
    assert section.section_id == "auth-wall-config"
    # visibility derived from service_type, not a hardcoded string
    assert section.visible({"services": ["authorization-wall"]}) is True
    assert section.visible({"services": []}) is False
    # cached: same object on repeat (consumers compare identity)
    assert provider.config_form_section(ConfigLayer.PROJECT) is section

    # data editables + api fields for the project layer
    assert [e.yaml_path for e in provider.config_editables(ConfigLayer.PROJECT)] == [
        "services/authorization-wall/config/banner"
    ]
    # api fields are DERIVED from AuthorizationWallConfig (banner), not re-declared
    assert provider.config_api_fields(ConfigLayer.PROJECT) == ["banner"]
    assert provider.config_api_fields(ConfigLayer.PROJECT) == provider.config_model_field_names()


def test_namespace_postgres_owns_its_config_section():
    """namespace-postgres owns its project-level config section (instances + storage).
    Its API surface is the FULL config model (broader than the 2 UI fields) - the
    UI/API split the design calls for."""
    provider = get_service(ServiceType.NAMESPACE_POSTGRESQL_DATABASE)

    section = provider.config_form_section(ConfigLayer.PROJECT)
    assert section is not None
    assert section.section_id == "postgresql-config"
    assert section.visible({"services": ["namespace-postgresql-database"]}) is True
    assert section.visible({"services": []}) is False
    assert provider.config_form_section(ConfigLayer.PROJECT) is section

    # UI section exposes 2 fields; their paths are enum-built.
    assert [e.yaml_path for e in provider.config_editables(ConfigLayer.PROJECT)] == [
        "services/namespace-postgresql-database/config/instances",
        "services/namespace-postgresql-database/config/storage",
    ]
    # API surface = the whole model (image/registry/privileges/... are API/YAML-only).
    api_fields = provider.config_api_fields(ConfigLayer.PROJECT)
    assert api_fields == provider.config_model_field_names()
    assert set(api_fields) >= {"instances", "storage", "image", "privileges"}


def test_metrics_scraper_hooks_into_component_form():
    """metrics-scraper is a component-level service: no standalone wizard section, it
    HOOKS its fieldset into the per-component form via config_component_layout()."""
    provider = get_service(ServiceType.METRICS_SCRAPER)

    # no standalone project/edit section
    assert provider.config_form_section(ConfigLayer.PROJECT) is None
    # contributes exactly one component-form fieldset
    nodes = provider.config_component_layout()
    assert len(nodes) == 1
    assert nodes[0].legend == "Prometheus metrics scraper configuratie"
    assert nodes[0].children == ["services{metrics-scraper}/port", "services{metrics-scraper}/path"]
    # component-level api fields + editables
    assert set(provider.config_api_fields(ConfigLayer.COMPONENT)) == {"port", "path"}
    assert [e.yaml_path for e in provider.config_editables(ConfigLayer.COMPONENT)] == [
        "components[*]/services{metrics-scraper}/port",
        "components[*]/services{metrics-scraper}/path",
    ]
    # a project-level service (auth-wall) contributes NO component layout
    assert get_service(ServiceType.AUTHORIZATION_WALL).config_component_layout() == []


def test_storage_services_hook_sequences_into_component_form():
    """persistent/temp-storage are component-level: each hooks a mounts Sequence into
    the component form and owns its component config editables."""
    for service_type, field_name in (
        (ServiceType.PERSISTENT_STORAGE, "services{persistent-storage}/config"),
        (ServiceType.TEMP_STORAGE, "services{temp-storage}/config"),
    ):
        provider = get_service(service_type)
        assert provider.config_form_section(ConfigLayer.PROJECT) is None
        nodes = provider.config_component_layout()
        assert len(nodes) == 1
        assert nodes[0].field_name == field_name
        assert len(provider.config_editables(ConfigLayer.COMPONENT)) == 1


def test_publish_on_web_owns_its_component_config():
    """publish-on-web owns its component TLS/attachment fieldset (the rest of the
    domain feature is cross-project platform infra, not part of this service)."""
    provider = get_service(ServiceType.PUBLISH_ON_WEB)
    nodes = provider.config_component_layout()
    assert len(nodes) == 1
    assert nodes[0].legend == "Publicatie op het web"
    assert [e.yaml_path for e in provider.config_editables(ConfigLayer.COMPONENT)] == [
        "components[*]/services{publish-on-web}/config/tls",
        "components[*]/services{publish-on-web}/config/attachment",
    ]


def test_component_layout_collection_is_ordered_by_config_component_order():
    """The per-component form collects service fieldsets in config_component_order."""
    from opi.forms.visualizers.wizard_sections import _service_component_layouts

    labels = [getattr(n, "legend", None) or getattr(n, "field_name", None) for n in _service_component_layouts()]
    assert labels == [
        # The aliases / user-env-vars system services sort first (RC-25), where the
        # hand-authored "Variabelen" fieldset used to sit.
        "Aliassen",
        "Eigen omgevingsvariabelen",
        "services{persistent-storage}/config",
        "services{temp-storage}/config",
        "services{attachments}/config",
        "Publicatie op het web",
        "Prometheus metrics scraper configuratie",
        "Health check configuratie",
    ]


def test_config_path_builds_from_enums():
    """Service config yaml_paths are built from ConfigLayer + ServiceType enums, not
    hardcoded strings - one place encodes each layer's path shape."""
    assert (
        config_path(ConfigLayer.PROJECT, ServiceType.AUTHORIZATION_WALL, "config", "banner")
        == "services/authorization-wall/config/banner"
    )
    assert (
        config_path(ConfigLayer.COMPONENT, ServiceType.PERSISTENT_STORAGE, "config[*]", "name")
        == "components[*]/services{persistent-storage}/config[*]/name"
    )
    assert config_path(ConfigLayer.DEPLOYMENT, ServiceType.POSTGRESQL_DATABASE) == (
        "deployments[*]/services{postgresql-database}"
    )


def test_auth_wall_depends_on_keycloak():
    """auth-wall does not work without keycloak: its definition requires it, and
    selecting auth-wall alone pulls keycloak (+ publish-on-web) in."""
    provider = get_service(ServiceType.AUTHORIZATION_WALL)
    assert "services/keycloak" in provider.definition.requires

    resolved = ServiceAdapter.resolve_service_dependencies(["authorization-wall"])
    assert "keycloak" in resolved
    assert "publish-on-web" in resolved


def test_keycloak_owns_its_config_section():
    """keycloak (the complex one) owns its project-level config section; the nested
    additional-clients sequence stays hand-authored but is referenced by the service."""
    provider = get_service(ServiceType.KEYCLOAK)
    section = provider.config_form_section(ConfigLayer.PROJECT)
    assert section is not None
    assert section.section_id == "keycloak-config"
    assert section.visible({"services": ["keycloak"]}) is True
    assert section.visible({"services": []}) is False
    assert provider.config_form_section(ConfigLayer.PROJECT) is section
    # the section's Template fieldset path is enum-built
    assert section.layout[0].children == ["services/keycloak/config/template"]
    # api surface = full KeycloakConfig (broader than the UI section)
    api_fields = provider.config_api_fields(ConfigLayer.PROJECT)
    assert api_fields == provider.config_model_field_names()
    assert set(api_fields) >= {"template", "restrict-access", "additional-clients"}


def test_config_ownership_defaults_are_noop():
    """Services that don't own config fields (and non-project layers) return the empty
    defaults, so unmigrated services keep working."""
    provider = get_service(ServiceType.AUTHORIZATION_WALL)
    assert provider.config_form_section(ConfigLayer.COMPONENT) is None
    assert provider.config_api_fields(ConfigLayer.COMPONENT) == []
    # a service with no config-field ownership at this layer (redis owns the project
    # layer since RC-25, but nothing on the component layer)
    redis = get_service(ServiceType.REDIS)
    assert redis.config_form_section(ConfigLayer.COMPONENT) is None
    assert redis.config_editables(ConfigLayer.COMPONENT) == []


# --- approval ownership (RC-5) ---------------------------------------------------
# The generic approval capability: a service DECLARES what needs approval
# (config_approvals / ApprovalSpec) and supplies the rule to CHECK a stored verdict
# (status_of). Grounded in publish-on-web's domain / subdomain approval.

_APPROVAL_PROJECT = {
    "domains": {
        "allowed-domains": [
            {"domain": "ok.nl", "status": "approved"},
            {"domain": "wait.nl", "status": "requested"},
            {"domain": "no.nl", "status": "denied"},
        ],
        "allowed-subdomains": [
            {"domain": "ok.nl", "subdomains": [{"name": "app", "status": "approved"}]},
        ],
    }
}


def test_publish_on_web_declares_domain_approval():
    """DEFINITION: the service declares (as data) that domain + subdomain need
    platform-admin approval, only at the deployment layer."""
    from opi.services.catalog.approval import ApprovalSpec, ApproverScope

    provider = get_service(ServiceType.PUBLISH_ON_WEB)
    specs = provider.config_approvals(ConfigLayer.DEPLOYMENT)
    assert all(isinstance(s, ApprovalSpec) for s in specs)
    assert [(s.key, s.approver) for s in specs] == [
        ("domain", ApproverScope.PLATFORM_ADMIN),
        ("subdomain", ApproverScope.PLATFORM_ADMIN),
    ]
    # approvals are layer-scoped like the rest of the config surface
    assert provider.config_approvals(ConfigLayer.COMPONENT) == []
    # and looked up by key across layers
    assert provider.get_approval("domain").label == "Domein"
    assert provider.get_approval("does-not-exist") is None


def test_domain_approval_check_reads_stored_verdict():
    """CHECK: status_of maps the persisted verdict onto ApprovalStatus, for a value the
    service knows how to identify (a domain string). Unknown -> NONE (nothing to gate)."""
    from opi.services.catalog.approval import ApprovalStatus

    dom = get_service(ServiceType.PUBLISH_ON_WEB).get_approval("domain")
    assert dom.status(_APPROVAL_PROJECT, "ok.nl") is ApprovalStatus.APPROVED
    assert dom.status(_APPROVAL_PROJECT, "wait.nl") is ApprovalStatus.REQUESTED
    assert dom.status(_APPROVAL_PROJECT, "no.nl") is ApprovalStatus.DENIED
    assert dom.status(_APPROVAL_PROJECT, "unknown.nl") is ApprovalStatus.NONE
    # the common gating shortcut
    assert dom.is_approved(_APPROVAL_PROJECT, "ok.nl") is True
    assert dom.is_approved(_APPROVAL_PROJECT, "wait.nl") is False


def test_subdomain_approval_check_reads_stored_verdict():
    """CHECK: a compound value (domain, subdomain) resolves through the service's rule."""
    from opi.services.catalog.approval import ApprovalStatus

    sub = get_service(ServiceType.PUBLISH_ON_WEB).get_approval("subdomain")
    assert sub.is_approved(_APPROVAL_PROJECT, ("ok.nl", "app")) is True
    assert sub.status(_APPROVAL_PROJECT, ("ok.nl", "missing")) is ApprovalStatus.NONE


def test_services_without_approvals_default_empty():
    """Services with nothing to approve return the empty default at every layer."""
    redis = get_service(ServiceType.REDIS)
    assert all(redis.config_approvals(layer) == [] for layer in ConfigLayer)
    assert redis.get_approval("domain") is None


# --- generic, catalog-driven approver interface ----------------------------------
# The router lists pending items + records verdicts by iterating the catalog's specs,
# not by hard-coding the domains subsystem.


def test_approval_services_are_discovered_from_the_catalog():
    """approval_services() surfaces exactly the services that declare an ApprovalSpec."""
    from opi.services.registry import approval_services

    services = approval_services()
    assert get_service(ServiceType.PUBLISH_ON_WEB) in services
    # a service with no approvals is not listed
    assert get_service(ServiceType.REDIS) not in services


def test_collect_approval_items_tags_owner_across_catalog():
    """LIST: the generic collector concatenates each spec's items and tags them with
    the owning service + spec key, so a verdict can later be routed back."""
    from opi.services.approvals import collect_approval_items

    items = collect_approval_items(_APPROVAL_PROJECT)
    # 3 domains + 1 subdomain from the fixture
    assert len(items) == 4
    assert all(i["service"] == ServiceType.PUBLISH_ON_WEB.value for i in items)
    assert {i["type"] for i in items} == {"domain", "subdomain"}
    assert all(i["status"] == "skip" for i in items)


def test_apply_approval_verdicts_routes_to_owning_spec():
    """RECORD: the generic applier routes a decided item to its spec's record, writing
    the new status + a history entry; skips are left untouched."""
    import copy

    from opi.connectors.subdomain import get_domains_config
    from opi.services.approvals import apply_approval_verdicts

    project = copy.deepcopy(_APPROVAL_PROJECT)
    items = [
        {
            "service": "publish-on-web",
            "type": "domain",
            "domain": "wait.nl",
            "name": "wait.nl",
            "status": "approved",
            "message": "ok",
        },
        {"service": "publish-on-web", "type": "subdomain", "domain": "ok.nl", "name": "app", "status": "skip"},
    ]
    apply_approval_verdicts(project, items, admin_email="admin@test.nl")

    # the verdict write migrates the block from root to the publish-on-web service
    # config; read it back location-independently.
    domains = get_domains_config(project)
    approved = next(d for d in domains["allowed-domains"] if d["domain"] == "wait.nl")
    assert approved["status"] == "approved"
    assert approved["history"][-1] == {
        "date": approved["history"][-1]["date"],  # timestamp is dynamic
        "status": "approved",
        "by": "admin@test.nl",
        "message": "ok",
    }
    # the skipped subdomain is unchanged (still approved, no new history)
    sub = domains["allowed-subdomains"][0]["subdomains"][0]
    assert sub["status"] == "approved"
    assert "history" not in sub or len(sub["history"]) == 0


def test_apply_approval_verdicts_falls_back_to_type_when_untagged():
    """An in-flight item without a ``service`` tag still routes on its spec key."""
    import copy

    from opi.connectors.subdomain import get_domains_config
    from opi.services.approvals import apply_approval_verdicts

    project = copy.deepcopy(_APPROVAL_PROJECT)
    items = [{"type": "domain", "domain": "wait.nl", "name": "wait.nl", "status": "denied"}]
    apply_approval_verdicts(project, items)

    denied = next(d for d in get_domains_config(project)["allowed-domains"] if d["domain"] == "wait.nl")
    assert denied["status"] == "denied"
