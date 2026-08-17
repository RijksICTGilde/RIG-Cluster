# Stapregels van een taak in het Nederlands

## Wat het is

De voortgang van een achtergrondtaak toont een stapregel: "Realm aanmaken - productie",
"In wachtrij". Die regel komt uit `async_tasks.current_step` in de database. Een deel
ervan werd in het Engels weggeschreven en verscheen zo ook op het scherm, terwijl de rest
van de interface Nederlands is.

`opi/web/stap_labels.py` vertaalt die vaste regels op het moment van tonen.

| Opgeslagen | Op het scherm |
|---|---|
| `Queued` | In wachtrij |
| `Starting...` | Wordt gestart... |
| `Done` | Klaar |
| `Failed: <fout>` | Mislukt: `<fout>` |

Een regel die hier niet in staat gaat ongewijzigd door: de meeste stapregels worden al in
het Nederlands geschreven door `format_step_line()`, en een onbekende regel raden is
slechter dan hem laten staan.

## Waarom bij de weergave en niet bij het opslaan

`Queued` is de kolomstandaard van `current_step` (`opi/core/async_task_schema.py`,
`opi/services/persistence/async_tasks.py`). De waarde omzetten zou drie dingen tegelijk
raken: de standaard, elke plek die de tekst schrijft, en de rijen die er al staan. Dat
levert niets extra's op - de opgeslagen tekst is een technisch spoor, geen gebruikerstekst
- en een migratie die halverwege stopt laat een tabel met twee talen achter. Bestaande
taken blijven op deze manier gewoon leesbaar.

## Waar het toegepast wordt

Op de twee plekken waar een stapregel een mens bereikt:

- `opi/web/router.py`, `_v2_task_to_template_context()` - het voortgangsblok, dat door de
  voortgangspagina, het gepolde fragment en de wizardmodals gedeeld wordt;
- `opi/web/router_tasks.py`, `_normalize_task()` - de kolom in de tabel Taken.

De API (`opi/api/task_models.py`) vertaalt met opzet **niet**. Dat is een machinecontract:
de zad-cli en de tests lezen die waarde.

## Test

`tests/test_stap_labels.py` - de vertaling zelf, allebei de schermen, en de vastlegging dat
de kolomstandaard onveranderd is.
