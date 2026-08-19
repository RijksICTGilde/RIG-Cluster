"""Wat een taak kan zeggen over een mislukking die niet aan het platform ligt.

Een handler kan op twee manieren falen: hij geeft een faal-dict terug (blijvend, geen
nieuwe poging) of hij gooit (de worker probeert het opnieuw). Dat onderscheid is precies
goed voor een storing die overwaait, en precies verkeerd voor een verzoek dat niet kan:
"dit component bestaat niet" wordt bij een tweede poging niet waar.

``TaskInputError`` is de gooiende vorm van "de aanroeper vroeg iets wat niet kan". De
worker maakt er een blijvende mislukking van MET het ``error_type`` dat de werper meegaf,
zodat een client hem kan toeschrijven in plaats van hem als onverklaarbaar te zien. Het
vertalen van dat type naar een categorie gebeurt niet hier maar op de plek waar een
taakrecord een antwoord wordt (``opi/api/task_models.py``), zodat er één vertaling is.

Gemeld door de zad-cli als punt 26b: ``update-image`` op een component dat niet bestaat
kwam terug zonder categorie en zonder type, en dus als exit 3 ("niet toe te schrijven")
terwijl het gewoon een typefout in de aanroep was.
"""

from __future__ import annotations

#: Wat een fout is als de aanroeper niets fout deed en wij het niet nader weten.
INTERNAL_ERROR_TYPE = "internal_error"

#: Wat een 404 uit een manager betekent voor de aanroeper van een taak.
NOT_FOUND_ERROR_TYPE = "not_found"

#: Wat elke andere 4xx betekent: het verzoek zelf deugt niet.
INVALID_REQUEST_ERROR_TYPE = "invalid_request"

#: De ``error_type``-waarden die zeggen: de aanroeper vroeg iets wat niet kan. Een taak die
#: hierop faalt is geen storing, dus hij wordt op WARNING gelogd en niet op ERROR. Dit is de
#: enige lijst; de types zelf komen uit de managers, er wordt er hier geen bedacht.
CLIENT_ERROR_TYPES = frozenset(
    {
        "validation_error",
        "invalid_component_references",
        NOT_FOUND_ERROR_TYPE,
        INVALID_REQUEST_ERROR_TYPE,
    }
)


class TaskInputError(Exception):
    """De aanroeper vroeg iets wat niet kan; opnieuw proberen verandert daar niets aan.

    ``error_type`` is de specifieke reden, in dezelfde woorden die de managers al
    gebruiken (``deployment_not_found``, ``component_not_found``, ...), zodat er geen
    tweede woordenlijst ontstaat.
    """

    def __init__(self, message: str, error_type: str = INVALID_REQUEST_ERROR_TYPE) -> None:
        super().__init__(message)
        self.error_type = error_type
