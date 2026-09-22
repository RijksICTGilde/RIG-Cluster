# Het repository-pad blijft binnen de repo

Status: plan, 17 september 2026. Niet gebouwd. Komt uit de securityreview van RC-177 (PR #165, ronde 15), waar het als niet door die PR geïntroduceerd is afgesplitst. Het staat al langer op main.

## Wat er is, gemeten

`repositories[].path` is vrij invulbaar door een projectbeheerder:

- in `opi/schemas/project_v2.json` is `$defs.repository.path` letterlijk `{"type": "string"}`, zonder patroon;
- in `opi/forms/models/project_file.py:431` is het een gewoon tekstveld met standaard `.`.

Die waarde gaat als voorvoegsel het bestandspad in bij de manifestschrijvers. In `opi/utils/naming.py:1396`:

```python
cluster_clean = _sanitize_for_lowercase(cluster)
project_clean = _sanitize_for_lowercase(project_name)

if repo_path:
    return f"{repo_path}/{cluster_clean}/{project_clean}/{PROJECT_LEVEL_DIR}"
```

`cluster` en `project_name` worden gesaneerd, `repo_path` niet. Er is geen normalisatie en geen ankercontrole, dus:

```
generate_project_level_manifest_path('odcn-production', 'demo', '../../../../tmp/uit-de-repo')
```

levert een pad buiten de deployments-repo op. Dezelfde vorm zit in `generate_infrastructure_manifest_path` ernaast.

## Wat er moet gebeuren

**Weiger de waarde bij de poort, en anker hem bij de schrijver.** Allebei, want ze vangen verschillende gevallen: het schema houdt nieuwe invoer tegen, het anker beschermt tegen wat er al in bestaande projectbestanden staat.

### 1. Een patroon in het schema en in het formuliermodel

Een relatief pad zonder `..`, zonder voorloopstreep en zonder backslash. Zet het patroon op één plek en laat het schemafragment en het pydantic-model er allebei uit putten, zodat de melding van de API en die van het formulier niet uiteenlopen.

De standaard `.` moet geldig blijven, want die staat vandaag in elk projectbestand.

### 2. Een ankercontrole bij het samenstellen

In `naming.py`, op de plek waar `repo_path` wordt voorgevoegd: normaliseer het samengestelde pad en weiger het als de uitkomst buiten de basis valt. Dat is de controle die blijft werken als er ooit een andere weg naar diezelfde functie loopt.

Eén helper, gebruikt door beide functies die `repo_path` voorvoegen. Twee losse controles die elk hun eigen randgeval missen is precies het patroon dat dit soort gaten laat bestaan.

### 3. Bestaande projectbestanden nalopen

Voer geen migratie uit, maar stel wel vast of er vandaag een projectbestand is met een `path` die de nieuwe regel niet haalt. Staat die er, dan breekt de eerstvolgende verwerking van dat project en dat wil je van tevoren weten, niet uit een foutmelding van een gebruiker. Zet de uitkomst in de PR, ook als het er nul zijn.

## De toets

- `path: '../../../../tmp/uit-de-repo'` wordt geweigerd bij het opslaan, met een leesbare melding;
- `path: '/etc'` en `path: 'map\\..\\..'` worden geweigerd;
- `path: '.'` en `path: 'sub/map'` blijven werken, want dat is wat er vandaag in gebruik is;
- `generate_project_level_manifest_path` en `generate_infrastructure_manifest_path` weigeren een uitkomst buiten de basis, ook als de waarde langs het schema zou zijn gekomen. Toets die functies rechtstreeks, niet alleen via het opslaan;
- de drift-test op `project_v2.json` is groen na het opnieuw genereren;
- een bestaand projectbestand met `path: '.'` valideert ongewijzigd.

## Waar op te letten

**Dit is een tenantgrens, geen opmaakregel.** De manifestschrijvers schrijven naar de gedeelde deployments-repo. Een pad dat eruit breekt, schrijft in de map van een ander project.

**Het schema alleen is niet genoeg.** Wie denkt dat een patroon in `project_v2.json` de zaak afdekt, vergeet dat een projectbestand ook langs andere wegen in de repo komt en dat bestaande bestanden niet opnieuw gevalideerd worden. Daarom staat de ankercontrole in stap 2 er expliciet naast en niet als alternatief.
