"""Provider-coverage guard (RC-5 Phase 1).

This is the guardrail that keeps ``SERVICE_PROVIDERS`` the single source of truth
for services: adding a ``ServiceType`` without a provider fails CI here, so the
registry can never silently fall behind the enum. It also asserts each provider
carries the same ``ServiceDefinition`` object the rest of the app already reads,
so the provider abstraction composes with today's design instead of forking it.
"""

from opi.services.provider import ServiceProvider
from opi.services.registry import SERVICE_PROVIDERS, get_provider
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
