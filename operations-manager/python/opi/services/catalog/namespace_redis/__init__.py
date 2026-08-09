"""namespace-redis service (dedicated variant; currently falls back to shared Redis)."""

from __future__ import annotations

from opi.services.catalog.base import Service
from opi.services.catalog.redis.variables import RedisVariables
from opi.services.services import ServiceDefinition
from opi.services.services_enums import CleanupStrategy, ManagerKey, ServiceBinding, ServiceType


class NamespaceRedisService(Service):
    service_type = ServiceType.NAMESPACE_REDIS
    definition = ServiceDefinition(
        name="Namespace Redis Cache",
        description="Dedicated Redis instance per namespace voor caching en Celery task queues",
        help_template="namespace_redis/help.md",
        icon="zandloper",
        color="rood",
        binding=ServiceBinding.DEPLOYMENT,
        secret_class="RedisSecret",
        variables=[var.value for var in RedisVariables],
        hidden=True,
        cleanup_strategy=CleanupStrategy.IMMEDIATE,
    )
    cleanup_manager_key = ManagerKey.REDIS
