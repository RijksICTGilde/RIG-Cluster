# Een stap in de voortgang zegt waarvoor hij loopt

Status: plan, 12 augustus 2026. Aanleiding: bij het aanmaken van een project staat dit onder elkaar in de voortgangsindicator:

```
Project aanmaken
Kubernetes namespace(s) aanmaken
Database klaarmaken
Creating MinIO storage resources
Creating Redis cache resources
Database klaarmaken
Creating MinIO storage resources
Creating Redis cache resources
```

Dezelfde drie regels twee keer, zonder dat er iets bij staat waaraan je ziet welke deployment aan de beurt is. Wie kijkt of het opschiet, kan niet zien waar hij is.

## Wat er nu is, gemeten

`add_task(name)` neemt één string, en die string is de hele regel op het scherm. Er zijn **68** aanroepen; de signatuur staat in `opi/core/persistent_task_progress.py:176` en `opi/core/task_manager.py:91`.

De drie uit het voorbeeld:

| bestand | naam |
|---|---|
| `opi/manager/database_manager.py:157` | `"Database klaarmaken"` |
| `opi/manager/minio_manager.py:232` | `"Creating MinIO storage resources"` |
| `opi/manager/redis_manager.py:71` | `"Creating Redis cache resources"` |

Twee dingen vallen hier samen, en ze zijn los te repareren:

1. **De naam draagt geen context.** Wordt dezelfde stap voor twee deployments gedaan, dan staat hij twee keer identiek in de lijst.
2. **De helft is Engels.** "Database klaarmaken" naast "Creating MinIO storage resources" in hetzelfde blok. Een steekproef over alle 68: `Project processing` (6x), `Deployment processing` (2x) en meer.

## Wat er moet gebeuren

### 1. Een stap krijgt zijn onderwerp erbij

Geef `add_task` de mogelijkheid te zeggen **waarvoor** de stap loopt, en toon dat naast de naam. Denk aan de deploymentnaam, en waar dat betekenis heeft de component- of dienstnaam.

Kies bewust hóé het op het scherm komt, want dat is de eigenlijke ontwerpbeslissing:

* in de naam zelf (`"Database klaarmaken - productie"`), simpel maar het maakt de naam onbruikbaar om op te groeperen;
* als apart veld naast de naam, wat de weergave laat kiezen hoe het toont en groeperen mogelijk houdt.

Het tweede is te verkiezen, maar meet eerst wat de voortgangsweergave met een extra veld kan; ligt dat vast in een sjabloon dat alleen `name` kent, dan is dat een deel van het werk.

**Doe niet alle 68 aanroepen.** Begin bij de stappen die aantoonbaar herhalen omdat ze per deployment of per component draaien; dat zijn er een handvol. Een stap die één keer per project loopt (`"Project aanmaken"`) heeft geen onderwerp nodig, en die eraan toevoegen maakt de lijst alleen langer.

### 2. De Engelse regels naar het Nederlands

Het portaal is Nederlands. `Creating MinIO storage resources` en `Creating Redis cache resources` staan tussen Nederlandse regels en vallen op als iets dat vergeten is. Loop de 68 langs en vertaal wat Engels is, in de toon van de bestaande regels: `Database klaarmaken`, niet `De database wordt klaargemaakt`.

Let op `Project processing` (6x) en `Deployment processing` (2x): die komen op meerdere plekken voor, dus vertaal ze overal hetzelfde, anders lijkt het op het scherm alsof er twee verschillende dingen gebeuren.

## De toets

- een project met twee deployments toont per stap waarvoor hij loopt, en twee gelijke stappen zijn uit elkaar te houden;
- een stap die maar één keer per project draait heeft geen onderwerp gekregen;
- er staat geen Engels meer in de voortgangsindicator;
- `Project processing` en `Deployment processing` heten overal hetzelfde;
- een lopende taak blijft werken: de weergave verandert, de voortgang zelf niet.

## Waar op te letten

**De namen zijn geen identiteit.** `add_task` geeft een task-id terug en daar hangt de status aan; als ergens op de naam wordt gezocht of vergeleken, breekt dat door een hernoeming. Controleer dat vóór het vertalen, want dat is precies het soort fout dat pas bij een mislukte stap opvalt.

**Bestaande taken in de opslag.** `persistent_task_progress` bewaart namen zoals ze geschreven zijn. Taken die al liepen dragen de oude naam; dat is geen probleem zolang de weergave daar niet op struikelt, maar controleer het wel even.

**Niet en passant de voortgangsindicator herbouwen.** Dit gaat over wat er in een regel staat, niet over hoe het blok eruitziet.
