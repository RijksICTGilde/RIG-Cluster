"""platform service (hidden, always-on platform variables)."""

from __future__ import annotations

from opi.services.catalog.base import Service
from opi.services.catalog.platform.variables import PlatformVariables
from opi.services.services import ServiceDefinition
from opi.services.services_enums import ServiceBinding, ServiceKind, ServiceType


class PlatformService(Service):
    service_type = ServiceType.PLATFORM
    definition = ServiceDefinition(
        name="Platform",
        description="Automatisch beschikbare platform variabelen",
        help_template="platform/help.html.j2",
        icon="info",
        color="grijs-600",
        binding=ServiceBinding.COMPONENT,
        secret_class="PlatformSecret",
        variables=[var.value for var in PlatformVariables],
        # Always on, never chosen by a project -> a system service. kind=SYSTEM
        # also keeps it out of the picker, so an explicit hidden is not needed.
        kind=ServiceKind.SYSTEM,
    )
