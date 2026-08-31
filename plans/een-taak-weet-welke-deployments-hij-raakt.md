# Een taak weet welke deployments hij raakt

Op 31 augustus 2026 gaf `mpfb-8wh` twee keer binnen een half uur een uitrolfout die geen uitrolfout was: `timed out after 300s waiting for sync`, terwijl de Application waar op gewacht werd al lang verwijderd was door een andere taak van hetzelfde project. Diezelfde nacht gaf `asses-k2n/pr-537` dezelfde melding om een heel andere reden: de ArgoCD-status bevroor op `Progressing` en wij wachten korter dan ArgoCD zichzelf hertoetst.

Twee oorzaken, één melding. Dit plan repareert allebei, en scheidt ze in twee delen zodat ze los te reviewen zijn. Deel A zorgt dat de wachtlus niet meer liegt over wat hij ziet. Deel B zorgt dat twee taken op hetzelfde project niet meer op elkaar stappen, door de scope van een taak op te slaan in plaats van hem op twee plekken verschillend af te leiden.

Beide delen horen in één PR, want ze raken allebei `wait_for_application_synced()` en zouden elkaar anders in de weg zitten.

## Wat er gebeurde

### mpfb-8wh: twee taken op één project

Uit de OPI-logs van 31 augustus, tijden in CEST:

| tijd | taak | wat |
|---|---|---|
| 09:21:10 | `8fc9553b` | aangemaakt: `configure_service` voor `mpfb-8wh/None`, dus projectbreed |
| 09:21:33 | `6c502a06` | aangemaakt: `delete_deployment` voor `mpfb-8wh/pr-244` |
| 09:21:38 | `6c502a06` | geclaimd, naast de al lopende `8fc9553b` |
| 09:21:42 | `6c502a06` | verwijdert `mpfb-8wh-pr-244-argocd-application.yaml` uit de argo-repo |
| 09:22:02 | `8fc9553b` | begint te wachten op `mpfb-8wh-pr-244`, timeout 300s |
| 09:22:16 | `8fc9553b` | ArgoCD antwoordt `403 permission denied`: de Application bestaat niet meer |
| 09:22:20 | `6c502a06` | bevestigt verwijderd, dropt de databases, rondt succesvol af |
| 09:27:28 | `8fc9553b` | `Timed out after 300s waiting for sync`, taak mislukt |

Tussen 09:22:16 en 09:27:28 heeft de wachtlus 144 keer een niet-bestaande Application opgevraagd en elke keer `Could not get status, retrying` gelogd. Om 09:51 herhaalde zich precies hetzelfde met `pr-247`: delete geclaimd om 09:51:32, wacht blind vanaf 09:52:11, timeout om 09:57:14.

Het is dus geen zeldzame samenloop. Het is de standaardvorm waarin een CI-pijplijn een PR afsluit: eerst een dienst configureren, dan de PR-deployment opruimen.

### asses-k2n/pr-537: een bevroren status

Op 30 augustus 21:07 tot 21:12 CEST, tijdens een sleep-mode wake (`req-82b04d7e`):

- De Application stond de volle 300s op `sync=Synced, health=Progressing, fresh=True`.
- De `resourceVersion` van het Application-object stond 285 van de 300 seconden stil op `2413972417`.
- De application-controller logde tussen 19:07:10 en 19:12:20 UTC geen enkele regel over deze app, terwijl hij in diezelfde minuten 5 tot 77 reconciles per minuut deed voor andere apps.
- Onze eigen podcontrole bleef `no issues detected in rig-prd-asses-k2n` melden, en de app staat inmiddels gewoon op `Synced/Healthy`.

De oorzaak zit in de ArgoCD-configuratie: `argocd-cm` heeft `timeout.reconciliation = 15m`. ArgoCD herbeoordeelt een app alleen op een watch-event van een beheerde resource, of anders pas na een kwartier. Het laatste event kwam binnen toen de Deployments net van 0 naar 1 schaalden en de health terecht `Progressing` was. Het vervolg-event is niet aangekomen of samengevoegd, en daarna zat de health vast tot de volgende resync. Wij wachten 300 seconden onder een hertoetsing van 900 seconden, dus elk gemist watch-event wordt automatisch een gebruikerszichtbare fout.

## Wat er nu gebeurt

Er zitten drie onafhankelijke coördinatiemechanismen in de taakwachtrij, met elk hun eigen idee van "scope":

| mechanisme | plek | sleutel | wat het doet |
|---|---|---|---|
| claim-guard | `opi/core/async_task_service.py:206-217` | `(project_name, deployment_name)` letterlijk, NULL-safe | claimt een pending taak niet zolang er een andere actieve taak met dezelfde sleutel loopt |
| conflict-check | `opi/core/async_task_service.py:505` | `(project_name, task_type)` | logt een waarschuwing in `task_worker.py:230`, blokkeert niets |
| supersede | `opi/core/task_supersede.py:80` | `scope_of()`: projectbreed is `None`, anders een set namen | laat een ArgoCD-wacht wijken voor een nieuwere taak die alles overdoet |

De claim-guard doet al precies wat er nodig is, alleen op de verkeerde sleutel:

```
taak A: configure_service  mpfb-8wh / NULL      projectbreed
taak B: delete_deployment  mpfb-8wh / 'pr-244'
NULL IS NOT DISTINCT FROM 'pr-244'  ->  false  ->  geen conflict  ->  allebei claimen
```

En `scope_of()` in `task_supersede.py` weet wél dat een lege `deployment_name` projectbreed betekent. Die kennis staat er, alleen leest de claim-guard hem niet. Dat is het eigenlijke defect: twee begrippen van scope die het oneens zijn over dezelfde twee taken.

Dit is bovendien geen nieuwe ontdekking. Het plan van RC-164 (`plans/een-overgenomen-taak-zegt-wie-het-overnam.md`) beschrijft de asymmetrie al woordelijk: "projectbrede taken dragen geen deployment_name, dus zij en een deployment-gerichte taak zien elkaar nooit als in-flight en lopen tegelijk". Toen was het een randopmerking bij een ander probleem. Nu is het twee keer in dertig minuten een mislukte uitrol geworden.

Supersede greep hier terecht niet in. `covers()` eist dat de nieuwere taak een superset is, en `{pr-244}` is smaller dan projectbreed. Een smallere taak een bredere laten overnemen zou de andere drie deployments ongesyncet achterlaten. Die regel blijft zoals hij is.

De derde plek waar het misgaat is de wachtlus zelf. `ArgoConnector.get_application_status()` op `opi/connectors/argo.py:345` documenteert `None` als "de applicatie bestaat niet", voor zowel 404 als 403. Maar `wait_for_application_synced()` op `opi/manager/argo_manager.py:1142` leest datzelfde `None` als een tijdelijke leesfout en polt door tot de timeout vol is. Een verdwenen app en een onbereikbare ArgoCD zien er voor die lus identiek uit.

## Het besluit

Zes keuzes, met de reden erbij.

**De scope van een taak wordt een opgeslagen kolom, niet een afleiding.** Er zijn nu twee afleidingen die het oneens zijn, en een derde in SQL bouwen zou er drie maken. `scope_of()` wordt de enige schrijver, de kolom de enige lezer. Dat maakt de scope bovendien inspecteerbaar: je kunt aan een taakrij zien welke deployments hij claimt te raken, in plaats van dat uit een tabel met tasktypes te moeten reconstrueren.

**Claimen kijkt naar overlap, superseden naar superset.** Dat lijkt inconsistent maar is het niet: het zijn twee verschillende vragen op dezelfde kolom. Claimen vraagt "kunnen deze twee elkaar in de weg zitten", en dat is overlap. Superseden vraagt "doet die nieuwere taak alles over waar ik op wacht", en dat is superset. Eén kolom, twee predicaten.

**Wachten is de goede prijs.** De taakduur over 7 uur productielogboek: 31 taken, minimaal 22s, mediaan 56s, p90 86s, maximaal 243s. Een delete die achter een projectbrede taak wacht kost dus typisch onder de minuut. Daar staat een fout tegenover die in deze vorm nu honderd procent van de keren optreedt. Serialiseren is hier niet de luie oplossing maar de juiste.

**De volgorde is eerst binnen, eerst gedraaid, per project.** Alleen "niet claimen zolang er iets overlappends loopt" is niet genoeg: claimen gaat op `created_at ASC` en slaat geblokkeerde taken over, dus in een druk PR-project kan een stroom smalle taken steeds vóór een projectbrede taak blijven springen. Die kan dan willekeurig lang wachten. Met FIFO per project is de wachttijd uit te leggen in plaats van van het toeval afhankelijk.

**Backup en restore doen niet mee aan die volgorde.** Ze zijn wereldwijd afgeknepen op `BACKUP_MAX_CONCURRENT = 2` (`opi/core/config.py:531`), en de nachtelijke sweep zet er tientallen tegelijk in de wachtrij. Zou een wachtende backup meetellen als blokkeerder, dan legt een limiet die niets met dít project te maken heeft de hele wachtrij van dat project stil. Ze blijven wel meedoen aan de in-flight-blokkade: je verwijdert geen deployment terwijl zijn backup draait.

**De bevroren status trekken we zelf los, we draaien niet aan `timeout.reconciliation`.** Die knop van 15 minuten naar bijvoorbeeld 3 zetten zou werken, maar het is een clusterbrede instelling die elke Application vaker laat hertoetsen, voor een probleem dat wij in één wachtlus kunnen oplossen door tijdens het wachten opnieuw een refresh te vragen. Goedkoper, en het raakt alleen de apps waar we daadwerkelijk op staan te wachten.

## Deel A: de ArgoCD-wacht liegt niet meer

### A1. De wacht onderscheidt "weg" van "onbereikbaar"

In `opi/manager/argo_manager.py`, in `wait_for_application_synced()`. Voeg een uitzondering toe naast de bestaande:

```python
class ApplicationGone(RuntimeError):
    """De Application bestond tijdens deze wacht en is er nu niet meer.

    Geen mislukking van de app zelf: iets anders heeft hem verwijderd, meestal een
    delete_deployment voor dezelfde deployment. Doorwachten heeft geen zin, want er
    komt niets meer terug om op te wachten.
    """
```

De lus houdt bij of de app in déze wacht ooit met succes is uitgelezen, en gooit alleen dan:

```python
seen = False
...
status_data = await argo_connector.get_application_status(app_name)
if not status_data:
    if seen:
        raise ApplicationGone(
            f"Application '{app_name}' verdween tijdens het wachten "
            f"(na {elapsed_time}s); een andere taak heeft hem waarschijnlijk verwijderd"
        )
    logger.debug(f"Could not get status for '{app_name}', retrying...")
    await asyncio.sleep(poll_interval)
    elapsed_time += poll_interval
    continue
seen = True
```

Waarom `seen` en niet meteen gooien bij het eerste lege antwoord: aan het begin van de wacht kan de Application er nog net niet zijn, en dan is doorpollen wél goed. Die eerste fase heeft trouwens al zijn eigen functie (`wait_for_application_created()`, `opi/manager/argo_manager.py:881`, via `application_exists()`), dus in de praktijk is de app hier bijna altijd meteen zichtbaar. `seen` maakt dat expliciet in plaats van er op te vertrouwen.

Let op het bestaande `except RuntimeError, TimeoutError: raise` op `opi/manager/argo_manager.py:1222`. `ApplicationGone` erft van `RuntimeError` en propageert daardoor vanzelf goed. Verander die regel niet.

`wait_for_infrastructure_ready()` op `opi/manager/argo_manager.py:970` heeft dezelfde lus met hetzelfde gat, maar die wacht op `<project>-infrastructure`, die niemand tussentijds verwijdert. Laat die staan; wij repareren waar het aantoonbaar misgaat.

### A2. Een verdwenen app is geen synchronisatiefout

In `opi/manager/project_manager.py`, in `_refresh_and_wait()` rond `:3416`. Er is nu `ok`, `health_error`, `timeout` en `error`. Voeg `removed` toe:

```python
except ApplicationGone as e:
    logger.info("Application '%s' (%s) verdween tijdens de wacht: %s", app_name, dep_name, e)
    if app_subtask:
        progress_manager.update_task(app_subtask, dep_name, subject="verwijderd tijdens de uitrol")
        progress_manager.complete_subtask(app_subtask)
    return {"app_name": app_name, "dep_name": dep_name, "status": "removed"}
```

Vang hem vóór `except RuntimeError`, anders slikt die hem op als sync-fout.

In de lus die de uitkomsten verwerkt (`opi/manager/project_manager.py:3449`) krijgt `removed` dezelfde behandeling als `ok`: `continue`, dus geen `sync_failures`, geen `health_warnings`. De deployment bestaat niet meer, dus er valt niets over te melden.

De subtaak wordt afgerond en niet gefaald. Voor de gebruiker is dit geen probleem maar een gevolg van iets dat hij zelf startte.

### A3. De wacht trekt een bevroren status zelf los

In dezelfde lus in `wait_for_application_synced()`. Vraag opnieuw een refresh als de status al een tijd niet beweegt:

```python
REFRESH_EVERY = 60  # seconden

if elapsed_time and elapsed_time % REFRESH_EVERY == 0 and health_status == "Progressing":
    logger.info(
        "Application '%s' staat %ds op Progressing; opnieuw een refresh gevraagd "
        "(ArgoCD hertoetst zelf pas na timeout.reconciliation)",
        app_name,
        elapsed_time,
    )
    refreshed_after = await argo_connector.refresh_application(app_name)
```

Twee dingen die hierbij horen en makkelijk vergeten worden:

`refreshed_after` moet meebewegen. Die waarde bepaalt via `reconciledAt` of de status vers genoeg is om terminale toestanden op te baseren (`opi/manager/argo_manager.py:1160-1168`). Zet je hem niet bij, dan blijft de lus de oude drempel gebruiken en telt een status als vers die dat niet is.

Alleen op `Progressing`, niet op `Degraded`. Een degraded app heeft een eigen afhandeling verderop in de lus, en die niet in de weg lopen.

De poll staat op 2 seconden, dus `elapsed_time % 60` valt netjes samen met de klok van de lus. Als die aanname ooit wijzigt is een expliciete `next_refresh_at`-teller beter; nu zou dat extra staat zijn voor niets.

### A4. Verificatie deel A

Nieuwe tests bij de bestaande ArgoCD-managertests:

1. Een gefaket antwoordreeks die eerst `Progressing` geeft en daarna `None`, levert `ApplicationGone` op, binnen één pollinterval, niet na 300s.
2. Dezelfde reeks die meteen met `None` begint en daarna gaat leveren, levert géén `ApplicationGone` op: de app moest nog verschijnen.
3. `_refresh_and_wait()` met een `ApplicationGone` geeft `status: "removed"` terug, en de aanroepende verwerking komt uit op nul `sync_failures`. Dit is de assertie die de mpfb-situatie vasthoudt.
4. Een status die op `Progressing` blijft staan leidt tot een tweede `refresh_application()`-aanroep na 60s, en `refreshed_after` is daarna de nieuwe waarde. Dit is de assertie die de asses-k2n-situatie vasthoudt.

## Deel B: taken op één project stappen niet meer op elkaar

### B1. Migratie 005: de scope als kolom

`opi/migrations/versions/005_add_affects_deployments.py`, in de vorm van `004_add_runs.py`:

```python
def upgrade() -> None:
    op.execute("ALTER TABLE async_tasks ADD COLUMN IF NOT EXISTS affects_deployments VARCHAR(63)[];")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_async_tasks_affects "
        "ON async_tasks USING GIN (affects_deployments);"
    )

def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_async_tasks_affects;")
    op.execute("ALTER TABLE async_tasks DROP COLUMN IF EXISTS affects_deployments;")
```

`NULL` betekent projectbreed, net als `None` in `scope_of()`. Bestaande rijen blijven `NULL` en zijn daarmee maximaal blokkerend, wat de veilige kant is voor de handvol taken die tijdens een upgrade openstaan.

Werk `ASYNC_TASKS_TABLE_SQL` in `opi/core/async_task_schema.py` bij, zodat een verse database dezelfde tabel krijgt als een gemigreerde. Die constante wordt door de baseline-migratie gebruikt en is de schema-waarheid voor nieuwe installaties.

### B2. Het model

In `opi/services/persistence/async_tasks.py`, naast de bestaande kolommen. Let op welke `ARRAY` je pakt, want dat is niet de vanzelfsprekende:

```python
from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY, JSONB

affects_deployments: Mapped[list[str] | None] = mapped_column(PG_ARRAY(String(63)))
```

Het bestand importeert `ARRAY` nu uit `sqlalchemy` voor de kolom `logs`. Die generieke variant heeft geen `.overlap()` in zijn comparator, alleen de postgresql-variant heeft dat (gecontroleerd op SQLAlchemy 2.0.46). Zonder de dialect-import valt B4 om op een `AttributeError` bij het bouwen van de query, en dat is een fout die pas bij het opstarten zichtbaar wordt. Laat `logs` staan zoals hij is; alleen de nieuwe kolom heeft de dialect-variant nodig.

En de index bij `__table_args__`:

```python
Index("idx_async_tasks_affects", "affects_deployments", postgresql_using="gin"),
```

`to_dict()` loopt over `self.__table__.columns` en pikt de kolom vanzelf mee.

### B3. `create_task` vult hem, als enige

In `opi/core/async_task_service.py:106`. Roep `scope_of()` aan en sla het resultaat op:

```python
from opi.core.task_supersede import scope_of

scope = scope_of(task_type, deployment_name, payload)
...
affects_deployments=None if scope is None else sorted(scope),
```

`sorted()` omdat een `frozenset` geen vaste volgorde heeft en een stabiele kolomwaarde makkelijker te lezen en te vergelijken is.

Let op de deduplicatiecontrole die er direct boven staat (`:125-136`): die matcht op `project_name`, `deployment_name` en `task_type` en blijft ongewijzigd. De nieuwe kolom is afgeleid van precies die velden plus de payload, dus hij voegt aan dedup niets toe.

### B4. `claim_next_task` kijkt naar overlap en naar volgorde

In `opi/core/async_task_service.py:203-236`. De bestaande `inflight`-subquery met `is_not_distinct_from` vervalt en wordt vervangen door twee subquery's.

Een hulpfunctie voor het overlappredicaat, zodat het maar op één plek staat en punt B6 hem kan hergebruiken:

```python
def _scopes_overlap(a, b):
    """Twee taken kunnen elkaar in de weg zitten.

    NULL is projectbreed en overlapt met alles, inclusief met een andere NULL. Twee
    concrete scopes overlappen als ze een deploymentnaam delen; ``&&`` is de
    array-overlapoperator van Postgres en gebruikt de GIN-index.
    """
    return or_(
        a.affects_deployments.is_(None),
        b.affects_deployments.is_(None),
        a.affects_deployments.overlap(b.affects_deployments),
    )
```

Clausule 1, in-flight overlap. Dit blokkeert altijd, ongeacht tasktype en ongeacht wie ouder is:

```python
running = aliased(AsyncTask)
inflight = (
    select(1)
    .select_from(running)
    .where(
        running.project_name == AsyncTask.project_name,
        running.status.in_(_ACTIVE_STATES),
        running.id != AsyncTask.id,
        _scopes_overlap(running, AsyncTask),
    )
    .exists()
)
```

Clausule 2, volgorde binnen het project. Een oudere wachtende taak met overlappende scope gaat voor:

```python
earlier = aliased(AsyncTask)
queued_ahead = (
    select(1)
    .select_from(earlier)
    .where(
        earlier.project_name == AsyncTask.project_name,
        earlier.status == "pending",
        earlier.task_type.notin_(_UNORDERED_TASK_TYPES),
        tuple_(earlier.created_at, earlier.id) < tuple_(AsyncTask.created_at, AsyncTask.id),
        _scopes_overlap(earlier, AsyncTask),
    )
    .exists()
)

stmt = select(AsyncTask.id).where(
    AsyncTask.status == "pending",
    AsyncTask.cluster == cluster,
    ~inflight,
    ~queued_ahead,
)
```

Met daarbij, naast `_ACTIVE_STATES` bovenaan de module:

```python
# Taaktypes die niet meedoen aan de volgorde binnen een project, omdat ze wereldwijd
# zijn afgeknepen (BACKUP_MAX_CONCURRENT) en dus lang kunnen blijven staan om een reden
# die niets met dit project te maken heeft. Ze blokkeren wél zolang ze draaien.
_UNORDERED_TASK_TYPES = ("backup", "restore")
```

Drie dingen die hier bewust zo staan:

De vergelijking is op `(created_at, id)` en niet op `created_at` alleen. Twee taken kunnen dezelfde tijdstempel dragen, en dan is er zonder tweede sleutel geen totale ordening en kunnen ze elkaar wederzijds blokkeren. `tuple_()` uit `sqlalchemy` vertaalt naar de rijvergelijking `(a.created_at, a.id) < (t.created_at, t.id)`, die Postgres links naar rechts evalueert. Beide constructies zijn tegen de postgresql-dialect gecompileerd en leveren `a.sc && t.sc` respectievelijk de rijvergelijking op.

Er is geen deadlock mogelijk. Een taak wordt alleen geblokkeerd door een oudere, en de oudste wachtende taak van een project heeft per definitie niemand vóór zich. De keten loopt dus altijd ergens op leeg.

De parallelliteit die we vandaag hebben blijft: `{pr-244}` en `{pr-250}` overlappen niet, dus die lopen nog steeds naast elkaar. Dat is geen bijvangst maar een eis, en punt B7 legt er een test op.

Verwijder ook de bestaande commentaarregel op `:204-205` die de oude sleutel beschrijft, en werk de docstring van `claim_next_task` bij: die zegt nu "same project/deployment" en dat is straks niet meer waar.

### B5. `covers()` leest dezelfde kolom

In `opi/core/task_supersede.py`. `find_superseding_task()` roept nu voor elke kandidaat `scope_of()` opnieuw aan met `task_type`, `deployment_name` en `payload` uit de taakrij. Die rij draagt straks `affects_deployments`, dus lees die:

```python
for candidate in candidates:
    stored = candidate.get("affects_deployments")
    candidate_scope = None if stored is None else frozenset(stored)
    if covers(candidate_scope, current.scope):
        return candidate
```

Zo draait `scope_of()` nog maar op één moment in het leven van een taak: bij het aanmaken.

Voor de lopende taak zelf zet `task_worker.py:271` de scope via `scope_of()` in `RunningTask`. Lees die ook uit de kolom van de geclaimde taak, om dezelfde reden. Valt de kolom `None` uit, dan is dat projectbreed, en dat is precies wat een taak van vóór de migratie moet krijgen.

`scope_of()` en `_PROJECT_WIDE_TASK_TYPES` blijven bestaan en behouden hun docstrings: ze zijn nu de definitie die bij het aanmaken gebruikt wordt, in plaats van bij elke controle opnieuw.

### B6. Een wachtende taak zegt waarop hij wacht

Zonder dit lijkt een geblokkeerde taak gewoon te hangen, en dat is precies wat we niet willen ruilen voor de fout die we weghalen. Iemand die een delete start terwijl er een projectbrede taak loopt, moet kunnen zien dat er niets stuk is.

Een opzoekfunctie in `AsyncTaskService`, met hetzelfde overlappredicaat als B4:

```python
async def find_blocking_task(self, task_id: str) -> dict | None:
    """De taak waardoor deze pending taak nog niet geclaimd is, of None.

    Dezelfde twee redenen als in claim_next_task, in dezelfde volgorde: een
    overlappende taak die draait, anders een oudere overlappende taak die wacht.
    Op leesmoment berekend en niet op de rij geschreven: de wachtrij verandert
    continu, en een opgeslagen reden zou vrijwel altijd verouderd zijn.
    """
```

En de aansluiting in `opi/api/task_router.py:212`, naast de bestaande `pending_rollout`-verrijking:

```python
if status == "pending":
    blocker = await task_service.find_blocking_task(task_id)
    if blocker:
        response_body["waiting_for"] = {
            "task_id": str(blocker["task_id"]),
            "task_type": blocker["task_type"],
            "deployment_name": blocker.get("deployment_name"),
            "reason": "running" if blocker["status"] in ("claimed", "running") else "queued_ahead",
        }
```

In `TaskResponse` (`opi/api/task_models.py:445`) komt het veld erbij, in de vorm van `superseded_by`: altijd aanwezig, `null` als er niets is. Een sleutel die soms ontbreekt dwingt elke lezer tot een extra controle, en dat is precies de reden die daar al bij `superseded_by` en `pending_rollout` staat.

```python
waiting_for: WaitingForResponse | None = Field(
    default=None,
    description=(
        "Waarom deze taak nog wacht, zolang zijn status 'pending' is. Null zodra hij "
        "draait of klaar is. 'reason' is 'running' als de blokkerende taak bezig is, "
        "en 'queued_ahead' als die zelf ook nog wacht maar eerder werd aangemaakt."
    ),
)
```

Zet daarnaast `current_step` van de wachtende taak in het antwoord op iets leesbaars, want dat is het veld dat het portaal en de CLI al tonen. Bijvoorbeeld `Wacht op delete_deployment (pr-244)`. Dat is een afleiding in `task_response_from_dict()` op basis van `waiting_for`, geen tweede schrijfactie op de rij: de kolom houdt zijn `Queued`.

### B7. Verificatie deel B

Nieuwe tests bij de bestaande taakwachtrij-tests:

1. De overlapmatrix als tabeltest: projectbreed tegen projectbreed, projectbreed tegen `{a}`, `{a}` tegen `{a}`, `{a}` tegen `{b}`, `{a,b}` tegen `{b,c}`. Beide richtingen, want overlap hoort symmetrisch te zijn en dat is de fout die de huidige code maakt.
2. De mpfb-situatie: een projectbrede taak draait, een `delete_deployment` voor één deployment van hetzelfde project blijft `pending` en wordt niet geclaimd. Dit is de assertie die het incident vasthoudt.
3. De parallelliteit die moet blijven: twee taken voor `pr-244` en `pr-250` van hetzelfde project worden allebei geclaimd.
4. FIFO: een oudere wachtende projectbrede taak houdt een nieuwere smalle taak van hetzelfde project tegen, ook als er niets draait.
5. De uitzondering: een wachtende `backup` houdt een nieuwere `upsert_deployment` van hetzelfde project níet tegen, maar een dráaiende backup met overlappende scope wel.
6. `create_task` zet de kolom goed per tasktype, inclusief `add_component` dat zijn scope uit `payload.deployment_names` haalt, en inclusief de drie typen uit `_PROJECT_WIDE_TASK_TYPES` die `NULL` moeten krijgen ook als er een `deployment_name` meekomt.
7. Een rij met `affects_deployments = NULL` uit de tijd vóór de migratie gedraagt zich als projectbreed, aan beide kanten van het predicaat.
8. `find_blocking_task()` geeft de draaiende blokkeerder terug als er één draait, anders de oudste wachtende, en `None` als de taak vrij is.
9. Het API-antwoord van een geblokkeerde pending taak draagt `waiting_for` met de vier velden, en een niet-geblokkeerde pending taak draagt `null`.

En de gebruikelijke poort:

```
cd operations-manager/python
uv run ruff check . --fix && uv run ruff format .
uv run pyright
uv run pytest tests/ -k "task or supersede or argo" -q
```

## Wat er niet in deze taak zit

**Per-wacht supersede.** De elegantere oplossing zou zijn dat een lopende projectbrede taak een deployment uit zijn eigen wachtlijst haalt zodra er een delete voor klaarstaat, in plaats van dat de delete achteraan sluit. Dat scheelt wachttijd en houdt de andere deployments aan de gang. Het is nieuwe machinerie voor een blokkade die volgens de meting meestal onder de minuut duurt, dus eerst B laten draaien en dan meten. De scope in een kolom maakt dit later een kleine toevoeging in plaats van een herontwerp.

**Voorrang voor een delete.** Zelfde afweging, en het is de variant die pas zin heeft als per-wacht supersede er is.

**`timeout.reconciliation` in `argocd-cm`.** Blijft op 15 minuten. A3 lost het gerichter op.

**De conflict-check in `task_worker.py:230`.** Die logt een waarschuwing op `(project, task_type)` en wordt door B4 grotendeels overbodig: wat hij signaleert kan straks niet meer gebeuren. Laat hem staan; hem opruimen is een aparte, veilige schoonmaak en hoort niet in een PR die de wachtrij verandert.

## Randvoorwaarden

Er zit een schemamigratie in, anders dan bij RC-164. De kolom is nullable en additief, dus een oude OPI-versie draait op de nieuwe tabel zonder te breken. Andersom, een nieuwe OPI op een niet-gemigreerde database, werkt niet: `claim_next_task` verwijst naar een kolom die er niet is. Alembic draait bij het opstarten, dus dat lost zichzelf op, maar het is de reden om deze versie niet naast een oude te laten draaien tijdens een rollende update.

Niets uitrollen. Deze taak levert code, migratie en tests op, geen deploy.
