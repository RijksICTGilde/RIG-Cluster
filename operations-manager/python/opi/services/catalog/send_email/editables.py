"""Editable definitions for the send-email service (project-level).

Three fields, and only three: what the recipient sees as the sender name, the local part
of the address, and the daily budget. The domain is not editable here -- a domain of your
own needs a DKIM record in that zone before a single message arrives, so it is a request,
not a form field (see ``help.md`` and the plan's afzenderdomein paragraph).

``from-domain`` therefore exists in the config model but has no editable AND is marked
platform-managed: no editable keeps it out of the form, but the generated PUT is the other
door, and one that stays open after the single approval verdict has been given.
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

SEND_EMAIL_FROM_LOCAL_PART_EDITABLE = Editable(
    yaml_path=config_path(ConfigLayer.PROJECT, ServiceType.SEND_EMAIL, "config", "from-local-part"),
    # The rule ITSELF comes from the config model, not a copy of it: the form is the
    # early guard, the model is the guard. A bad value is refused at save time instead of
    # arriving as a relay rejection on a message nobody is watching.
    validator=ModelFieldValidator(
        SendEmailConfig,
        "from_local_part",
        "Gebruik alleen kleine letters, cijfers, punt, streepje en liggend streepje; "
        "begin en eindig met een letter of cijfer.",
    ),
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
