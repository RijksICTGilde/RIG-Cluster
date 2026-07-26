"""platform service (hidden, always-on platform variables)."""

from __future__ import annotations

from opi.services.catalog.base import Service
from opi.services.services_enums import ServiceType


class PlatformService(Service):
    service_type = ServiceType.PLATFORM
