# Projecten zonder deployments

Een project dat via `POST /api/v2/projects` is aangemaakt heeft **geen deployments**. Dat
is bewust: er valt op dat moment nog niets uit te rollen. Sinds die endpoint bestaat
(RC-51) is dat dus een normale toestand van een projectbestand, en alles wat erna komt
moet ermee omgaan.

Dat deed het niet. Een externe client (zad-cli) speelde op 10 augustus 2026 een volledig
draaiboek af tegen de sandbox en kwam niet tot een draaiende applicatie: de eerste
deployment aanmaken faalde, en daarmee alles daarna.

## Wat er misging

### De eerste deployning liep op een ontbrekende sleutel

`POST /api/v2/projects/{project}/:upsert-deployment` gaf, achter de validatie en op elk
vers project:

```
Error upserting deployment 'productie': 'deployments'
```

Die aangehaalde `'deployments'` was een `KeyError`: het aanmaakpad deed
`project_data["deployments"].append(...)` op een projectbestand dat die sleutel niet
heeft. Dezelfde aanname als de wizardfout van een dag eerder (de eerste deployment kreeg
geen cluster en geen repository omdat die van een *bestaande* deployment werden
gekopieerd), op een andere plek.

Nu wordt de lijst aangemaakt als hij er niet is. De rest van het aanmaakpad leunt al niet
op een bestaande deployment: cluster, namespace en repository komen uit het project zelf,
en `cloneFrom` naar een deployment die er niet is geeft nog steeds een nette fout die de
gevraagde bron noemt.

### Verversen meldde een mislukking terwijl er niets te doen was

`POST /api/v2/projects/{project}/:refresh` faalde op de stap "Diensten en manifesten
bijwerken". `process_project` gaf `False` terug zodra een project geen deployments op
*deze* cluster had - en dat is geen fout maar een lege werklijst. Twee gevallen vallen
eronder:

- een vers project, dat nog geen enkele deployment heeft;
- een project waarvan alle deployments op een andere cluster staan; die zijn het werk van
  de operations manager dáár.

Beide zijn nu succes: er is niets te verzoenen, er gebeurt niets, en dat wordt zo gemeld.

### De melding wees naar logs waar de aanroeper niet bij kan

Faalde het verwerken echt, dan luidde de melding "Project processing failed - check logs
for details". Een projectgebruiker kan niet bij de logs van de operations manager. De
opgeslagen reden (`_processing_error`, bijvoorbeeld de dienst die niet aangemaakt kon
worden) staat nu in het antwoord; alleen als die er niet is, blijft er een algemene
melding staan - zonder verwijzing naar logs.

### Verwijderen meldde succes zonder te zeggen wat er gebeurd was

`DELETE /api/v2/projects/{project}/{deployment}` op een deployment die niet bestaat is
succes. Dat is een bewuste keuze: verwijderen is idempotent, en de nachtelijke opruimer
leunt erop. Maar "hij is weg" en "hij was er niet" waren niet te onderscheiden, en in een
script leest het tweede als bevestiging dat er iets verwijderd is.

Het taakresultaat draagt nu beide feiten:

```jsonc
{"status": "completed", "deleted": true,  "already_absent": false,
 "message": "Deployment 'productie' in project 'demo' deleted successfully"}

{"status": "completed", "deleted": false, "already_absent": true,
 "message": "Deployment 'weg' bestond niet (meer) in project 'demo'; er is niets verwijderd"}
```

Het gedrag verandert niet - nog steeds succes, nog steeds geen fout. Het antwoord
verandert.

## Toetsen

Elke reparatie heeft een toets die op een **leeg** project draait; dat is de rode draad
onder alle vier. Een toets op een project mét deployment had geen van deze fouten
gevonden.

| wat | waar |
|---|---|
| eerste deployment op een project zonder deployments | `tests/test_first_deployment_on_empty_project.py` |
| verversen zonder deployments, en de melding bij een echte fout | `tests/test_refresh_without_deployments.py` |
| verwijderen van een deployment die er niet is | `tests/test_delete_deployment_already_absent.py` |

## Waar het staat

| onderdeel | bestand |
|---|---|
| aanmaken van de eerste deployment | `ProjectManager._upsert_deployment_once` |
| lege werklijst is succes | `ProjectManager.process_project` |
| de reden in plaats van de logverwijzing | `ProjectManager.process_project_from_git` |
| `already_absent` bij verwijderen | `DeleteProjectManager.delete_deployment`, `handle_delete_deployment` |
