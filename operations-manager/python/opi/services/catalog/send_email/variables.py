"""Environment variables the send-email service provides -- single source of truth.

Lives in the service's own package (RC-36). The names are the ones an application
library already expects (``SMTP_HOST``, ``SMTP_PORT``, ...), so a project that already
speaks SMTP needs no alias to use the platform relay.
"""

from enum import Enum

from opi.services.services import VariableDefinition


class SendEmailVariables(Enum):
    """send-email service variable definitions - single source of truth."""

    HOST = VariableDefinition(
        name="SMTP_HOST",
        description="Hostname van de mailrelay binnen het cluster",
        source="secret",
        secret_key="host",
    )
    PORT = VariableDefinition(
        name="SMTP_PORT",
        description="Submission-poort van de mailrelay (587)",
        source="secret",
        secret_key="port",
    )
    USERNAME = VariableDefinition(
        name="SMTP_USERNAME",
        description="SMTP-account van dit project op de relay",
        source="secret",
        secret_key="username",
    )
    PASSWORD = VariableDefinition(
        name="SMTP_PASSWORD",
        description="Wachtwoord van het SMTP-account van dit project",
        source="secret",
        secret_key="password",
    )
    FROM = VariableDefinition(
        name="SMTP_FROM",
        description="Afzenderadres dat de relay voor dit project afdwingt",
        source="secret",
        secret_key="from_address",
    )
