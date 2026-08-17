"""Environment variables the publish-on-web service provides -- single source of truth.

Lives in the service's own package (RC-36): what a service hands to a deployment
is part of that service, not of a shared module.
"""

from enum import Enum

from opi.services.services import VariableDefinition


class WebVariables(Enum):
    """Web publishing service variable definitions - single source of truth."""

    PUBLIC_HOST = VariableDefinition(
        name="PUBLIC_HOST",
        description="De publieke hostname/URL waar een component bereikbaar zal zijn",
        source="direct",
    )
    PUBLIC_HOSTNAME = VariableDefinition(
        name="PUBLIC_HOSTNAME",
        description="De publieke hostname (zonder scheme) waar een component bereikbaar zal zijn",
        source="direct",
    )
