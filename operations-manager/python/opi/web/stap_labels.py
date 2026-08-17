"""De stapregel van een taak, in het Nederlands, op het moment van TONEN.

De interface is Nederlands, maar op het scherm kwam nog ``Queued`` voorbij, plus
``Starting...``, ``Done`` en ``Failed: <fout>``. Dat zijn geen labels maar OPGESLAGEN
waarden: ``Queued`` is de kolomstandaard van ``async_tasks.current_step`` (zie
``opi/core/async_task_schema.py`` en ``opi/services/persistence/async_tasks.py``), de
andere drie worden door de voortgangsschrijvers weggeschreven.

DE KEUZE, EXPLICIET: er wordt bij de WEERGAVE vertaald, niet bij het opslaan.

De andere weg - de waarde omzetten, alle lezers nalopen en de bestaande rijen migreren -
raakt drie dingen tegelijk (de kolomstandaard, elke plek die de tekst schrijft, en de
rijen die er al staan) en levert niets extra's op: de opgeslagen tekst is een technisch
spoor, geen gebruikerstekst. Bovendien blijven bestaande taken zo gewoon leesbaar; een
migratie die halverwege stopt zou een tabel met twee talen achterlaten.

De API vertaalt met opzet NIET (``opi/api/task_models.py``): dat is een machinecontract,
en de zad-cli en de tests lezen die waarde. Vertalen hoort waar de tekst een mens bereikt.
"""

from __future__ import annotations

#: De vaste, in het Engels opgeslagen stapregels, met hun Nederlandse weergave.
STAP_LABELS = {
    "Queued": "In wachtrij",
    "Starting...": "Wordt gestart...",
    "Done": "Klaar",
}

#: ``Failed: <fout>`` draagt de fouttekst achter de dubbele punt; alleen de kop vertaalt.
_MISLUKT_PREFIX = "Failed: "


def stap_label(stap: str | None) -> str | None:
    """De Nederlandse weergave van een opgeslagen stapregel.

    Een stap die hier niet in staat gaat ongewijzigd door: de meeste stapregels worden al
    in het Nederlands geschreven (``format_step_line`` zet er de naam van de stap in), en
    een onbekende regel raden zou erger zijn dan hem laten staan.
    """
    if not stap:
        return stap
    if stap in STAP_LABELS:
        return STAP_LABELS[stap]
    if stap.startswith(_MISLUKT_PREFIX):
        return "Mislukt: " + stap[len(_MISLUKT_PREFIX) :]
    return stap
