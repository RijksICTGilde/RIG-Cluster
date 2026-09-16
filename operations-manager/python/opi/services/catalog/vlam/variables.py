"""The environment variables the vlam service hands a component.

Three, and they are not interchangeable. ``VLAM_API_URL`` is the TERMINATED path: plain
HTTP to the in-cluster proxy, which sets up the verified TLS session towards VLAM itself.
The other two belong to the DOORLUS (RC-167), where the consumer speaks TLS to VLAM
directly and therefore has to verify the certificate itself -- so it needs both the
address and the issuer to verify against.

Two things are deliberately NOT here, and leaving them out is the point of the doorlus
design. ``REQUESTS_CA_BUNDLE``, ``SSL_CERT_FILE`` and ``NODE_EXTRA_CA_CERTS`` REPLACE a
runtime's trust chain rather than adding to it: a pod pointed at only this bundle would
stop trusting everything else -- public APIs, Keycloak, package mirrors -- and that
failure looks nothing like its cause. The service cannot build a reliable merged bundle
either, because it does not know the consumer's base image. So it offers a path and the
application decides; ``help.md`` says per language how to add a CA NEXT TO the defaults.

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
            " in de URL en de CA-bundel uit VLAM_CA_BUNDLE_PATH om te verifieren"
        ),
        source="direct",
        conditional=True,
    )

    CA_BUNDLE_PATH = VariableDefinition(
        name="VLAM_CA_BUNDLE_PATH",
        description=(
            "Pad in de pod naar de CA-bundel van de UITGEVER van het VLAM-certificaat."
            " Zet hem NAAST de standaardbundel van je runtime, niet in de plaats ervan"
        ),
        source="direct",
        # Alleen gezet op een cluster dat het doorlus-pad aanbiedt: zonder CA-bundel is
        # er niets om naar te wijzen, en een pad naar een bestand dat niet bestaat is
        # erger dan geen pad.
        conditional=True,
    )
