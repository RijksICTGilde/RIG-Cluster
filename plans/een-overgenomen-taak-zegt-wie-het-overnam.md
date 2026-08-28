# Een overgenomen taak zegt wie het overnam

Op 28 augustus 2026 liep in `mpfb-8wh` een deploy via zad-actions v4 rood met de melding `Could not extract URLs from result`, terwijl de uitrol gewoon was aangemaakt. De oorzaak is dat de deploy zijn ArgoCD-wachtstap heeft overgedragen aan een nieuwere taak, en dat het resultaat dat wij dan teruggeven niet vertelt wat er gebeurde. Dit plan repareert de kant van de Operations Manager: een overgenomen taak zegt voortaan dat hij is overgenomen en door wie. Wat een client daarmee doet, is een keuze van die client, en die keuze staat in deel B en deel C beschreven als contract voor zad-cli en zad-actions.

**Alleen deel A wordt in deze taak gebouwd.** Deel B en deel C liggen in andere repositories en horen niet in deze PR. Ze staan er wel in, want zonder die twee is deel A een verandering waar niemand iets aan heeft, en het plan is het document dat naar die twee repositories meegaat.

## Wat er nu gebeurt

De keten, met ankers, zoals hij op dit moment loopt:

1. `opi/manager/argo_manager.py:884` en `:1138` roepen `raise_if_superseded()` aan in de twee ArgoCD-wachtlussen: wachten tot de Application bestaat, en wachten tot hij gesynct en gezond is.
2. `opi/core/task_supersede.py:152` kijkt of er een nieuwere, nog niet afgeronde taak voor hetzelfde project klaarstaat waarvan de deployment-scope die van de huidige taak omvat. Zo ja, dan gooit hij `TaskSuperseded` met een zin als boodschap.
3. `opi/core/task_worker.py:315` vangt dat op en rondt de taak **af** met `{"status": "superseded", "message": str(superseded)}`. Bewust geen mislukking: het duurzame werk (projectbestand gecommit, manifests gegenereerd en gepusht) is gedaan, en de nieuwere taak doet de rest opnieuw. Als mislukking zou het retries en alarmen aanzetten voor een nette overdracht.
4. De CLI ziet taakstatus `completed`, `result_failure()` slaat alleen aan op `failed` en `error`, dus exitcode 0 en de melding "Deployment successful".
5. De action (tag `v4`, commit `13434cd4`) leest daarna `.urls.<deployment>.urls` uit dat resultaat, vindt niets, en maakt daar zelf een fout van.

De melding die de gebruiker ziet komt dus uit stap 5, niet uit stap 3. Bij ons faalt er niets. Wat er misgaat is dat stap 3 een resultaat teruggeeft waaruit niet valt af te leiden dat er nog werk loopt, en stap 4 dat leest als "klaar".

## Twee dingen die niet aan de hand zijn

Het incidentbericht bevat twee aannames die niet kloppen, en die zijn gecontroleerd omdat ze anders de oplossing sturen.

**Het slot is niet per project maar per deployment.** `opi/core/async_task_service.py:207-217` sluit een pending taak uit zolang er een andere actieve taak is met hetzelfde `project_name` **en** hetzelfde `deployment_name`, waarbij `is_not_distinct_from` NULL alleen met NULL laat matchen. Twee deployments in hetzelfde project draaien dus gewoon naast elkaar. De asymmetrie die het incident veroorzaakte zit in die NULL: projectbrede taken dragen geen deployment_name, dus zij en een deployment-gerichte taak zien elkaar nooit als in-flight en lopen tegelijk.

**Twee PR's kunnen elkaar niet superseden.** `covers()` in `opi/core/task_supersede.py` eist dat de scope van de nieuwere taak een superset is van de huidige. `{pr-250}` is geen superset van `{pr-248}`. Alleen deze taaktypes nemen een lopende deploy over, want alleen zij zijn projectbreed: `update_component`, `add_service`, `refresh_project`, `delete_component`, en `add_component` als zijn payload geen `deployment_names` bevat. Alle andere API-taken dragen wel een deployment_name en blijven daarbinnen. De cleanup-action valt daar niet onder: die roept `zad deployment delete` aan, en dat is `delete_deployment` met een deployment_name.

Er is nog een tweede, gezonde manier om overgenomen te worden: een nieuwere taak voor **dezelfde** deployment. Push je twee keer naar dezelfde PR, dan wacht de tweede deploy in de wachtrij en geeft de eerste zijn ArgoCD-wait op in plaats van vijf minuten te verbranden. Daar heeft GitHub zijn `cancel-in-progress` de oudere job meestal al gekilld, dus er wacht niemand meer op het antwoord.

In de praktijk is dit dus vooral een botsing tussen handmatig portaalwerk en een lopende CI-run, precies zoals het incident was.

## Het besluit

Drie keuzes, met de reden erbij, want ze zijn alle drie tegen een plausibel alternatief afgewogen.

**Een overgenomen resultaat draagt geen urls.** Het lag voor de hand om ze wel mee te geven, want ze zijn op dat moment bekend: `process_project()` staat op `opi/manager/project_manager.py:3110` en vult `_deployment_results`, de ArgoCD-waits staan daarna op `:3229` en `:3387`. Toch niet doen. De sync is precies het stuk dat we laten vallen, en de overnemende taak staat op het punt diezelfde manifests opnieuw te genereren en te committen. Urls teruggeven zou een toestand beweren die we expres niet meer gecontroleerd hebben.

**De taakstatus blijft `completed`.** Een vierde eindstatus `superseded` is inhoudelijk zuiverder, en de kolom is een gewone string dus het kost geen migratie. De prijs valt buiten onze repo: elke zadctl die vandaag draait pollt tot hij een bekende eindstatus ziet, en een onbekende status betekent voor die versies niet "klaar" maar dóórpollen tot de task-timeout. Dat ruilt een verkeerde melding in voor een hang van minuten bij iedereen die niet meteen upgradet. De eerlijkheid gaat daarom in het resultaat en in de envelop zitten, niet in de statuskolom.

**Volgen is een clientkeuze, geen serverkeuze.** De server zegt wat er gebeurde en wie het overnam. Of je daarop doorwacht verschilt per aanroeper: een CI-job wil groen worden als het uiteindelijk goed ging, een script wil misschien juist meteen terug. Die knop hoort in de CLI.

## Deel A: Operations Manager

### A1. `TaskSuperseded` draagt de identiteit van de overnemer

In `opi/core/task_supersede.py` krijgt de exception velden in plaats van alleen een boodschap:

```python
class TaskSuperseded(BaseException):
    def __init__(self, message: str, *, task_id: str, task_type: str, project_name: str) -> None:
        super().__init__(message)
        self.task_id = task_id
        self.task_type = task_type
        self.project_name = project_name
```

`raise_if_superseded()` heeft die drie waarden al in handen: `find_superseding_task()` geeft het hele taakrecord terug en de huidige boodschap wordt er al uit opgebouwd. Vul ze dus vanuit dezelfde `newer`-dict en laat de boodschap staan zoals hij is, want die is voor mensen en wordt op meerdere plekken gelogd.

Blijft ongewijzigd: `TaskSuperseded` erft van `BaseException` en niet van `Exception`, zodat de brede `except Exception`-handlers langs het verwerkingspad een nette overdracht niet als mislukking oppikken. De module-docstring legt dat uit; laat die uitleg staan en vul hem aan met wat de velden zijn.

### A2. De worker schrijft het gestructureerd weg

`opi/core/task_worker.py:315` wordt:

```python
await self._task_service.complete_task(
    task_id,
    {
        "status": "superseded",
        "message": str(superseded),
        "superseded_by": {
            "task_id": superseded.task_id,
            "task_type": superseded.task_type,
            "project_name": superseded.project_name,
        },
    },
)
```

Verder niets: geen urls, geen half resultaat van de handler. Het resultaat zegt precies één ding, namelijk dat deze taak zijn werk niet heeft afgemaakt en wie het overnam.

### A3. De API-envelop toont het zonder dat je de resultaatvorm kent

Een client moet hierop kunnen handelen zonder per taaktype de resultaatvorm te kennen. Voeg daarom in `opi/api/task_models.py` een klein model toe en hang het aan `TaskResponse`, naast `error_message`:

```python
class SupersededByResponse(BaseModel):
    task_id: str
    task_type: str
    project_name: str
```

`task_response_from_dict()` (`opi/api/task_models.py:492`) is het enige punt waar een opgeslagen taakrecord een API-antwoord wordt, voor V1 en V2 allebei, en `error_category` wordt daar om precies die reden ook al bijgezet. Til `superseded_by` daar uit het resultaat naar het topniveau, en zet de sleutel altijd neer (`None` als er niets is), consistent met hoe `pending_rollout` het doet. Er komt geen kolom bij in de database en er is dus geen migratie.

De route zelf (`opi/api/task_router.py:200`) hoeft niets te doen: die geeft `response_body` door als platte dict en valideert niet tegen `TaskResponse`, dat model staat er alleen voor de documentatie. Wel bijwerken: de beschrijving van het `status`-veld op `TaskResponse` zegt nu dat `completed` betekent dat de hele taak geslaagd is. Dat is niet meer waar en die zin is precies wat een clientbouwer leest. Schrijf erbij dat een taak ook `completed` kan zijn terwijl zijn resultaat `status: "superseded"` draagt, en dat `superseded_by` dan gevuld is.

### A4. Wat er niet verandert

Geen wijziging aan `scope_of()` of `covers()`. De supersede-regel zelf klopt: de `delete_component` uit het incident riep `handle_refresh_project` aan voor het hele project (`opi/core/task_handlers_components.py:1023`), dus pr-248 werd wel degelijk opnieuw verwerkt.

Geen wijziging aan het claim-slot. Een NULL `deployment_name` laten conflicteren met alle deployments van een project zou deze hele klasse laten verdwijnen, maar dan blokkeert elke projectbrede taak al het deploymentwerk in dat project en omgekeerd. Dat is een doorvoerkeuze die hier niet thuishoort; noem hem niet stilzwijgend mee.

### A5. Verificatie

Nieuwe tests bij de bestaande supersede-tests:

1. Een taak die `TaskSuperseded` krijgt tijdens de ArgoCD-wait wordt afgerond met `status: "completed"` op de taak, en met een resultaat dat `status: "superseded"` en een gevulde `superseded_by` met de drie velden draagt.
2. Datzelfde resultaat bevat **geen** `urls`-sleutel. Dit is de assertie die de beslissing hierboven vasthoudt; zonder hem sluipt het er bij de eerste volgende handigheid weer in.
3. `task_response_from_dict()` tilt `superseded_by` naar het topniveau van het antwoord, en zet `None` neer voor een gewone voltooide taak.
4. De keten `raise_if_superseded` → worker → API-antwoord in één test, zodat de drie velden aantoonbaar van de gevonden taak tot in de envelop komen en niet onderweg van naam veranderen.

En de gebruikelijke poort:

```
cd operations-manager/python
uv run ruff check . --fix && uv run ruff format .
uv run pyright
uv run pytest tests/ -k "supersede or task_worker or task_models" -q
```

## Deel B: zad-cli (contract, niet in deze PR)

Wat er voor zad-cli verandert, is dat een voltooide taak nu kan zeggen dat hij is overgenomen. Vandaag ziet `_poll_task` in `src/zad_cli/api/client.py:355` status `completed`, laat `result_failure()` het passeren omdat die alleen op `failed` en `error` aanslaat, en geeft het resultaat terug met exitcode 0. Dat blijft werken zoals het werkt; oude versies breken niet.

Wat de CLI zou moeten gaan doen:

1. Zie je een voltooide taak met `superseded_by`, volg dan standaard die taak: pol verder op `superseded_by.task_id` binnen dezelfde `task_timeout`-deadline, met een hop-limiet (vijf is ruim) tegen een keten die blijft doorschuiven.
2. Zeg per hop hardop wat er gebeurt, op stderr, zodat het ook in een CI-log te lezen is: "taak `<id>` (`<type>`) heeft jouw taak overgenomen; wachten op dat resultaat".
3. Label wat je teruggeeft. Het resultaat dat uiteindelijk terugkomt is niet van de taak die de gebruiker startte, en dat mag niet impliciet blijven: neem in de JSON-uitvoer op welke taak gevraagd werd en welke taak dit resultaat opleverde.
4. Volg de uitkomst van de laatste taak in de keten voor de exitcode. Faalt de overnemende taak, dan faalt de aanroep, met een boodschap die zegt dat het die andere taak was en welk type.
5. Uitzetten kan met `--no-follow-superseded`, en met `ZAD_FOLLOW_SUPERSEDED` voor wie het per omgeving wil regelen. `src/zad_cli/settings.py` combineert vlag, env en envfile al op deze manier via `_bool_setting`; sluit daarbij aan. Uit betekent: geef het superseded resultaat terug zoals het is.

Eén valkuil die je niet hoeft te ontdekken: de overnemende taak zit per definitie in hetzelfde project, dus de API-sleutel die je al gebruikt geeft er toegang toe. `_validate_task_access` valideert op project, niet op taak.

## Deel C: zad-actions (contract, niet in deze PR)

Er ligt al een onuitgebrachte fix. Op branch `chore/bump-zad-cli-v1`, commit `da90372`, haalt `deploy/action.yml` de urls niet meer uit de vorm van het taakresultaat maar vraagt hij ze op met `zad deployment url`. Daarmee verdwijnt de melding uit het incident vanzelf, want een superseded resultaat hoeft dan geen urls meer te dragen. Dat commando bestaat in zad-cli 0.11.0.

Wat er daarnaast zou moeten gebeuren:

1. Die branch uitbrengen. Dat is de eigenlijke reparatie van de rode job.
2. Een `::notice::` bij een overgenomen deploy die de overnemende taak noemt, zodat er in het log staat waarom het langer duurde en wat het overnam.
3. De README bijwerken op het punt van concurrency, en daar het advies **omkeren** ten opzichte van wat het incidentbericht suggereert. PR-deploys naar hetzelfde project mogen parallel: ze blokkeren elkaar niet en ze nemen elkaars werk niet over. Wat je uit een parallelle run wilt houden zijn de projectbrede opdrachten, dus `zad service add` en `zad component update`, en handmatig portaalwerk terwijl er een deploy loopt. Een concurrency-groep per project zou PR-deploys onnodig serialiseren.

## Randvoorwaarden

Geen migratie, geen schemawijziging, geen nieuwe eindstatus. De verandering is additief: een sleutel erbij in een resultaat dat vandaag al bestaat, en een veld erbij in een envelop. Een client die er niets van weet ziet precies wat hij nu ziet.

Niets uitrollen. Deze taak levert code en tests op, geen deploy.
