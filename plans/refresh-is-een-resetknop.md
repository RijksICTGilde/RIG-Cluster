# Een refresh is een resetknop en hoort een lopende sync af te breken

Dit bouwt voort op RC-250 en staat op de branch van die taak (`een-sync-die-niet-afbreekt-blokkeert-elke-volgende`). De code die hier uitgebreid wordt bestaat daar; op `main` nog niet.

RC-250 heeft `_clear_blocking_sync_operations` toegevoegd, een ronde die vlak voor elke push de sync-operations beëindigt die een volgende sync blokkeren. Die ronde gebruikt `STUCK_OPERATION_THRESHOLD_SECONDS` (300s), en dat is juist, want ze loopt bij élke push: ook bij die van een sweep, de OOM- of VPA-tuner of een member-add. Daar is "deze uitrol is nog gewoon bezig" een echte mogelijkheid, en een nachtelijke sweep mag niet de uitrol afbreken die een ontwikkelaar twintig seconden eerder startte.

Bij een refresh is dat geen mogelijkheid meer. Iemand drukt op reset. Dan hoort een lopende sync af te breken, ook een die net begonnen is.

Datzelfde onderscheid maakt de code al op de andere aanroepplek: `_terminate_operation_we_waited_on` geeft `threshold_seconds=0` mee, met de reden erbij dat "nog gewoon bezig" daar geen mogelijkheid meer is. Dit plan voegt de derde plek toe, met dezelfde redenering.

## Beslist

* **Drempel 0 op het refresh-pad.** Elke lopende operation wordt beëindigd, ook een van vijf seconden oud. Een reset die soms wel en soms niet reset is erger dan een reset die een uitrol van enkele seconden opnieuw laat beginnen: dat kost seconden en breekt niets, en voorspelbaarheid is bij een resetknop de hele waarde.
* **Beide endpoints.** `POST /api/v2/projects/{p}/:refresh` en `POST /api/v2/projects/{p}/deployments/{d}/:refresh`. Het is dezelfde knop op een andere schaal.
* **Op projectniveau geldt het voor elke deployment in het project**, niet alleen voor de deployment die vastzit. Dat is bij een project met twintig deployments een grote hamer, en dat is de bedoeling: de knop zit op het project.

## De stappen

### 1. De vlag bij het endpoint, niet bij de handler

**Waar**: `opi/api/v2/router.py` (de twee refresh-endpoints), `opi/core/task_handlers_operations.py` (`handle_refresh_project` rond regel 444, `handle_refresh_deployment` rond regel 286), en van daar naar `ProjectManager._clear_blocking_sync_operations`.

Zet bij het endpoint een veld in de payload dat zegt "dit is een reset", en laat dat als drempel doorlopen naar `_clear_blocking_sync_operations`. Volg het pad dat `force_clone` al aflegt: endpoint, payload, handler, `process_project`, `_process_repository_manifests`. Naam van het veld is een voorstel, niet voorgeschreven; `reset_running_sync` dekt wat het doet.

Waarom bij het endpoint en niet bij de handler: `handle_refresh_project` wordt ook intern aangeroepen, uit het component-delete-pad (`opi/core/task_handlers_components.py:1023`, met `force_clone: True`). Hangt het gedrag aan de handler, dan erft die aanroeper het stilzwijgend, en dat is precies het soort stille uitwaaiering dat hier niet hoort.

**Voorstel voor die interne aanroeper**: geef de vlag daar wél mee, want een component-delete moet landen en een sync op de oude staat hoort dat niet te vertragen. Maar expliciet, met een test die vastlegt dat het een keuze was. Blijkt bij de review dat het anders moet, dan is dat één regel.

**Verify**: een test die aantoont dat een operation van vijf seconden oud bij een refresh wél beëindigd wordt en bij een gewone push niet, en een test die de interne aanroeper uit het delete-pad pint op de keuze die gemaakt is.

### 2. Zeg het hardop in de taakuitkomst

**Waar**: de voortgang van de refresh-taak.

Breekt een refresh een lopende sync af, dan hoort dat in de voortgang en in de uitkomst te staan. Anders ziet de ontwikkelaar van wie de uitrol werd afgebroken een mislukking zonder oorzaak. OPI heeft daar al woorden voor: `task_supersede.py` kent `Superseded while waiting for sync`, en de refresh-taken staan al in die lijst.

**Verify**: een test die de melding in de taakuitkomst vastlegt wanneer er werkelijk een operation beëindigd is, en die niets meldt wanneer er niets liep.

### 3. Documenteren waar het al beschreven staat

**Waar**: `features/vastgelopen-sync-breekt-af.md`, dat RC-250 heeft gemaakt en dat de twee bestaande aanroepplekken met hun drempels al uitlegt.

Daar komt de derde plek bij, met dezelfde opzet: welke drempel, en waarom die daar anders is. Het open punt in dat document over welk endpoint `zadctl deployment refresh` aanroept kan dicht: `zad-cli` doet `POST /v2/projects/{p}/deployments/{d}/:refresh`, gemeten in de repo `~/IdeaProjects/zad-cli` (`tests/test_spec.py` en `tests/test_rollout_config.py` noemen dat pad). De bewering in het RC-250-plan dat een refresh alleen ArgoCD's vergelijking opnieuw aanzet ging over wat OPI's connector binnen die taak doet, niet over wat zadctl aanroept.

## Wat erbuiten valt

`PruneLast=true` blijft staan, dat is in RC-250 beslist met een meting. De controller-grens `--sync-timeout=600` verandert niet. De drempel van 300s voor gewone pushes verandert niet: die is juist en de reden staat opgeschreven.

Uitrollen naar odcn-production hoort hier niet bij, net als in RC-250.

## Hoe je weet dat het werkt

De eenheidstests hierboven dekken de drempel en de melding zonder cluster.

Wil je het in het echt zien, dan is de opstelling die van vorm A uit RC-250: maak een deployment met twee componenten, maak component A ongezond, haal component B weg zodat er prune-taken op wave 1 staan, en kijk of de sync `Running` blijft. Druk daarna op refresh en controleer dat de operation binnen die ronde beëindigd wordt zonder dat je 300 seconden hoeft te wachten, en dat de eerstvolgende sync de nieuwste revisie pakt. De sandbox is gedeeld: claim hem met `orch sandbox claim` en geef hem terug met `orch sandbox release`.
