# Laat een taak zien wat hij doet

**Status**: Implemented
**Date**: 2026-08-05
**Related**: [Async Task System](./async-task-system.md), [Eén voortgangsweergave](./task-progress-view.md)

## Wat het is

Een taak die minuten duurt toont niet langer alleen een balk en een regel tekst, maar
noemt de stappen die hij zet: het projectbestand ophalen, diensten en manifesten
bijwerken, wachten tot ArgoCD gesynchroniseerd is. De stappen verschijnen terwijl ze
lopen, met een vinkje als ze klaar zijn en een kruis als ze mislukken.

Het mechanisme (subtaken op de voortgangsmanager, gerenderd door
`templates/partials/task_progress_fragment.html.j2`) bestond al; wat ontbrak was gebruik
ervan op de plekken waar mensen zitten te wachten.

## Waar het zichtbaar is

| Actie | Stappen die je ziet |
|---|---|
| Deployment of project herverwerken | Project opzoeken; Projectbestand ophalen en controleren; (Projectbestand bijwerken naar de nieuwste vorm); (Verwijderde deployments opruimen); Diensten en manifesten bijwerken; Wachten tot ArgoCD gesynchroniseerd is (met per deployment een regel over waar hij op wacht) |
| Deployment in slaapstand zetten / wekken | Projectgegevens ophalen; Slaap- of wektoestand vastleggen in git; daarna dezelfde stappen van het herverwerken |
| Image bijwerken | Projectgegevens ophalen; Nieuwe image vastleggen in het projectbestand; Diensten en manifesten bijwerken; ArgoCD laten uitrollen |
| Database of bucket klonen | Projectgegevens ophalen; Database/bucket kopiëren (met bron en verbindingswijze in de naam) |

Stappen tussen haakjes verschijnen alleen als dat werk echt gebeurt.

## De regel: een stap mag niet liegen

Dit is de kern van de functie, niet een detail.

- Een stap wordt pas geopend als het werk erachter begint. Werk dat wordt overgeslagen
  krijgt geen stap, dus geen vinkje dat het niet verdient. De schema-migratie en het
  opruimen van verwijderde deployments verschijnen daarom alleen als ze plaatsvinden.
- Mislukt een stap, dan wordt hij als mislukt afgesloten met de reden, en volgen er geen
  groene stappen na. Een mislukte herverwerking toont geen groene ArgoCD-stap voor een
  synchronisatie die nooit is uitgevoerd.
- "Er was niets te doen" wordt met zoveel woorden gezegd: slapen zetten van een
  deployment die al slaapt levert de regel "Geen wijziging nodig, de deployment is al
  sleeping" op, geen reeks afgevinkte stappen.
- De namen staan in gewone taal en in het Nederlands, zoals de rest van de interface.

## Hoe je een stap toevoegt

In `ProjectManager` staan twee helpers:

```python
step = self._begin_step("Diensten en manifesten bijwerken")   # None zonder voortgangsmanager
...
self._end_step(step)                     # klaar
self._end_step(step, "reden waarom niet") # mislukt
```

`_begin_step` geeft `None` terug als er geen voortgangsmanager hangt, zodat dezelfde
code ook draait vanuit de sweeper, de scheduler of de CLI zonder iets te melden.

In een taakhandler gebruik je de voortgangsmanager rechtstreeks
(`progress.add_task(...)`, `complete_task`, `fail_task`). Wil je dat de stappen van de
herverwerking op jouw taak verschijnen, geef de voortgangsmanager dan mee:

- aan `process_project_from_git(..., task_progress_manager=progress)`
- of aan `trigger_reprocessing(..., task_progress_manager=progress)`

Alle stappen zijn top-level taken; alleen de ArgoCD-wachtstap hangt er per deployment
een regel onder, want het fragment rendert twee niveaus.

## Waar op te letten

- **Niet elke stap is een subtaak.** Kies de stappen die een gebruiker herkent en waar
  de tijd in gaat. Twintig regels voor een gewone herverwerking maken het scherm
  onleesbaar.
- **Het percentage in de balk is een verhouding** (afgeronde stappen gedeeld door
  bekende stappen, afgetopt op 99). Omdat stappen gaandeweg verschijnen kan de balk
  tussentijds terugspringen. Dat is bestaand gedrag van de voortgangsbalk en verandert
  niet met deze functie.
- Het fragment zelf kent geen limiet: het rendert alle stappen en blijft één
  poll-container van twee seconden, ook bij twintig deployments
  (`tests/test_task_progress_steps.py`).
