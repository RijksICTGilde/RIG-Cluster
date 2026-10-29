"""Visualizers for the send-email service (project-level)."""

from __future__ import annotations

from opi.core.config import settings
from opi.forms.editables.editable import WidgetType
from opi.forms.visualizers.visualizer import EditableVisualizer
from opi.services.catalog.send_email.config_model import MAX_MESSAGES_PER_DAY
from opi.services.catalog.send_email.editables import (
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
