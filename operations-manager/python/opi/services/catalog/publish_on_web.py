"""publish-on-web service."""

from __future__ import annotations

from opi.services.catalog.base import Service
from opi.services.services_enums import ServiceType


class PublishOnWebService(Service):
    service_type = ServiceType.PUBLISH_ON_WEB
