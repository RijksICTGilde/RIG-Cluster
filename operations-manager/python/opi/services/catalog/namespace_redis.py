"""namespace-redis service (dedicated variant; currently falls back to shared Redis)."""

from __future__ import annotations

from opi.services.catalog.base import Service
from opi.services.services_enums import ServiceType


class NamespaceRedisService(Service):
    service_type = ServiceType.NAMESPACE_REDIS
    cleanup_manager_key = "redis"
