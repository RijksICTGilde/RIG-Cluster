"""Visualizers for the send-email service (project-level)."""

from __future__ import annotations

from opi.forms.editables.editable import WidgetType
from opi.forms.visualizers.visualizer import EditableVisualizer
from opi.services.catalog.send_email.config_model import MAX_MESSAGES_PER_DAY
from opi.services.catalog.send_email.editables import (
    SEND_EMAIL_FROM_NAME_EDITABLE,
    SEND_EMAIL_MESSAGES_PER_DAY_EDITABLE,
)

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
    description=f"Een getal tussen 1 en {MAX_MESSAGES_PER_DAY}. Leeg laten kan ook.",
    help_text=(
        "De relay houdt per project bij hoeveel berichten er die dag verstuurd zijn. Zit je "
        "aan je maximum, dan krijgt je applicatie een tijdelijke weigering en probeert ze het "
        "later opnieuw. Dit is er vooral om te voorkomen dat een fout in je code duizenden "
        "berichten verstuurt voordat iemand het merkt. Laat je het leeg, dan geldt de "
        "standaard van het platform."
    ),
)
