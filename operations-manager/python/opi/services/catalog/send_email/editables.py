"""Editable definitions for the send-email service (project-level).

Two fields, and only two: what the recipient sees as the sender NAME, and the daily
budget. The address is not here and is not a field anywhere -- it is composed by the
platform from the project name (``noreply-rijksapp+<project>@rijksoverheid.nl``) and the
relay overwrites the ``From:`` header with it, so there is nothing to configure and
nothing to get wrong.

That is not tidiness, it is the only arrangement that delivers. Mail leaves over the
Rijksoverheid mail server and therefore carries their DOMAIN, which publishes
``p=reject``; we sign nothing with DKIM, so SPF alignment between envelope and ``From:``
is the single thing that can pass DMARC. A self-chosen domain would break exactly that.
The project in the plus part does not: same domain, and a bounce stays traceable.
See docs/ron-koppeling.md.
"""

from __future__ import annotations

from opi.core.config import settings
from opi.forms.editables.converters import IntegerConverter
from opi.forms.editables.editable import SERVICE_VIRTUALIZE, Editable
from opi.forms.editables.validators import ModelFieldValidator, RangeValidator
from opi.services.catalog.base import ConfigLayer, config_path
from opi.services.catalog.send_email.config_model import MAX_FROM_NAME_LENGTH, SendEmailConfig
from opi.services.services_enums import ServiceType

SEND_EMAIL_FROM_NAME_EDITABLE = Editable(
    yaml_path=config_path(ConfigLayer.PROJECT, ServiceType.SEND_EMAIL, "config", "from-name"),
    # De regel staat in het model en niet hier: dit veld gaat rechtstreeks een mailheader
    # in, en dan mag het formulier niet iets anders toelaten dan de API. ModelFieldValidator
    # wijst naar dezelfde constraints waarmee een opgeslagen projectbestand wordt getoetst,
    # dus er is een definitie en geen tweeling die uit elkaar loopt. De uitleg is wel van
    # hier: de pydantic-melding is Engels en praat over patronen.
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

SEND_EMAIL_MESSAGES_PER_DAY_EDITABLE = Editable(
    yaml_path=config_path(ConfigLayer.PROJECT, ServiceType.SEND_EMAIL, "config", "messages-per-day"),
    converter=IntegerConverter(),
    # De klant mag zichzelf alleen VERLAGEN. De grens van dit veld is de platformstandaard
    # (500), niet het schemamaximum (5000): het budget is volume dat met het mailteam is
    # afgesproken, en een project dat zichzelf van 500 naar 5000 opschroeft zou de
    # goedkeuring omzeilen die "dit project mag mailen" nu juist afdekt. Meer dan de
    # standaard is een afspraak met de beheerder, en die schrijft het schemabereik
    # (tot 5000) buiten dit formulier om.
    validator=RangeValidator(min_value=1, max_value=settings.MAIL_PROJECT_DEFAULT_MESSAGES_PER_DAY),
    remove_when_none=True,
    virtualize=SERVICE_VIRTUALIZE,
)
