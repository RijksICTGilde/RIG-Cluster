"""Environment variables the platform service provides -- single source of truth.

Lives in the service's own package (RC-36): what a service hands to a deployment
is part of that service, not of a shared module.
"""

from enum import Enum

from opi.services.services import VariableDefinition


class PlatformVariables(Enum):
    """Platform-provided variable definitions - always available in every deployment."""

    DEPLOYMENT_NAME = VariableDefinition(
        name="DEPLOYMENT_NAME",
        description="Naam van het huidige deployment",
        source="secret",
        secret_key="deployment_name",
    )
    COMPONENT_NAME = VariableDefinition(
        name="COMPONENT_NAME",
        description="Naam van het huidige component",
        source="secret",
        secret_key="component_name",
    )
