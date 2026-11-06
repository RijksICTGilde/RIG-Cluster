"""The environment variables the vlam service hands a component.

Two, and they are not interchangeable. ``VLAM_API_URL`` is the TERMINATED path: plain
HTTP to the in-cluster proxy, which sets up the verified TLS session towards VLAM itself.
``VLAM_API_URL_DIRECT`` is the DOORLUS (RC-167), where the consumer speaks TLS to VLAM
directly and verifies the certificate itself -- against the standard trust store of its
own runtime, because the certificate of VLAM is publicly trusted (RC-414).

Lives in the service's own package (RC-36).
"""

from enum import Enum

from opi.services.services import VariableDefinition


class VlamVariables(Enum):
    """vlam service variable definitions - single source of truth."""

    API_URL = VariableDefinition(
        name="VLAM_API_URL",
        description="Basisadres van de VLAM-API binnen het cluster, zonder pad",
        source="direct",
    )

    API_URL_DIRECT = VariableDefinition(
        name="VLAM_API_URL_DIRECT",
        description=(
            "Basisadres van het doorlus-pad: TLS tot aan VLAM zelf, met de VLAM-hostnaam"
            " in de URL die je pod tegen het certificaat verifieert"
        ),
        source="direct",
        conditional=True,
    )
