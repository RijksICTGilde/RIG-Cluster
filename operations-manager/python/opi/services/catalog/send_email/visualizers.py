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

#: De standaard, en de grens tussen zelf kiezen en aanvragen; zie de toelichting bij de
#: editable.
_STANDAARD = settings.MAIL_PROJECT_DEFAULT_MESSAGES_PER_DAY

#: Waar een eigen afzenderadres in mag liggen; zie ``MAIL_SENDER_DOMAIN_ALLOWLIST``.
_AFZENDERDOMEINEN = ", ".join(sorted(MAIL_SENDER_DOMAIN_ALLOWLIST))

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
        f"Moet goedgekeurd worden; alleen {_AFZENDERDOMEINEN}. "
        "Wijzigen = nieuwe aanvraag. Antwoorden zet je via Reply-To in je applicatie."
    ),
)

SEND_EMAIL_MESSAGES_PER_DAY = EditableVisualizer(
    editable=SEND_EMAIL_MESSAGES_PER_DAY_EDITABLE,
    widget=WidgetType.NUMBER,
    label="Maximaal aantal berichten per dag",
    description=(
        f"Standaard {_STANDAARD} per dag; leeg laten betekent die standaard. "
        f"Een lager getal kies je zelf, boven {_STANDAARD} is het een aanvraag."
    ),
    # Grenzen op het veld zelf, anders stappen de spinner-pijltjes vrolijk naar -1 en per
    # 1 tegelijk. Stappen van 50: een dagbudget is een orde van grootte, geen exact getal.
    # De server bewaakt hetzelfde bereik (zie de editable).
    attributes={"min": "50", "max": str(MAX_MESSAGES_PER_DAY), "step": "50"},
    help_text=(
        "De relay houdt per project bij hoeveel berichten er die dag verstuurd zijn, en telt "
        "per bericht en niet per ontvanger. Zit je aan je maximum, dan krijgt je applicatie een "
        "tijdelijke weigering en probeert ze het later opnieuw. Dit is er vooral om te voorkomen "
        "dat een fout in je code duizenden berichten verstuurt voordat iemand het merkt. Vul je "
        f"meer dan {_STANDAARD} in, dan is dat een aanvraag die een beheerder goedkeurt; tot die "
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
    """Het veld van een deployment: dezelfde editable-regels, een eigen uitleg.

    De uitleg is wel anders dan op het project, en dat is de helft die je makkelijk
    overslaat: leeg betekent hier ERVEN en niet "standaard", en dat verschil is precies wat
    een lezer van dit formulier moet weten.
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
        f"{_AFZENDERDOMEINEN}. Wijzigen = nieuwe aanvraag, en tot de goedkeuring blijft gelden wat er al gold."
    ),
)

SEND_EMAIL_DEPLOYMENT_MESSAGES_PER_DAY = _deployment_field(
    SEND_EMAIL_DEPLOYMENT_MESSAGES_PER_DAY_EDITABLE,
    label="Maximaal aantal berichten per dag",
    description="Het dagbudget van DEZE deployment; zij heeft haar eigen account en dus haar eigen teller.",
    widget=WidgetType.NUMBER,
    attributes={"min": "50", "max": str(MAX_MESSAGES_PER_DAY), "step": "50"},
    help_text=(
        f"Laat je dit leeg, dan geldt het budget van het project. Tot en met {_STANDAARD} per dag kies je "
        "zelf, ook als het project meer mag; daarboven is het een aanvraag die een beheerder goedkeurt. Tot "
        "die tijd blijft gelden wat er al gold."
    ),
)

#: In formulierorde, gelijk aan de editables.
SEND_EMAIL_DEPLOYMENT_VISUALIZERS = (
    SEND_EMAIL_DEPLOYMENT_FROM_NAME,
    SEND_EMAIL_DEPLOYMENT_FROM_ADDRESS,
    SEND_EMAIL_DEPLOYMENT_MESSAGES_PER_DAY,
)

#: Wat een lege placeholder betekent per veld, als het project er geen waarde heeft. Voor
#: het adres is leeg geen ontbrekende waarde: dan leidt de relay het af uit de accountnaam.
_LEEG_ERFT = {
    "from-name": "geen naam",
    "from-address": "wordt afgeleid uit je projectnaam",
    "messages-per-day": "",
}


def deployment_visualizers(inherited: dict[str, str] | None = None) -> tuple[EditableVisualizer, ...]:
    """De drie velden van een deployment, met de geerfde waarde als placeholder.

    Een kopie per aanroep en geen gedeelde instantie: de placeholder hangt aan het PROJECT
    waar het formulier voor wordt gebouwd, en een gedeeld object zou die waarde aan het
    volgende project doorgeven.
    """
    if inherited is None:
        return SEND_EMAIL_DEPLOYMENT_VISUALIZERS
    velden = []
    for veld in SEND_EMAIL_DEPLOYMENT_VISUALIZERS:
        sleutel = veld.editable.yaml_path.rsplit("/", 1)[1]
        waarde = inherited.get(sleutel) or _LEEG_ERFT.get(sleutel, "")
        velden.append(replace(veld, placeholder=f"van het project: {waarde}" if waarde else None))
    return tuple(velden)
