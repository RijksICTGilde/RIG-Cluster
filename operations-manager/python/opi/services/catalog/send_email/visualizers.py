"""Visualizers for the send-email service (project-level)."""

from __future__ import annotations

from opi.core.config import settings
from opi.forms.editables.editable import WidgetType
from opi.forms.visualizers.visualizer import EditableVisualizer
from opi.services.catalog.send_email.editables import (
    SEND_EMAIL_FROM_NAME_EDITABLE,
    SEND_EMAIL_MESSAGES_PER_DAY_EDITABLE,
)

#: De grens van het klantveld is de platformstandaard, niet het schemamaximum; zie de
#: toelichting bij de editable.
_KLANT_MAX = settings.MAIL_PROJECT_DEFAULT_MESSAGES_PER_DAY

SEND_EMAIL_FROM_NAME = EditableVisualizer(
    editable=SEND_EMAIL_FROM_NAME_EDITABLE,
    widget=WidgetType.TEXT,
    label="Naam van de afzender",
    description="De naam die de ontvanger boven het bericht ziet staan.",
    help_text=(
        "Dit is het enige deel van de afzender dat je zelf kiest. Laat je het leeg, dan ziet "
        "de ontvanger alleen het e-mailadres. Het adres zelf ligt vast en is voor alle projecten "
        "hetzelfde, omdat post uit een ander domein bij de ontvanger niet aankomt."
    ),
)

SEND_EMAIL_MESSAGES_PER_DAY = EditableVisualizer(
    editable=SEND_EMAIL_MESSAGES_PER_DAY_EDITABLE,
    widget=WidgetType.NUMBER,
    label="Maximaal aantal berichten per dag",
    description=(
        f"Standaard {_KLANT_MAX} per dag; leeg laten betekent die standaard. "
        "Hier kun je jezelf alleen een lagere grens geven."
    ),
    # Grenzen op het veld zelf, anders stappen de spinner-pijltjes vrolijk naar -1 en per
    # 1 tegelijk. Stappen van 50: een dagbudget is een orde van grootte, geen exact getal.
    # De server bewaakt hetzelfde bereik (zie de editable).
    attributes={"min": "50", "max": str(_KLANT_MAX), "step": "50"},
    help_text=(
        "De relay houdt per project bij hoeveel berichten er die dag verstuurd zijn. Zit je "
        "aan je maximum, dan krijgt je applicatie een tijdelijke weigering en probeert ze het "
        "later opnieuw. Dit is er vooral om te voorkomen dat een fout in je code duizenden "
        "berichten verstuurt voordat iemand het merkt. Heb je structureel meer dan "
        f"{_KLANT_MAX} berichten per dag nodig, dan is dat een afspraak met de beheerder; "
        "dat stel je hier niet zelf in."
    ),
)
