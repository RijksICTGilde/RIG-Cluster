"""Visualizers for the send-email service, on the project and the deployment layer."""

from __future__ import annotations

from dataclasses import replace

from opi.core.config import settings
from opi.forms.editables.editable import Editable, WidgetType
from opi.forms.visualizers.visualizer import EditableVisualizer
from opi.services.catalog.send_email.config_model import MAX_MESSAGES_PER_DAY
from opi.services.catalog.send_email.editables import (
    SEND_EMAIL_DEPLOYMENT_FROM_ADDRESS_EDITABLE,
    SEND_EMAIL_DEPLOYMENT_FROM_NAME_EDITABLE,
    SEND_EMAIL_DEPLOYMENT_MESSAGES_PER_DAY_EDITABLE,
    SEND_EMAIL_FROM_ADDRESS_EDITABLE,
    SEND_EMAIL_FROM_NAME_EDITABLE,
    SEND_EMAIL_MESSAGES_PER_DAY_EDITABLE,
)
from opi.utils.naming import MAIL_SENDER_DOMAIN_ALLOWLIST

#: The default, and the boundary between choosing yourself and requesting; see the note on the
#: editable.
_DEFAULT_PER_DAY = settings.MAIL_PROJECT_DEFAULT_MESSAGES_PER_DAY

#: Which domains a private sender address may sit in; see ``MAIL_SENDER_DOMAIN_ALLOWLIST``.
_SENDER_DOMAINS = ", ".join(sorted(MAIL_SENDER_DOMAIN_ALLOWLIST))

SEND_EMAIL_FROM_NAME = EditableVisualizer(
    editable=SEND_EMAIL_FROM_NAME_EDITABLE,
    widget=WidgetType.TEXT,
    label="Naam van de afzender",
    description="De naam die de ontvanger boven het bericht ziet staan.",
    help_text=("Laat je dit leeg, dan ziet de ontvanger alleen het e-mailadres."),
)

SEND_EMAIL_FROM_ADDRESS = EditableVisualizer(
    editable=SEND_EMAIL_FROM_ADDRESS_EDITABLE,
    widget=WidgetType.TEXT,
    label="E-mailadres van de afzender",
    description="Laat dit leeg om te versturen vanaf het adres van je project.",
    help_text=(
        f"Moet goedgekeurd worden; alleen {_SENDER_DOMAINS}. "
        "Wijzigen = nieuwe aanvraag. Antwoorden zet je via Reply-To in je applicatie."
    ),
)

SEND_EMAIL_MESSAGES_PER_DAY = EditableVisualizer(
    editable=SEND_EMAIL_MESSAGES_PER_DAY_EDITABLE,
    widget=WidgetType.NUMBER,
    label="Maximaal aantal berichten per dag",
    description=(
        f"Standaard {_DEFAULT_PER_DAY} per dag; leeg laten betekent die standaard. "
        f"Een lager getal kies je zelf, boven {_DEFAULT_PER_DAY} is het een aanvraag."
    ),
    # Bounds on the field itself, otherwise the spinner arrows cheerfully step to -1 and by 1
    # at a time. Steps of 50: a daily budget is an order of magnitude, not an exact number. The
    # server guards the same range (see the editable).
    attributes={"min": "50", "max": str(MAX_MESSAGES_PER_DAY), "step": "50"},
    help_text=(
        "De relay houdt per project bij hoeveel berichten er die dag verstuurd zijn, en telt "
        "per bericht en niet per ontvanger. Zit je aan je maximum, dan krijgt je applicatie een "
        "tijdelijke weigering en probeert ze het later opnieuw. Dit is er vooral om te voorkomen "
        "dat een fout in je code duizenden berichten verstuurt voordat iemand het merkt. Vul je "
        f"meer dan {_DEFAULT_PER_DAY} in, dan is dat een aanvraag die een beheerder goedkeurt; tot die "
        "tijd blijft het getal gelden dat je eerder had."
    ),
)


def _deployment_field(
    editable: Editable,
    *,
    label: str,
    description: str,
    help_text: str,
    widget: WidgetType = WidgetType.TEXT,
    attributes: dict[str, str] | None = None,
) -> EditableVisualizer:
    """A deployment's field: the same editable rules, an explanation of its own.

    The explanation does differ from the project's, and that is the half easily skipped: empty
    means INHERIT here and not "default", and that difference is exactly what a reader of this
    form has to know.
    """
    return EditableVisualizer(
        editable=editable,
        widget=widget,
        label=label,
        description=description,
        help_text=help_text,
        attributes=attributes,
    )


SEND_EMAIL_DEPLOYMENT_FROM_NAME = _deployment_field(
    SEND_EMAIL_DEPLOYMENT_FROM_NAME_EDITABLE,
    label="Naam van de afzender",
    description="De naam die de ontvanger boven het bericht van DEZE deployment ziet staan.",
    help_text="Laat je dit leeg, dan geldt de naam van het project.",
)

SEND_EMAIL_DEPLOYMENT_FROM_ADDRESS = _deployment_field(
    SEND_EMAIL_DEPLOYMENT_FROM_ADDRESS_EDITABLE,
    label="E-mailadres van de afzender",
    description="Het adres waaronder DEZE deployment verstuurt.",
    help_text=(
        f"Laat je dit leeg, dan geldt het adres van het project. Moet goedgekeurd worden; alleen "
        f"{_SENDER_DOMAINS}. Wijzigen = nieuwe aanvraag, en tot de goedkeuring blijft gelden wat er al gold."
    ),
)

SEND_EMAIL_DEPLOYMENT_MESSAGES_PER_DAY = _deployment_field(
    SEND_EMAIL_DEPLOYMENT_MESSAGES_PER_DAY_EDITABLE,
    label="Maximaal aantal berichten per dag",
    description="Het dagbudget van DEZE deployment; zij heeft haar eigen account en dus haar eigen teller.",
    widget=WidgetType.NUMBER,
    attributes={"min": "50", "max": str(MAX_MESSAGES_PER_DAY), "step": "50"},
    help_text=(
        f"Laat je dit leeg, dan geldt het budget van het project. Tot en met {_DEFAULT_PER_DAY} per dag kies je "
        "zelf, ook als het project meer mag; daarboven is het een aanvraag die een beheerder goedkeurt. Tot "
        "die tijd blijft gelden wat er al gold."
    ),
)

#: In form order, the same as the editables.
SEND_EMAIL_DEPLOYMENT_VISUALIZERS = (
    SEND_EMAIL_DEPLOYMENT_FROM_NAME,
    SEND_EMAIL_DEPLOYMENT_FROM_ADDRESS,
    SEND_EMAIL_DEPLOYMENT_MESSAGES_PER_DAY,
)

#: What an empty placeholder means per field, when the project has no value for it. For the
#: address, empty is not a missing value: the relay then derives it from the account name.
_EMPTY_INHERITS = {
    "from-name": "geen naam",
    "from-address": "wordt afgeleid uit je projectnaam",
    "messages-per-day": "",
}


def deployment_visualizers(inherited: dict[str, str] | None = None) -> tuple[EditableVisualizer, ...]:
    """The three fields of a deployment, with the inherited value as placeholder.

    A copy per call and not a shared instance: the placeholder hangs on the PROJECT the form is
    built for, and a shared object would pass that value on to the next project.
    """
    if inherited is None:
        return SEND_EMAIL_DEPLOYMENT_VISUALIZERS
    fields = []
    for field in SEND_EMAIL_DEPLOYMENT_VISUALIZERS:
        key = field.editable.yaml_path.rsplit("/", 1)[1]
        value = inherited.get(key) or _EMPTY_INHERITS.get(key, "")
        fields.append(replace(field, placeholder=f"van het project: {value}" if value else None))
    return tuple(fields)
