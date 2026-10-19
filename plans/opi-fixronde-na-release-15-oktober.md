# OPI: fixronde na de release van 15 oktober

## Waarom

Na de uitrol van 15 oktober 2026 (image `2026.10.15.1647-237570c4-dirty`) is OPI een avond lang gevolgd in productie. Een deel van wat misging zit al in RC-263 (event loop) en RC-264 (`/metrics`). Deze taak pakt de rest op: kleine, losse fouten die elk een gemeten gevolg hadden.

Gemeten op 15 oktober, tijden in CEST:

- **De nieuwe delete-wachtlus was de grootste CPU-verbruiker.** Per poll draait `kubectl get <321 types> -n <namespace>`, één aanroep duurt ongeveer 60 s, en 92 van de 321 types geven Forbidden in de tenant. Bij `pr-383` (drie projecten, 18:27) en bij een bulkverwijdering in `nd-j7s` (acht taken, 18:51) liepen drie tot acht van die lussen tegelijk, minstens 15 minuten lang (zo lang duurt het voordat ArgoCD git pollt). OPI zat op zijn CPU-limit van 3 cores; na het einde van de lussen zakte dat binnen een minuut naar 1,4 cores en lager. Om 18:53 liepen de inventarissen zelf op hun timeout van 120 s.
- **Gelijktijdige schrijfacties op dezelfde projectmap in `argo-applications` botsen.** Elke delete- en create-taak herschrijft `odcn-production/<project>/kustomization.yaml`. Van de acht gelijktijdige deletes in `nd-j7s` strandden er drie (`pr-66`, `pr-69`, `pr-75`) op `GitPushConflictError`, en om 18:45 ook een create (`Added ArgoCD resources for project nd-j7s`). `rebase_on_remote` breekt een conflict af en verwacht dat de aanroeper opnieuw toepast, maar op deze paden doet niemand dat.
- **Een delete waarvan de git-commit mislukt, wacht daarna toch 30 minuten.** `delete_deployment` (`opi/manager/delete_project_manager.py`) vangt de fout in stap 2 af, noteert hem, en gaat door naar stap 4: wachten tot ArgoCD een app verwijdert die nog gewoon in git staat. De drie taken liepen tot `Task exceeded maximum duration of 1800s` (19:21), met de zware inventarislus erbij. Pas de herhaling door de worker zette de verwijdering in git, en toen was hij binnen een minuut klaar.
- **Na een herstart van ArgoCD falen deploys hard.** De application-controller had na de uitrol 6,5 minuten nodig om zijn cluster-cache op te bouwen (15:06:52 tot 15:13:31 UTC). In dat venster bleef elke `GET ...?refresh=normal` hangen tot zijn timeout van 120 s, en `wait_for_project_level_application` gooide meteen `RuntimeError("Failed to refresh ArgoCD project-level application ...")`. Zes taken faalden zo zonder tweede poging (`mr-7qd` vier keer, `md-a8y` twee keer), waarvan één al op de login (`Failed to login to ArgoCD`, TimeoutError).
- **De image-tag is altijd `-dirty`.** `publish-operations-manager` hangt af van `generate-env-secrets-for-operations-manager`, die het secrets-bestand in `bootstrap/` herschrijft. `git status --porcelain` kijkt naar de hele repo en ziet dat. Het image bevat alleen `operations-manager/` (zie de `COPY`-regels in `operations-manager/Dockerfile`), dus de tag noemt de bron vuil terwijl hij schoon is. Daarmee verliest `-dirty` zijn betekenis, en de guard in de promote-taak (`Taskfile.yaml`, "komt van ongecommitte bron en hoort niet op productie") weigert een image dat in orde is.
- **Twee logregels die liegen.** `Mailrelay antwoordt niet op /healthz/ready: ` eindigt leeg bij een timeout (`opi/connectors/mail.py:244`). En elke definitief mislukte taak geeft twee regels, `permanently failed after 1 attempts` en direct daarna `after 0 attempts`, omdat `persistent_task_progress._fail_in_db` en `task_worker` allebei `fail_task` aanroepen en de regel `max_attempts` toont in plaats van de poging.

## Wat bewezen moet worden

1. **Een delete-wachtlus inventariseert alleen de types die de app beheert**, en de telling blijft live.
2. **Acht gelijktijdige deletes in één project landen alle acht in `argo-applications`**, zonder conflictfout en met een `kustomization.yaml` die alleen bestaande bestanden noemt. Hetzelfde voor een create en een delete tegelijk in één project.
3. **Een delete waarvan het verwijderen uit git niet lukt, wacht niet op ArgoCD.** De taak eindigt binnen seconden met een fout die de worker opnieuw probeert.
4. **Een ArgoCD die tijdelijk niet antwoordt, laat het projectniveau niet falen** zolang het budget niet op is. Alleen het verstrijken van het budget eindigt in een fout, en die noemt het budget.
5. **Een schone OPI-bron geeft een schone tag**, ook als het secrets-bestand net herschreven is.

Toets gedrag, niet aanroepen. Een test die alleen controleert dat een helper wordt aangeroepen, bewijst niets.

## Basis

- **Bouw op forgejo/main** (`e98a2cfaa` bij het schrijven). RC-263 loopt tegelijk en raakt dezelfde bestanden (`argo.py`, `argo_manager.py`, `delete_project_manager.py`, `project_manager.py`, `git.py`); hoe de twee samenkomen, wordt na afloop bij de merge bepaald. Houd je wijzigingen daarom klein en lokaal: geen herindeling of hernoeming van code die je niet voor deze punten nodig hebt.
- Dit plan is gemeten op de tak van RC-263 (commit `ea806a9d8`). De regelnummers wijken op main een paar tot enkele tientallen regels af; zoek op de naam.
- Wat RC-263 toevoegt, staat op main nog niet: `ArgoTransportError`, de gecachte jsonpath-compiler, wachtlussen die doorpollen op een transportfout, en de afgevangen kubectl-timeout. Bouw die niet na in deze taak; punt 4 gebruikt dus geen `ArgoTransportError` maar herhaalt op een lege uitkomst of een transportfout.

## Het werk

### 1. Delete-wachtlus: alleen de types van de app

**Probleem.** `wait_for_application_deletion` (`opi/connectors/argo.py:621`) haalt in `_deletion_progress` (`:684`) één keer `list_namespaced_resource_types()` op, alle namespaced types die het cluster kent (321), en geeft die per poll aan `argocd_application_deletion_progress` (`opi/connectors/kubectl.py:740`). Die telt via `list_tracked_resources` (`kubectl.py:973`) alle ArgoCD-resources in de namespace over al die types.

**Fix.** Lees bij de start van de wacht, één keer, de kinds uit `status.resources` van de Application (`group` en `kind` per resource) en vertaal ze met de bestaande `list_namespaced_kinds()` (`kubectl.py:941`, geeft `{"Kind.group": "plural.group"}`) naar kubectl-typenamen. Inventariseer per poll alleen die types. De telling blijft live in de namespace, zoals de docstring van `argocd_application_deletion_progress` eist: alleen de typelijst komt uit `status.resources`, nooit het aantal (dat bevriest zodra er een deletionTimestamp is).

Is `status.resources` leeg of niet te lezen, of kent `list_namespaced_kinds` een kind niet, val dan terug op het huidige gedrag voor die wacht. Log dat één keer op INFO.

**Test.** Een gestubde kubectl-connector met een Application die drie kinds beheert: de `get` per poll noemt precies die drie types, en de wachtlus ziet het aantal dalen. Een Application zonder `status.resources`: de wacht gebruikt de volledige discovery, zoals nu.

### 2. Schrijfacties op `argo-applications` per project serieel, op de nieuwste stand

**Probleem.** Zes plekken muteren een projectmap in `argo-applications` en committen: `delete_project_manager.py:534`, `:779`, `:1511`, `:2324` en in `argo_manager.py` `_commit_and_push_argo` (`:227`) en `:968`. De werkboom is gekloond bij de start van de taak, dus de mutatie en de herbouw van `kustomization.yaml` gaan uit van een stand die een andere taak intussen kan hebben gewijzigd. Bij een conflict breekt `rebase_on_remote` af (`opi/connectors/git.py`, rond `:906`) en volgt `GitPushConflictError`. Lukt de rebase wel, dan kan de herbouwde kustomization nog een bestand noemen dat een andere taak net heeft verwijderd.

**Fix.** Eén helper voor alle zes plekken: per (repo, project) een `asyncio.Lock`, en binnen die lock `reset_to_remote()` (`git.py:922`), dan de mutatie (bestand schrijven of verwijderen, kustomization herbouwen), dan commit en push. Dat kan omdat één OPI-instantie de enige schrijver is voor zijn eigen clustermap (CLAUDE.md, "Distributed Model"). De bestaande rebase-herhaling in `push_changes` blijft voor botsingen tussen verschillende projecten. Kijk eerst of het patroon van `ProjectStore` (`opi/services/project_store.py`, reset en opnieuw toepassen op `GitPushConflictError`) te hergebruiken is, voordat je iets nieuws schrijft.

Houd de lock alleen om de mutatie, de commit en de push, niet om het wachten op ArgoCD daarna.

**Test.** Acht gelijktijdige deletes in één project tegen een lokale bare repo als remote: alle acht commits staan erop, en `kustomization.yaml` noemt precies de overgebleven bestanden. Daarnaast een create en een delete tegelijk in hetzelfde project. Draai de test eerst zonder de fix en noteer in de PR dat hij dan faalt.

### 3. Een mislukte git-commit stopt de delete meteen

**Probleem.** In `delete_deployment` (`delete_project_manager.py:1369`) vangt stap 2 (`:1438`) elke fout af en zet alleen `argocd_file_removed` niet op True. Stap 4 (`:1600`) wacht daarna toch op het verdwijnen van de app, tot de maximale taakduur van 1800 s.

**Fix.** Lukt de commit van stap 2 niet, gooi de fout dan door in plaats van hem af te vangen. Vóór stap 2 gebeurt alleen lezen (stap 1), dus er is dan nog niets weggegooid. Een doorgegooide fout krijgt de herhaling van de worker (`task_worker.py`, `failed (attempt n/m), re-queued for retry`). Een fout die de handler zelf als resultaat meldt, krijgt die herhaling niet; kies dus de doorgegooide fout. Met punt 2 wordt een conflict hier zeldzaam, maar een push kan ook om andere redenen mislukken.

**Test.** Een gestubde git-connector die op de commit `GitPushConflictError` gooit: `delete_deployment` gooit binnen een seconde, de wachtlus wordt niet aangeroepen, en er wordt geen namespace, database of bucket aangeraakt.

### 4. Het projectniveau overleeft een ArgoCD die net herstart

**Probleem.** `wait_for_project_level_application` (`opi/manager/project_manager.py:3029`) doet één `argo_connector.login()` en één `refresh_application()` (`argo.py:520`, één GET met `timeout_seconds=120`). Geeft een van beide niets terug, dan gooit hij direct `RuntimeError`. Een refresh-GET blokkeert tot de controller de app verwerkt; tijdens het opwarmen van de controller gebeurt dat niet.

**Fix.** Herhaal login en refresh binnen een budget in plaats van één poging. Een transportfout of een lege refresh is opnieuw proberen, met een WARNING in de log. Meet het budget met de wandklok (`asyncio.get_running_loop().time()`), zoals RC-263 doet in de wachtlussen. Zet het budget als constante naast `PROJECT_LEVEL_SYNC_TIMEOUT_SECONDEN`, standaard 10 minuten (de gemeten opwarming was 6,5 minuten). Laat `refresh_application` zelf ongewijzigd voor de andere aanroepers (`grep -rn refresh_application opi`); de herhaling hoort in `wait_for_project_level_application`.

**Test.** Een stub die de eerste twee refreshes niets teruggeeft en daarna een `reconciledAt`: de wacht slaagt. Een stub die nooit iets teruggeeft: de wacht stopt pas na het budget, met een fout die het budget noemt. Hetzelfde voor een login die eerst faalt.

### 5. De image-tag kijkt alleen naar de bron van het image

**Probleem.** `publish-operations-manager` en `image-version` in `Taskfile.yaml` (rond regel 942 en 954) zetten `-dirty` op `git status --porcelain` over de hele repo. De afhankelijkheid `generate-env-secrets-for-operations-manager` herschrijft het secrets-bestand in `bootstrap/`, dus de tag is altijd `-dirty`.

**Fix.** Bepaal de vuilheid over de bronpaden van het image, zoals `scripts/bron-tag.sh` dat al doet voor `SANDBOX_OM_TAG` (`Taskfile.yaml:27`, `scripts/bron-tag.sh operations-manager`). Laat het CalVer-formaat van de tag gelijk. Hergebruik `bron-tag.sh` of zijn `git status --porcelain -- <pad>`; schrijf geen derde variant. `publish-keycloak-image` (rond regel 1497) heeft hetzelfde patroon; pas het daar ook toe als de bronpaden van dat image eenduidig zijn, en zeg anders in de PR waarom niet.

**Test.** Een bats- of shelltest is niet nodig. Toon in de PR de uitvoer van `task image-version` in drie standen: schone boom, alleen het secrets-bestand gewijzigd (geen `-dirty`), een wijziging onder `operations-manager/` (wel `-dirty`).

### 6. Twee logregels

- `opi/connectors/mail.py:244`: `{error or type(error).__name__}` levert bij een `TimeoutError` een lege tekst, omdat een exceptie-object altijd waar is. Gebruik `str(error) or type(error).__name__`. Test: een timeout geeft `TimeoutError` in de regel.
- `opi/core/async_task_service.py`, `fail_task` (rond regel 395): de regel `permanently failed after %d attempts` toont `max_attempts`, en de functie wordt per mislukte taak twee keer aangeroepen (`persistent_task_progress.py:375` met `max_attempts=1`, `task_worker.py:300` met `max_attempts=0`). Maak er één regel per definitieve fout van, met het echte pogingnummer. Verander niet wie het resultaat opslaat: de aanroep van `task_worker` schrijft `result` mee, en dat moet blijven. Test: een handler die faalt geeft precies één regel `permanently failed`.

## Buiten deze taak

- **Al gedekt door RC-263:** de jsonpath-cache die de event loop 8 tot 18 s liet stilstaan, ArgoCD-wachtlussen die doorpollen op een transportfout, en de kubectl-timeout. Die laatste wordt op de tak van RC-263 in `_run_kubectl_command` afgevangen, zodat `_deletion_progress` geen kale `TimeoutError` meer ziet. Op main gooit hij nog; raak dat hier niet aan, het komt met RC-263.
- **ArgoCD pollt git maar elke 15 minuten** (`timeout.reconciliation: 15m`, geen GitHub-webhook). Daardoor duurt elke verwijdering minstens zo lang. OPI ververst `user-applications` wel na de commit, maar om 16:27:30 UTC resolvede ArgoCD toen een oudere revisie (`b92aef7c`) terwijl de nieuwere al gepusht was. Dat vraagt eerst een meting en is een platformkeuze.
- **Een grens op het aantal gelijktijdige taken per project.** Met punt 2 botsen ze niet meer; of ze ook minder tegelijk moeten draaien, meet je pas daarna met de watchdog van RC-263.
- **Taken die tijdens de storing faalden, opnieuw aanbieden** (`md-a8y-pr-111`, `nd-j7s-pr-63`, `mb-docs-helmfile`): dat is beheer, geen code.

## Randvoorwaarden

- Niets uitrollen. Geen wijziging aan clusters, geen push naar GitHub.
- Geen gedragswijziging buiten wat hierboven staat. Waar een fix verandert welke fout een taak beëindigt of herhaalt (punt 3 en 4), staat dat expliciet in de PR.
- Zoek eerst of een helper bestaat voordat je hem schrijft (`opi/utils/`, `opi/services/project_store.py`, `opi/connectors/git.py`). Schrijf je toch iets nieuws naast iets bestaands, zeg dan in de PR waarom.
- Code-stijl volgens `CLAUDE.md`: imports bovenaan, moderne type hints, specifieke exceptions, minimaal commentaar, Nederlandse prose met Engelse vaktermen, nooit em dashes.

## Klaar als

- Alle toetsen hierboven staan er en zijn groen; voor punt 2 en 3 staat in de PR dat ze zonder de fix faalden.
- `cd operations-manager/python && uv run ruff check . && uv run ruff format --check . && uv run pyright` is schoon.
- De bestaande testsuite is groen.
- De PR toont de uitvoer van `task image-version` in de drie standen van punt 5.
- `features/` is bijgewerkt waar een beschreven gedrag verandert, in elk geval de beschrijving van de delete-wachtlus (zoek op `deletion_progress_window` en "vastgelopen verwijdering").
