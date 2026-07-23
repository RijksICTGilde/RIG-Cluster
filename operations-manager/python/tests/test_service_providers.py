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

from opi.services.provider import ProvisionContext, ServiceProvider
from opi.services.registry import SERVICE_PROVIDERS, get_provider, provisioning_providers
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
