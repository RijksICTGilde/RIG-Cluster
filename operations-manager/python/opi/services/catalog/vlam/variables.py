"""The environment variable the vlam service hands a component.

One variable, and it is a plain address rather than a credential: the proxy is reached
over HTTP inside the cluster and there is nothing to authenticate with. Lives in the
service's own package (RC-36).
"""

from enum import Enum

from opi.services.services import VariableDefinition


class VlamVariables(Enum):
    """vlam service variable definitions - single source of truth."""

    API_URL = VariableDefinition(
        name="VLAM_API_URL",
        description="Basisadres van de VLAM-API binnen het cluster, zonder pad",
        source="direct",
        aliases=["APP_VLAM_API_URL"],
    )
