"""Editable definitions for the send-email service.

Three fields: sender name, sender address (after approval), and daily budget (above the
platform default also after approval). Leave the address empty and the platform derives one
from the account name. Mail leaves over the
Rijksoverheid server carrying their domain; SPF alignment via DMARC is the only control
that passes, so a self-chosen domain breaks it. See docs/ron-koppeling.md.

The same three fields exist per deployment (RC-401), with the SAME validation: the form and
the API judge an override exactly as they judge the project value, so a value that cannot
go into a mail header is refused wherever it is written. An empty field means "inherit",
which is why every one of them removes its key instead of writing a null.
"""

from __future__ import annotations

from opi.forms.editables.converters import IntegerConverter
from opi.forms.editables.editable import SERVICE_VIRTUALIZE, Editable
from opi.forms.editables.validators import ModelFieldValidator, RangeValidator
from opi.services.catalog.base import ConfigLayer, config_path
from opi.services.catalog.send_email.config_model import (
    FROM_ADDRESS_MESSAGE,
    MAX_FROM_NAME_LENGTH,
    MAX_MESSAGES_PER_DAY,
    SendEmailConfig,
)
from opi.services.services_enums import ServiceType

SEND_EMAIL_FROM_NAME_EDITABLE = Editable(
    yaml_path=config_path(ConfigLayer.PROJECT, ServiceType.SEND_EMAIL, "config", "from-name"),
    # Validator in model, not here: form and API share constraints so they don't drift.
    validator=ModelFieldValidator(
        SendEmailConfig,
        "from_name",
        "De afzendernaam mag geen regeleindes, @, punthaken, aanhalingstekens, backslash of "
        f"dollarteken bevatten en is hoogstens {MAX_FROM_NAME_LENGTH} tekens lang",
    ),
    # Empty means "no display name", which is a valid outcome, so the key is dropped
    # rather than written as null.
    remove_when_none=True,
    virtualize=SERVICE_VIRTUALIZE,
)

SEND_EMAIL_FROM_ADDRESS_EDITABLE = Editable(
    yaml_path=config_path(ConfigLayer.PROJECT, ServiceType.SEND_EMAIL, "config", "from-address"),
    # Dezelfde regel als de connector toetst, via het model: wat het formulier aanneemt mag
    # de verwerking niet halverwege laten struikelen.
    validator=ModelFieldValidator(SendEmailConfig, "from_address", FROM_ADDRESS_MESSAGE),
    # Leeg betekent "het afgeleide adres", en dat is een geldige uitkomst. Het veld
    # leegmaken trekt daarmee ook de aanvraag in (zie ``config_value_approval``).
    remove_when_none=True,
    virtualize=SERVICE_VIRTUALIZE,
)

SEND_EMAIL_MESSAGES_PER_DAY_EDITABLE = Editable(
    yaml_path=config_path(ConfigLayer.PROJECT, ServiceType.SEND_EMAIL, "config", "messages-per-day"),
    converter=IntegerConverter(),
    # Het schemaplafond en niet de platformstandaard: een hoger getal is sinds RC-400 een
    # AANVRAAG en geen fout, dus het formulier moet hem aannemen. Wat er daadwerkelijk naar
    # de relay gaat, beslist ``effective_messages_per_day``.
    validator=RangeValidator(min_value=1, max_value=MAX_MESSAGES_PER_DAY),
    remove_when_none=True,
    virtualize=SERVICE_VIRTUALIZE,
)


def _deployment_editable(field: str, project_editable: Editable) -> Editable:
    """The same field, one layer down: same validator, same converter, same emptiness rule.

    Derived from the project editable rather than written out a second time -- what a
    display name, an address and a budget may be is one rule, and a second copy of it is a
    second rule that agrees today.
    """
    return Editable(
        yaml_path=config_path(ConfigLayer.DEPLOYMENT, ServiceType.SEND_EMAIL, "config", field),
        validator=project_editable.validator,
        converter=project_editable.converter,
        # Leeg betekent "neem de waarde van het project", en dat is de normale situatie.
        remove_when_none=True,
        virtualize=SERVICE_VIRTUALIZE,
    )


SEND_EMAIL_DEPLOYMENT_FROM_NAME_EDITABLE = _deployment_editable("from-name", SEND_EMAIL_FROM_NAME_EDITABLE)
SEND_EMAIL_DEPLOYMENT_FROM_ADDRESS_EDITABLE = _deployment_editable("from-address", SEND_EMAIL_FROM_ADDRESS_EDITABLE)
SEND_EMAIL_DEPLOYMENT_MESSAGES_PER_DAY_EDITABLE = _deployment_editable(
    "messages-per-day", SEND_EMAIL_MESSAGES_PER_DAY_EDITABLE
)

#: In formulierorde: wie schrijft, kiest eerst een naam, dan een adres, dan een budget.
SEND_EMAIL_DEPLOYMENT_EDITABLES = (
    SEND_EMAIL_DEPLOYMENT_FROM_NAME_EDITABLE,
    SEND_EMAIL_DEPLOYMENT_FROM_ADDRESS_EDITABLE,
    SEND_EMAIL_DEPLOYMENT_MESSAGES_PER_DAY_EDITABLE,
)
