# Het repositorypad blijft binnen de repo

`repositories[].path` in een projectbestand is het voorvoegsel waaronder OPI de manifesten van
een project schrijft (`{path}/{cluster}/{project}/{deployment}`, `.../infrastructure`,
`.../_project`). Dat pad mag de repository niet verlaten: de manifestschrijvers schrijven in een
gedeelde werkkopie, en een pad dat eruit breekt schrijft bij een ander project.

## De regel

Een relatief pad, zonder `..`, zonder voorloopstreep, zonder backslash en zonder stuurtekens.
`.` en een leeg pad betekenen de repository zelf.

| Waarde | Uitkomst |
|---|---|
| `.` | geldig (de standaard, staat in elk projectbestand uit het portaal) |
| `sub/map`, `./sub`, `infra` | geldig |
| `../../tmp/x`, `a..b` | geweigerd |
| `/etc` | geweigerd |
| `map\..\..` | geweigerd |

## Waar het wordt afgedwongen

Op twee plekken, omdat ze elk een ander geval vangen:

1. **De poort.** Het patroon `REPOSITORY_PATH_PATTERN` in `opi/forms/models/project_file.py`
   staat op `RepositoryModel.path` en, letterlijk hetzelfde, in `$defs/repository/path` van
   `opi/schemas/project_v2.json`. Een toets bewaakt dat de twee gelijk blijven. Een weigering
   noemt het veld (`repositories/0/path`) en de beschrijving uit het schema.
2. **De schrijver.** `generate_deployment_manifest_path`, `generate_infrastructure_manifest_path`
   en `generate_project_level_manifest_path` in `opi/utils/naming.py` normaliseren het
   samengestelde pad en geven `RepositoryPathError` als het buiten de repository valt. Dit vangt
   een bestaand projectbestand dat nooit opnieuw langs het schema komt. Elke manager die een
   manifestpad bouwt, gaat via deze functies.

## Bestaande projectbestanden

Er is geen migratie. Een bestand met een pad dat de regel niet haalt, wordt bij de eerstvolgende
verwerking geweigerd; een beheerder zet het pad dan op `.` of op een relatieve submap.

## Toetsen

`operations-manager/python/tests/test_repository_pad_binnen_de_repo.py`.
