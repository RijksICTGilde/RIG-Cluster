"""Editable definitions for the send-email service (project-level).

Two fields, and only two: what the recipient sees as the sender NAME, and the daily
budget. The address itself is not here and is not a field anywhere -- every project sends
from one fixed address and the relay overwrites the ``From:`` header with it, so there is
nothing to configure and nothing to get wrong.

That is not tidiness, it is the only arrangement that delivers. Mail leaves over the
Rijksoverheid mail server and therefore carries their domain, which publishes
``p=reject``; we sign nothing with DKIM, so SPF alignment between envelope and ``From:``
is the single thing that can pass DMARC. An address per project would break exactly that.
See docs/ron-koppeling.md.
"""

from __future__ import annotations

from opi.forms.editables.converters import IntegerConverter
from opi.forms.editables.editable import SERVICE_VIRTUALIZE, Editable
from opi.forms.editables.validators import ModelFieldValidator
from opi.services.catalog.base import ConfigLayer, config_path
from opi.services.catalog.send_email.config_model import MAX_MESSAGES_PER_DAY, SendEmailConfig
from opi.services.services_enums import ServiceType

SEND_EMAIL_FROM_NAME_EDITABLE = Editable(
    yaml_path=config_path(ConfigLayer.PROJECT, ServiceType.SEND_EMAIL, "config", "from-name"),
    # Empty means "no display name", which is a valid outcome, so the key is dropped
    # rather than written as null.
    remove_when_none=True,
    virtualize=SERVICE_VIRTUALIZE,
)

SEND_EMAIL_MESSAGES_PER_DAY_EDITABLE = Editable(
    yaml_path=config_path(ConfigLayer.PROJECT, ServiceType.SEND_EMAIL, "config", "messages-per-day"),
    converter=IntegerConverter(),
    validator=ModelFieldValidator(
        SendEmailConfig,
        "messages_per_day",
        f"Kies een aantal tussen 1 en {MAX_MESSAGES_PER_DAY}.",
    ),
    remove_when_none=True,
    virtualize=SERVICE_VIRTUALIZE,
)
