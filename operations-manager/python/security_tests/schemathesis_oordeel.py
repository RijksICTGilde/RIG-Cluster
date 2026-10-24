"""De beoordeling van een geweigerd verzoek, los van schemathesis zelf.

Het importeren van ``test_schemathesis.py`` haalt altijd een draaiende sandbox op (schemathesis
leest het schema bij het laden van de module), dus zonder sandbox wordt die hele module
overgeslagen en blijft deze beoordelingslogica ongetoetst. Door haar hier apart te zetten kan
``test_schemathesis_oordeel.py`` haar zonder sandbox beproeven.
"""

from __future__ import annotations

#: Statuscodes waarmee een geweigerd verzoek mag antwoorden. Inclusief 4xx voor niet-bestaande
#: operaties en foute invoer, 429 voor de ratelimiter van RC-368 (die weigert voor de handler,
#: dus de aanroeper kwam ook dan niet binnen), en 501 voor routes zonder geconfigureerd secret.
GEWEIGERD = frozenset({400, 401, 403, 404, 405, 409, 415, 422, 429, 501})


def beoordeel_weigering(status: int, operatie: str, herkenning: str) -> str | None:
    """De melding als een verzoek dat geweigerd hoort te worden dat niet is."""
    if status in GEWEIGERD:
        return None
    if status >= 500:
        return f"{operatie} geeft {herkenning} een {status}; het verzoek raakte de applicatie in plaats van de grendel"
    return f"{operatie} laat {herkenning} door met {status}"
