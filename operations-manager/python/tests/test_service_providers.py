"""Provider-coverage guard (RC-5 Phase 1).

This is the guardrail that keeps ``SERVICE_PROVIDERS`` the single source of truth
for services: adding a ``ServiceType`` without a provider fails CI here, so the
registry can never silently fall behind the enum. It also asserts each provider
carries the same ``ServiceDefinition`` object the rest of the app already reads,
so the provider abstraction composes with today's design instead of forking it.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from opi.services.provider import ManifestContext, ProvisionContext, RemovalContext, ServiceProvider
from opi.services.registry import (
    SERVICE_PROVIDERS,
    get_provider,
    manifest_secret_providers,
    provisioning_providers,
)
from opi.services.services import ServiceAdapter
from opi.services.services_enums import ServiceType


def test_every_service_type_has_a_provider() -> None:
    """Every ServiceType must be registered -- this is the coverage guard."""
    missing = [t for t in ServiceType if t not in SERVICE_PROVIDERS]
    assert not missing, (
        f"ServiceType(s) without a provider: {[t.value for t in missing]}. "
        f"Add a ServiceProvider subclass and register it in opi/services/registry.py."
    )


def test_no_extra_providers() -> None:
    """The registry must not carry providers for non-existent service types."""
    extra = [t for t in SERVICE_PROVIDERS if t not in set(ServiceType)]
    assert not extra, f"Registry has providers for unknown types: {extra}"


def test_provider_definition_is_the_shared_service_definition() -> None:
    """Each provider's definition is the exact object in SERVICE_DEFINITIONS.

    The provider composes with the existing metadata registry; it does not clone
    it. Identity (``is``) guarantees no drift.
    """
    for t in ServiceType:
        provider = get_provider(t)
        assert provider.definition is ServiceAdapter.SERVICE_DEFINITIONS[t], (
            f"Provider for {t.value} does not carry the shared ServiceDefinition"
        )


def test_provider_service_type_matches_registry_key() -> None:
    for t, provider in SERVICE_PROVIDERS.items():
        assert provider.service_type == t, (
            f"Provider registered under {t.value} reports service_type {provider.service_type.value}"
        )


def test_providers_are_service_provider_instances() -> None:
    for provider in SERVICE_PROVIDERS.values():
        assert isinstance(provider, ServiceProvider)


def test_get_provider_returns_registered_instance() -> None:
    for t in ServiceType:
        assert get_provider(t) is SERVICE_PROVIDERS[t]


# ---------------------------------------------------------------------------
# Phase 4: generic provisioning dispatch
# ---------------------------------------------------------------------------


def test_provisioning_providers_order_matches_legacy_sequence():
    # Must reproduce today's fixed db -> minio -> keycloak -> redis order.
    assert [p.service_type.value for p in provisioning_providers()] == [
        "postgresql-database",
        "minio-storage",
        "keycloak",
        "redis",
    ]


def test_namespace_variants_do_not_double_provision():
    # namespace-postgres and namespace-redis share a manager with their primary and
    # must NOT override provision (else the manager would be called twice).
    provisioning = {p.service_type for p in provisioning_providers()}
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
        for provider in provisioning_providers():
            await provider.provision(ctx)

    asyncio.run(run())
    # same calls + args as the old fixed sequence
    db.assert_awaited_once_with({"name": "p"}, {"name": "d"}, True)
    minio.assert_awaited_once_with({"name": "p"}, {"name": "d"}, True)
    keycloak.assert_awaited_once_with({"name": "p"}, {"name": "d"})
    redis.assert_awaited_once_with({"name": "p"}, {"name": "d"})


def test_default_provision_is_noop():
    async def run():
        await get_provider(ServiceType.PUBLISH_ON_WEB).provision(None)  # type: ignore[arg-type]

    asyncio.run(run())  # must not raise


# ---------------------------------------------------------------------------
# Phase 5: generic cleanup dispatch
# ---------------------------------------------------------------------------

# Frozen copy of the old DeleteProjectManager._SERVICE_TYPE_MANAGER_ATTR map. The
# provider registry now owns this mapping via cleanup_manager_key; this guard fails
# if a provider's key ever drifts from the dispatch the map used to perform.
_LEGACY_SERVICE_MANAGER_KEYS = {
    ServiceType.POSTGRESQL_DATABASE: "database",
    ServiceType.NAMESPACE_POSTGRESQL_DATABASE: "database",
    ServiceType.MINIO_STORAGE: "minio",
    ServiceType.REDIS: "redis",
    ServiceType.NAMESPACE_REDIS: "redis",
    ServiceType.KEYCLOAK: "keycloak",
    ServiceType.PERSISTENT_STORAGE: "pvc",
}


def test_cleanup_manager_key_matches_legacy_map():
    actual = {t: p.cleanup_manager_key for t, p in SERVICE_PROVIDERS.items() if p.cleanup_manager_key is not None}
    assert actual == _LEGACY_SERVICE_MANAGER_KEYS


def test_services_without_cleanup_have_no_manager_key():
    # Anything not in the legacy map must have cleanup_manager_key None (no server-side
    # resources), so the generic dispatch skips it exactly as the old `.get() is None`
    # guard did.
    for t in ServiceType:
        if t not in _LEGACY_SERVICE_MANAGER_KEYS:
            assert get_provider(t).cleanup_manager_key is None, t.value


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
        return await get_provider(ServiceType.MINIO_STORAGE).handle_service_removal(ctx)

    result = asyncio.run(run())
    assert result == {"errors": []}
    assert resolved_keys == ["minio"]
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
        return await get_provider(ServiceType.PUBLISH_ON_WEB).handle_service_removal(ctx)

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


def test_manifest_secret_providers_order_and_names():
    ctx = ManifestContext(deployment_name="mydep")
    actual = [
        (p.service_type.value, p.contribute_manifest_context(ctx).env_from_secrets[0])
        for p in manifest_secret_providers()
    ]
    assert actual == _EXPECTED_ENVFROM_ORDER


def test_shared_providers_activate_for_namespace_variant():
    # postgres and redis contribute their single envFrom secret for BOTH the shared
    # and the namespace variant, so exactly one secret is added per manager.
    pg = get_provider(ServiceType.POSTGRESQL_DATABASE)
    assert set(pg.manifest_activation_types()) == {
        ServiceType.POSTGRESQL_DATABASE,
        ServiceType.NAMESPACE_POSTGRESQL_DATABASE,
    }
    redis = get_provider(ServiceType.REDIS)
    assert set(redis.manifest_activation_types()) == {ServiceType.REDIS, ServiceType.NAMESPACE_REDIS}


def test_default_manifest_activation_is_own_service_type():
    kc = get_provider(ServiceType.KEYCLOAK)
    assert kc.manifest_activation_types() == (ServiceType.KEYCLOAK,)


def test_non_secret_services_contribute_nothing():
    ctx = ManifestContext(deployment_name="mydep")
    for t in ServiceType:
        provider = get_provider(t)
        if provider.manifest_secret_class is None:
            assert provider.contribute_manifest_context(ctx).env_from_secrets == [], t.value


def test_namespace_variants_are_not_separate_contributors():
    # The shared provider owns the contribution; the namespace variants must not carry
    # their own manifest_secret_class (else the secret would be added twice).
    assert get_provider(ServiceType.NAMESPACE_POSTGRESQL_DATABASE).manifest_secret_class is None
    assert get_provider(ServiceType.NAMESPACE_REDIS).manifest_secret_class is None
    contributors = {p.service_type for p in manifest_secret_providers()}
    assert ServiceType.NAMESPACE_POSTGRESQL_DATABASE not in contributors
    assert ServiceType.NAMESPACE_REDIS not in contributors
