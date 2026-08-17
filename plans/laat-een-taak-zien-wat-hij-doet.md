# Laat een taak zien wat hij doet

Status: plan, 5 augustus 2026. Niet gebouwd. Aanleiding: gebruikers willen grip op wat een aangevraagde wijziging doet. Het mechanisme daarvoor bestaat compleet en wordt op de plekken waar het het meest nodig is niet gebruikt.

## Wat er al is

Subtaken zijn er van boven tot onder:

- `SubtaskStatus` in `opi/api/task_models.py`
- `add_subtask`, `complete_subtask` en `fail_subtask` op de voortgangsmanager (`opi/core/persistent_task_progress.py`)
- `templates/partials/task_progress_fragment.html.j2` rendert ze inclusief status, en pollt zichzelf

Er is ook een werkend voorbeeld om van af te kijken: `opi/core/task_handlers_backup.py` gebruikt het drie keer.

## Waar het niet gebruikt wordt, gemeten op 5 augustus

```
task_handlers_backup       add_subtask = 3
task_handlers_project      add_subtask = 2
task_handlers_operations   add_subtask = 0
sleep_mode/task.py         add_subtask = 0
```

`task_handlers_operations.py` is precies het bestand met de taken waar iemand naar zit te kijken: een deployment of project herverwerken, een database of bucket klonen, een image bijwerken. Die tonen nu een balk en één regel tekst, terwijl de handler onderwater een reeks stappen doorloopt.

De slaaptaak die op 5 augustus is toegevoegd doet hetzelfde: twee grove stappen (10% en 100%) terwijl er tussenin naar git gecommit wordt en de hele reprocessing draait, ArgoCD-sync inbegrepen. Juist die wachttijd was de reden om er een taak van te maken.

(De oude notitie zei "alleen backup gebruikt het". Dat is inmiddels niet meer waar: de projecthandlers doen het ook. Het gat zit in de operations-handlers.)

## Voorstel

Het patroon uit backup kopiëren naar de plekken waar gebruikers wachten. Geen nieuw mechanisme, geen nieuw ontwerp.

1. **Herverwerken** (`handle_refresh_deployment`, `handle_refresh_project`): de stappen die de handler nu al doorloopt als subtaken benoemen.
2. **Slapen en wekken** (`opi/services/catalog/sleep_mode/task.py`): commit, manifesten, sync. Klein, en het maakt meteen zichtbaar waarom die actie tijd kost.
3. **Klonen en image bijwerken**: dezelfde behandeling, als de stappen daar duidelijk af te bakenen zijn.

Neem daarbij hetzelfde mee als in de logging-sectie van `instructions/service-review-checklist.md`: begin, voortgang en eind met context, en het verschil tussen "er was niets te doen" en "het lukte niet". Een subtaak die stil slaagt terwijl hij is overgeslagen is misleidender dan geen subtaak.

## Volgorde

1. Eén handler doen, `handle_refresh_deployment`, en daar de vorm op vastleggen: welke stappen, hoe genoemd, wanneer afgerond, wat er bij een fout gebeurt. Verifiëren met een test die de subtaken van een verlopen taak naloopt, niet alleen dat ze bestaan.
2. De rest daarop laten volgen, inclusief de slaaptaak.
3. Nakijken of het voortgangsfragment het aankan als een taak veel subtaken heeft; het pollt elke twee seconden en vervangt zichzelf.

## Waar op te letten

**Namen zijn de hele opbrengst.** "Stap 3 van 7" helpt niemand; "Manifesten genereren" en "Wachten tot ArgoCD gesynchroniseerd is" wel. Dit is een taak waarbij de tekst het product is, dus schrijf ze in gewone taal en in het Nederlands, zoals de rest van de interface.

**Een subtaak mag niet liegen.** `complete_subtask` op iets dat is overgeslagen, of een handler die na een fout gewoon doorloopt, maakt het scherm slechter dan de balk die er nu staat. Dat is dezelfde eis die vandaag bij de gezondheidscheck gold: liever niets melden dan iets geruststellends melden.

**Niet elke stap is een subtaak.** Als er twintig verschijnen voor een gewone herverwerking is het scherm onleesbaar. Kies de stappen die een gebruiker herkent en waar de tijd in gaat.
