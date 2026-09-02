# Een taak weet welke deployments hij raakt

Elke taak draagt de deployments die hij raakt in een kolom, `async_tasks.affects_deployments`.
Twee taken van hetzelfde project waarvan die scopes elkaar overlappen draaien niet meer
tegelijk, en de ArgoCD-wacht meldt niet langer een time-out voor een deployment die
ondertussen door een andere taak is verwijderd.

## Het probleem

Op 31 augustus 2026 gaf `mpfb-8wh` twee keer binnen een half uur een uitrolfout die geen
uitrolfout was:

| tijd (CEST) | taak | wat |
|---|---|---|
| 09:21:10 | `8fc9553b` | aangemaakt: `configure_service` voor `mpfb-8wh/None`, dus projectbreed |
| 09:21:33 | `6c502a06` | aangemaakt: `delete_deployment` voor `mpfb-8wh/pr-244` |
| 09:21:38 | `6c502a06` | geclaimd, naast de al lopende `8fc9553b` |
| 09:21:42 | `6c502a06` | verwijdert de Application uit de argo-repo |
| 09:22:02 | `8fc9553b` | begint te wachten op `mpfb-8wh-pr-244`, timeout 300s |
| 09:22:16 | `8fc9553b` | ArgoCD antwoordt: de Application bestaat niet meer |
| 09:27:28 | `8fc9553b` | `Timed out after 300s waiting for sync`, taak mislukt |

Om 09:51 herhaalde zich hetzelfde met `pr-247`. Het is geen zeldzame samenloop maar de
standaardvorm waarin een CI-pijplijn een PR afsluit: eerst een dienst configureren, dan de
PR-deployment opruimen.

Diezelfde nacht gaf `asses-k2n/pr-537` dezelfde melding om een heel andere reden. De
Application stond de volle 300 seconden op `health=Progressing`, de `resourceVersion` stond
285 van die 300 seconden stil, en de application-controller logde in die vijf minuten geen
regel over deze app terwijl hij voor andere apps 5 tot 77 reconciles per minuut deed.
ArgoCD herbeoordeelt een app alleen op een watch-event van een beheerde resource, of anders
na `timeout.reconciliation` - op productie 15 minuten. Wij wachtten 300 seconden onder een
hertoetsing van 900, dus elk gemist watch-event werd automatisch een gebruikerszichtbare fout.

## Deel A: de wacht liegt niet meer over wat hij ziet

### Weg is niet hetzelfde als onbereikbaar

`ArgoConnector.get_application_status()` geeft `None` voor zowel 404 als 403, en documenteert
dat als "de applicatie bestaat niet". `wait_for_application_synced()` las datzelfde `None` als
een tijdelijke leesfout en polde door tot de timeout vol was: een verdwenen app en een
onbereikbare ArgoCD zagen er identiek uit.

De lus houdt nu bij of de applicatie in **deze** wacht ooit met succes is uitgelezen. Is dat zo
en komt er daarna een leeg antwoord, dan volgt `ApplicationGone` (een `RuntimeError`, dus de
bestaande `except RuntimeError, TimeoutError: raise` laat hem door). Is de app nog nooit gezien,
dan is doorpollen juist goed: hij moet nog verschijnen.

`project_manager` vangt die uitzondering vóór `except RuntimeError` en geeft de deployment terug
als `status: "removed"`. In de verwerking van de uitkomsten krijgt `removed` dezelfde behandeling
als `ok`: geen `sync_failures`, geen `health_warnings`. De substap wordt afgerond met
"verwijderd tijdens de uitrol" en niet gefaald - voor de gebruiker is dit geen probleem maar het
gevolg van iets dat hij zelf startte.

### Een bevroren status trekken we zelf los

Staat de status 60 seconden op `Progressing`, dan vraagt de wachtlus zelf opnieuw een refresh
(`HERVERVERS_ELKE_SECONDEN`). De teruggegeven `reconciledAt` gaat mee in `refreshed_after`, want
dat is de drempel die bepaalt of een status vers genoeg is om er terminale toestanden op te
baseren; zonder bijwerken zou een oude status alsnog als vers tellen. Alleen op `Progressing`:
`Degraded` heeft verderop zijn eigen afhandeling en die moet niet uitgesteld worden.

De clusterbrede knop `timeout.reconciliation` in `argocd-cm` blijft op 15 minuten staan. Die
lager zetten zou werken, maar laat élke Application vaker hertoetsen voor een probleem dat wij
in één wachtlus kunnen oplossen - dit raakt alleen de apps waar we daadwerkelijk op wachten.

`wait_for_infrastructure_ready()` heeft dezelfde lus met hetzelfde gat, maar wacht op
`<project>-infrastructure`, die niemand tussentijds verwijdert. Die is bewust ongemoeid gelaten.

## Deel B: de scope van een taak is een kolom

### Waarom een kolom en geen derde afleiding

Er zaten drie coördinatiemechanismen in de wachtrij, elk met een eigen idee van "scope":

| mechanisme | sleutel | wat het deed |
|---|---|---|
| claim-guard | `(project_name, deployment_name)` letterlijk, NULL-safe | claimt geen taak zolang er een andere met dezelfde sleutel loopt |
| conflict-check | `(project_name, task_type)` | logt een waarschuwing, blokkeert niets |
| supersede | `scope_of()`: projectbreed is `None`, anders een set namen | laat een ArgoCD-wacht wijken voor een nieuwere taak die alles overdoet |

De claim-guard deed al precies het goede, maar op de verkeerde sleutel:

```
taak A: configure_service  mpfb-8wh / NULL      projectbreed
taak B: delete_deployment  mpfb-8wh / 'pr-244'
NULL IS NOT DISTINCT FROM 'pr-244'  ->  false  ->  geen conflict  ->  allebei claimen
```

`scope_of()` wist wél dat een lege `deployment_name` projectbreed betekent. Die kennis stond er,
alleen las de claim-guard hem niet. Dat is het eigenlijke defect: twee begrippen van scope die
het oneens zijn over dezelfde twee taken. Er een derde bij bouwen in SQL zou er drie maken, dus
schrijft `create_task()` het antwoord van `scope_of()` weg in `affects_deployments` en lezen alle
controles die kolom. Dat maakt de scope bovendien inspecteerbaar: aan een taakrij is te zien
welke deployments hij claimt te raken.

`NULL` betekent projectbreed, net als `None` in `scope_of()`. Rijen van vóór migratie 005 blijven
`NULL` en zijn daarmee maximaal blokkerend - de veilige kant voor de handvol taken die tijdens
een upgrade openstaan.

### Claimen kijkt naar overlap, superseden naar superset

Dat lijkt inconsistent maar is het niet: twee verschillende vragen op dezelfde kolom.

- **Claimen** vraagt "kunnen deze twee elkaar in de weg zitten", en dat is **overlap**
  (`_scopes_overlap`, met de Postgres-operator `&&` op de GIN-index). NULL overlapt met alles,
  ook met een andere NULL.
- **Superseden** vraagt "doet die nieuwere taak alles over waar ik op wacht", en dat is
  **superset** (`covers`, zie `features/task-supersede.md`). Een smallere taak neemt een bredere
  niet over: dat zou de andere deployments ongesyncet achterlaten.

`{pr-244}` en `{pr-250}` overlappen niet, dus die lopen nog steeds naast elkaar. Dat is geen
bijvangst maar een eis, en er ligt een test op.

### Eerst binnen, eerst gedraaid - per project

Alleen "niet claimen zolang er iets overlappends loopt" is niet genoeg. Claimen gaat op
`created_at ASC` en slaat geblokkeerde taken over, dus in een druk PR-project kan een stroom
smalle taken steeds vóór een projectbrede taak blijven springen. `claim_next_task` heeft daarom
een tweede clausule: een oudere wachtende taak met overlappende scope gaat voor.

De vergelijking is op `(created_at, id)` en niet op `created_at` alleen. Twee taken kunnen
dezelfde tijdstempel dragen, en dan is er zonder tweede sleutel geen totale ordening en kunnen
ze elkaar wederzijds blokkeren. Een deadlock kan niet ontstaan: een taak wordt alleen door een
**oudere** geblokkeerd, en de oudste wachtende taak van een project heeft per definitie niemand
vóór zich.

**Backup en restore doen niet mee aan die volgorde** (`_UNORDERED_TASK_TYPES`). Ze zijn wereldwijd
afgeknepen op `BACKUP_MAX_CONCURRENT`, en de nachtelijke sweep zet er tientallen tegelijk in de
wachtrij; zou een wachtende backup als blokkeerder meetellen, dan legt een limiet die niets met
dít project te maken heeft de hele wachtrij van dat project stil. Ze blokkeren wél zolang ze
draaien: je verwijdert geen deployment terwijl zijn backup draait.

### Wachten is de goede prijs

De taakduur over 7 uur productielogboek: 31 taken, minimaal 22s, mediaan 56s, p90 86s, maximaal
243s. Een delete die achter een projectbrede taak wacht kost dus typisch onder de minuut. Daar
staat een fout tegenover die in deze vorm honderd procent van de keren optrad.

### Een wachtende taak zegt waarop hij wacht

Zonder dit lijkt een geblokkeerde taak gewoon te hangen, en dat is precies wat we niet willen
ruilen voor de fout die we weghalen. `GET /api/tasks/{id}` van een `pending` taak draagt daarom
`waiting_for`:

```json
{
  "status": "pending",
  "current_step": "Wacht op delete_deployment (pr-244)",
  "waiting_for": {
    "task_id": "6c502a06-...",
    "task_type": "delete_deployment",
    "deployment_name": "pr-244",
    "reason": "running"
  }
}
```

`reason` is `running` als de blokkerende taak bezig is, en `queued_ahead` als die zelf ook nog
wacht maar eerder werd aangemaakt. Het veld staat er **altijd**, met `null` als er niets is - een
sleutel die soms ontbreekt dwingt elke lezer tot een extra controle, dezelfde reden als bij
`superseded_by` en `pending_rollout`.

`find_blocking_task()` berekent dat op leesmoment en schrijft niets op de rij: de wachtrij
verandert continu, en een opgeslagen reden zou vrijwel altijd verouderd zijn. `current_step` is
een afleiding in `task_response_from_dict()`; de kolom houdt zijn `Queued`.

## Migratie

`opi/migrations/versions/005_add_affects_deployments.py` voegt de kolom en een GIN-index toe.
De kolom is nullable en additief, dus een oude OPI-versie draait op de nieuwe tabel zonder te
breken. Andersom werkt niet: een nieuwe OPI op een niet-gemigreerde database verwijst in
`claim_next_task` naar een kolom die er niet is. Alembic draait bij het opstarten, dus dat lost
zichzelf op, maar het is de reden om deze versie niet naast een oude te laten draaien tijdens een
rollende update.

`ASYNC_TASKS_TABLE_SQL` is meegewijzigd, zodat een verse database dezelfde tabel krijgt als een
gemigreerde.

## Wat hier bewust niet in zit

- **Per-wacht supersede.** Eleganter zou zijn dat een lopende projectbrede taak een deployment uit
  zijn eigen wachtlijst haalt zodra er een delete voor klaarstaat, in plaats van dat de delete
  achteraan sluit. Dat is nieuwe machinerie voor een blokkade die volgens de meting meestal onder
  de minuut duurt: eerst dit laten draaien en dan meten. De scope in een kolom maakt dit later een
  kleine toevoeging in plaats van een herontwerp.
- **Voorrang voor een delete.** Zelfde afweging, en het heeft pas zin als per-wacht supersede er is.
- **De conflict-check in `task_worker.py`.** Die logt een waarschuwing op `(project, task_type)` en
  wordt hierdoor grotendeels overbodig. Hem opruimen is een aparte, veilige schoonmaak en hoort
  niet in een wijziging aan de wachtrij zelf.

## Bestanden

| Bestand | Wat |
|---|---|
| `opi/manager/argo_manager.py` | `ApplicationGone`, de `seen`-vlag, en de herverversing op `Progressing` |
| `opi/manager/project_manager.py` | de uitkomst `removed` en de verwerking daarvan |
| `opi/migrations/versions/005_add_affects_deployments.py` | de kolom en de GIN-index |
| `opi/core/async_task_schema.py` | dezelfde tabel voor een verse database |
| `opi/services/persistence/async_tasks.py` | de kolom in het model (postgresql-`ARRAY`, want alleen die heeft `.overlap()`) |
| `opi/core/async_task_service.py` | `scope_of()` bij het aanmaken, `_scopes_overlap()`, de twee claim-clausules, `find_blocking_task()` |
| `opi/core/task_supersede.py` | `covers()` leest de kolom in plaats van opnieuw af te leiden |
| `opi/core/task_worker.py` | de scope van de lopende taak komt uit de kolom |
| `opi/api/task_models.py`, `opi/api/task_router.py` | `waiting_for` en de afgeleide `current_step` |

## Tests

- `tests/test_argo_manager.py` - een verdwenen app levert `ApplicationGone` binnen één
  pollinterval; een app die nog moet verschijnen niet; de refresh na 60s Progressing en het
  meebewegen van `refreshed_after`; geen refresh op `Degraded`.
- `tests/test_verdwenen_applicatie_tijdens_wacht.py` - de mpfb-situatie tot in de uitkomst:
  `removed` levert nul sync-fouten op, en een gewone `RuntimeError` faalt nog steeds.
- `tests/test_taakscope_overlap.py` - de overlapmatrix in beide richtingen, het incident zelf, de
  parallelliteit die moet blijven, de volgorde per project met de backup-uitzondering, wat
  `create_task` per taaktype wegschrijft, rijen van vóór de migratie, en `find_blocking_task()`.
- `tests/test_task_router.py` - het API-antwoord van een geblokkeerde en een vrije pending taak.

## Zie ook

- `features/task-supersede.md` - het superset-predicaat op dezelfde kolom.
- `features/async-task-system.md` - de wachtrij als geheel.
