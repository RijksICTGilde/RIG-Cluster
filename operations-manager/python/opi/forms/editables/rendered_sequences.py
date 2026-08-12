"""Welke reeksen het formulier daadwerkelijk heeft getekend.

Een reeks die de gebruiker leegmaakt en een reeks die het formulier nooit toonde,
komen op precies dezelfde manier terug: er staat niets over in de inzending. De
verwerker maakte daar tot nu toe een lijst van, en schreef in beide gevallen ``[]``.
Voor het eerste geval is dat wat de gebruiker vroeg; voor het tweede is het
gegevensverlies -- in productie zijn zo ``additional-clients``-vermeldingen
verdwenen.

De vorm die daarbij past is dezelfde als in ``wizard/write_set.py``: leid het af van
de stroom, in plaats van veldnamen op te sommen. Het formulier is de stroom, dus het
formulier zegt het zelf: elke gerenderde reeks stuurt een verborgen veld met haar
eigen pad mee. Staat het pad in die lijst, dan betekent "geen items" leeggemaakt.
Staat het er niet in, dan ging deze sectie er niet over en blijft de opgeslagen
waarde staan.

Draagt de inzending het veld helemaal niet, dan is er geen uitspraak gedaan (de
eindinzending van de wizard geeft de samengevoegde projectgegevens als "inzending"
mee, en die kent geen formuliervelden). Dan blijft het oude gedrag gelden.
"""

from __future__ import annotations

from typing import Any

#: Naam van het verborgen veld dat elke gerenderde reeks meestuurt. Met ``[]`` erachter
#: in het formulier, zodat json-enc de haakjes wegstreept en de meerdere waarden als
#: lijst aankomen (dezelfde route die ``services[]`` al loopt).
GERENDERDE_REEKSEN_VELD = "_gerenderde-reeksen"


def rendered_sequence_paths(submitted: dict[str, Any]) -> set[str] | None:
    """De paden van de reeksen die dit formulier tekende, of None als het niets zegt.

    Bij een enkele gerenderde reeks levert htmx een string in plaats van een lijst;
    beide vormen komen hier binnen.
    """
    raw = submitted.get(GERENDERDE_REEKSEN_VELD)
    if isinstance(raw, str):
        return {raw}
    if isinstance(raw, list):
        return {path for path in raw if isinstance(path, str)}
    return None


def sequence_was_not_drawn(submitted: dict[str, Any], *paths: str) -> bool:
    """Of de inzending zegt dat geen van *paths* op het scherm stond.

    *paths* zijn de vormen waarin dezelfde reeks kan heten: het echte yaml_path en,
    bij een gevirtualiseerde reeks, het pad waaronder het formulier hem post.
    """
    drawn = rendered_sequence_paths(submitted)
    if drawn is None:
        return False
    return not any(path in drawn for path in paths)
