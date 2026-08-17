# De generatie loopt niet op, en een herhaalde restore verdubbelt de rijen

Gevonden tijdens de sandboxmeting van RC-121, als aparte bevinding naast de reparatie die daar wel in zat.

Een restore hoort de generatie op te hogen en de teruggezette data in een database met een nieuwe naam te zetten (`{db}`, `{db}_v1`, `{db}_v2`). Dat gebeurt niet: **elke restoreronde meldt opnieuw 0 -> 1.** Bron en doel krijgen dus dezelfde naam, en de tweede restore schrijft de dump nog eens in dezelfde database. Rijen verdubbelen.

## Wat er gemeten is

De schrijfkant en de leeskant gebruiken twee verschillende opslagplekken in het projectbestand:

- **schrijven**: `opi/api/restore_router.py:1330,1335,2233,2238` roept `set_deployment_service_generation` aan, en dat zet de generatie in het **deployment-brede** services-blok;
- **lezen**: `opi/api/restore_router.py:2418` roept `get_database_generation` aan, en dat leest het **component-brede** reference/config-blok (`_set_service_config_generation` in `opi/handlers/project_file_handler.py:1827`).

Wat je schrijft komt dus nooit terug op de plek waar je het weer ophaalt.

De lezers zijn het onderling ook niet eens, en dat is geen detail maar het bewijs dat dit niet één vergissing op één regel is:

- `opi/manager/database_manager.py:111` leest deployment-breed;
- `opi/core/task_handlers_backup.py:122` en `opi/api/backup_router.py:484` lezen component-breed;
- `opi/core/backup_tasks.py:491` schrijft component-breed, terwijl de restore deployment-breed schrijft;
- `opi/manager/minio_manager.py` en `opi/jobs/reconciliation.py` doen het weer deployment-breed.

**Meet dit opnieuw voordat je iets kiest.** Dit is één sessie lezen, geen doorloop op het cluster, en de vraag welke van de twee plekken de juiste is hangt af van iets dat hier niet beantwoord wordt (zie hieronder).

## De vraag die eerst beantwoord moet worden

**Hoort een generatie bij een deployment of bij een component?** Dat is geen smaakkwestie en het bepaalt de hele reparatie:

- hoort hij bij het **component**, dan kan één deployment componenten met verschillende generaties hebben, en moet elke deployment-brede schrijver mee;
- hoort hij bij de **deployment**, dan is het component-brede blok het overblijfsel en moeten de lezers daar vanaf.

Kies op grond van wat een generatie BETEKENT (een database wordt per component geprovisioneerd of niet) en niet op grond van welke van de twee de minste regels kost. Schrijf het antwoord op in de PR.

## Wat er ook nog uit moet komen

**Bestaande projecten.** Er staan projectbestanden in productie met een generatie op de ene of de andere plek, en misschien op allebei met verschillende waarden. Wat er met die gegevens gebeurt hoort in het plan: migreren bij het lezen, of eenmalig via de schemamigratie. Wat NIET mag is een reparatie die stil de hoogste of de laagste kiest, want dan kan een database die al bestaat opnieuw als doelnaam uitkomen.

**De verdubbelde rijen zijn het echte gevaar, niet het foute getal.** Ook met de generatie gerepareerd hoort een restore die in een BESTAANDE database zou schrijven zich te verzetten in plaats van erin te dumpen. Zoek uit of dat vandaag ergens afgevangen wordt, en zo nee, of dat in deze taak hoort of een eigen taak is. Meld het oordeel, ook als het "apart" is.

**De andere diensten.** Buckets (`minio_manager.py`) en opslag hebben dezelfde tweedeling. Als de reparatie voor databases ook daar geldt, doe ze dan samen; verschillen ze, benoem dan waarom.

## Verifieerbaar

- Een test die twee restores achter elkaar doet en aantoont dat de tweede een ANDERE doelnaam krijgt dan de eerste. Rood zonder de wijziging.
- Een test op de migratie van een bestaand projectbestand met de generatie op de oude plek.
- Op het cluster gemeten: twee restorerondes op hetzelfde project, en na afloop het aantal rijen geteld. Meld de getallen, niet "het werkt".
- `uv run pytest tests/ -q` groen, plus `ruff check`, `ruff format`, `pyright`.

## Wat er buiten valt

- De restore-reparatie uit RC-121, die is gemerged.
- Productie.
