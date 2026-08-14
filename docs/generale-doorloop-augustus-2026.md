# De generale: de hele suite tegen een nieuwe versie van ZAD

Laatste meting van `release-augustus-2026` voor de merge naar `main`.

- **Gemeten commit**: `c0be0074`, de taakbranch bovenop `ab6ae614`
  (`feat(sleep-mode): een slapende applicatie wordt niet meer vanzelf gewekt`)
- **Cluster**: `kind-rig-sandbox`, `sandboxed-local`
- **Datum**: 14 augustus 2026

> Deze doorloop is twee keer verlegd. Hij begon op `418533e5`; daarna kwam de
> opdracht om op `e187015e` te meten (de standaardmaten), en vervolgens om op
> `ab6ae614` te meten (wake-mode standaard `manual`). Alles hieronder is de meting
> op de laatste tip. Metingen van een eerdere ronde staan er expliciet bij, en zijn
> alleen blijven staan waar ze iets aantonen dat later niet meer zichtbaar is.
>
> Dat verleggen is niet gratis geweest: twee keer is een lopende e2e-run ongeldig
> geworden doordat de werkboom onder de suite uit veranderde. Dat is te zien ook -
> de voettekst van een pagina in een faalrapport noemde al de nieuwe commit terwijl
> de run op de oude begonnen was. Zulke runs zijn weggegooid en overgedaan, niet
> geinterpreteerd.

## Oordeel

(volgt aan het eind van de doorloop)

## Taak 1 - Verse sandbox met een verse build

### Wat er niet gedaan kon worden, en waarom

Het plan vraagt `task sandbox:destroy` gevolgd door `task sandbox:setup`. **Dat is
in deze sessie niet uitgevoerd, en dat is een bewuste keuze.**

`sandbox:setup` begint met `task requirements-check`, en die faalt hier hard:

```
task: sops is not installed! Install with 'brew install sops' ...
task: precondition not met
```

Naast `sops` ontbreken ook `yq` en `pwgen`. Zwaarder weegt dat `sandbox:setup` als
tweede stap `sandbox:decrypt-wildcard-cert` draait, en die heeft
`security/developer-key.txt` nodig. Die sleutel wordt buiten de repo om verstrekt en
staat hier niet:

```
/workspace/security/
  readme.md
  tls/sandbox-wildcard/{fullchain.pem.age, privkey.pem.age}
```

Er is dus geen weg terug: `sandbox:destroy` zou het gedeelde cluster slopen zonder
dat deze sessie het opnieuw kan opbouwen. Dat is precies waar
`workflow/sandbox.md` voor waarschuwt ("Do NOT run `task sandbox:update-operations-manager`
(or `sandbox:setup`) in a session"). Daarom is het cluster blijven staan en is de
versie onder toets erop gezet met `sandbox-deploy`, de weg die voor sessies bedoeld is.

**Gevolg voor de meting**: het "vanaf nul"-argument uit het plan - dat de helft van
de vorige bevindingen uit blijven staande toestand kwam - geldt hier niet. Wat hier
gemeten is, is gemeten op een cluster dat al draaide. Wie een echte verse doorloop
wil, moet die op een machine draaien die de developersleutel en `sops` heeft.

### Versiecontrole

`kubectl config current-context` gaf `kind-rig-sandbox`.

Vijf keer achter elkaar, na afloop van de rollout:

```
1: c0be0074 dirty=false pod=operations-manager-7994df4c9-gttlx
2: c0be0074 dirty=false pod=operations-manager-7994df4c9-gttlx
3: c0be0074 dirty=false pod=operations-manager-7994df4c9-gttlx
4: c0be0074 dirty=false pod=operations-manager-7994df4c9-gttlx
5: c0be0074 dirty=false pod=operations-manager-7994df4c9-gttlx
```

Eén pod, één commit, geen mengsel.

### Bevinding - `sandbox-deploy` kan stilzwijgend de vorige build laten staan

Dit is de belangrijkste vondst van taak 1, en hij raakt iedereen die op de sandbox meet.

`sandbox-deploy` bouwt met een **vaste imagetag** (`operations-manager:rc-113-`) en
zet die daarna met `kubectl set image`. Bij de tweede en derde deploy is die tag
ongewijzigd, dus `kubectl set image` verandert niets aan de podspec en Kubernetes
start **geen nieuwe pod**. De nieuwe image staat wel op de node, maar de draaiende pod
blijft de oude draaien.

Wat het script dan meldt:

```
[sandbox-deploy] deployed version : c0be0074
[sandbox-deploy] running /version : {"version":"e187015e", ..., "dirty":true,
                                     "pod":"operations-manager-747d6b5698-t54jn"}
[sandbox-deploy] WARN - /version does not clearly show c0be0074. Re-run after the pod is ready.
```

`deployment "operations-manager" successfully rolled out` stond er nota bene boven.
De rollout slaagde ook - er was alleen niets te rollen.

Twee dingen deugen hier niet:

1. **Het advies in de waarschuwing werkt niet.** "Re-run after the pod is ready"
   levert exact hetzelfde resultaat op, want de tag blijft gelijk en er komt weer geen
   nieuwe pod. Wie het advies opvolgt blijft in een lus zitten en meet ondertussen de
   vorige release. Wat wel werkt is `kubectl -n rig-system rollout restart deployment/operations-manager`.
2. **De waarschuwing kan ook onterecht zijn.** Bij de eerste deploy verscheen dezelfde
   WARN, maar toen was het een echte race: het script leest `/version` voordat de oude
   pod weg is. Na `kubectl rollout status` klopte het beeld wel. Dezelfde melding dekt
   dus twee heel verschillende situaties - een die vanzelf goed komt en een die dat
   nooit doet.

Het `"dirty":true` in dat antwoord is het derde signaal: `version.json` was gebouwd
van een werkboom met wijzigingen erin.

Dit is geen bevinding over de release zelf, maar wel een over de meetopstelling, en
hij is precies zwaar genoeg om een groene suite betekenisloos te maken. Na
`rollout restart` gaf `/version` vijf keer `c0be0074` met `dirty=false`.

### De probepoort (nieuw in deze release)

Hier zit de belangrijkste bevinding van taak 1.

**`sandbox-deploy` zet alleen het image, niet het manifest.** De pod draaide daardoor
na de deploy nog op de OUDE deploymentspec: één containerpoort (8000) en alle drie de
probes op 8000. De probepoort uit deze release stond wél in de branch
(`bootstrap/rig-system/kustomize/operations-manager/base/deployment.yaml`), maar niet
op het cluster. Wie alleen `sandbox-deploy` draait en dan `describe pod` leest, meet
de vorige release en ziet dat niet.

De deployment blijkt niet door ArgoCD beheerd (geen `argocd.argoproj.io/instance`-label,
alleen `kubectl.kubernetes.io/last-applied-configuration`), dus de nieuwe spec is er met
een strategic-merge patch op gezet: de twee containerpoorten plus de drie probes precies
zoals de branch ze definieert. Daarna:

```
ports:
  - containerPort: 8000  name: http
  - containerPort: 8001  name: probe

Liveness:   http-get http://:probe/healthz  period=30s failure=3
Readiness:  http-get http://:probe/readyz   period=30s failure=3
Startup:    http-get http://:probe/healthz  delay=5s period=5s failure=60
Ready: True    Restart Count: 0
```

Port-forward naar 8001:

```
/healthz -> HTTP 200 {"status": "ok"}
/readyz  -> HTTP 200 {"status": "ok"}
```

De pod werd `Ready` met **0 herstarts**. Tijdens het opkomen faalt de startup-probe
een paar keer met `connection refused` op 8001 - dat is de verwachte race tussen
kubelet en het bindende proces, en de `failureThreshold: 60` vangt dat af.

Ook goed om vast te leggen: de `readinessProbe` staat nu op `periodSeconds: 30` met
`failureThreshold: 3`. Dat is de correctie op wat in RC-112 gemeld werd, waar
`failureThreshold: 1` één trage meting genoeg maakte om de pod uit de endpoints te
halen en de hele API een 503 te laten geven.

## Taak 2 - Unit, e2e en sandbox

### Unit

De eigen standaardaanroep van het project, zonder eigen `-m` (de `addopts` in
`pyproject.toml` sluiten `requires_infra` en `e2e` al uit):

```
uv run pytest tests/ -q
= 8683 passed, 7 skipped, 533 deselected, 19 warnings in 351.46s (0:05:51) =    exit 0
```

Nul failures, nul errors. De 7 die overslaan zijn goed; de 533 deselected zijn de
`requires_infra`- en `e2e`-tests die deze aanroep per definitie niet draait.

### E2E (eigen server, geen cluster nodig)

```
uv run pytest -m e2e -q
= 401 passed, 67 skipped, 8754 deselected, 1 xpassed, 33 warnings in 789.96s (0:13:09) =   exit 0
```

De 67 die overslaan zijn de `sandbox`-tests: die slaan zonder `E2E_BASE_URL` automatisch
over. De `1 xpassed` is een test die als "verwacht rood" staat aangemerkt en toch slaagde;
dat is geen failure, maar het is wel een aanwijzing dat een `xfail`-markering achterloopt.

**Maar dit werd pas groen na een reparatie.** De eerste schone run op de gemeten tip gaf
drie rode:

```
FAILED tests/e2e/test_lotc_projecten.py::test_de_pagina_levert_zelf_het_stuk_dat_het_zoekveld_ververst
FAILED tests/e2e/test_lotc_projecten.py::test_zoeken_en_sorteren_staan_in_de_toolbar
FAILED tests/e2e/test_gedragsoppervlak.py::test_de_pagina_kan_nog_alles_wat_er_vastligt[/projects]
```

#### Ligt het aan de test of aan de code? Aan de test.

Alle drie hebben dezelfde oorzaak, en die is met `git log -S` op de bron te vinden en niet
te raden. Commit `80da844c` (`fix(projecten): de gekozen sortering klopt met wat je ziet`)
deed twee dingen:

1. De projectenpagina swapt sindsdien het **hele zoekgebied** (`#projects-zoekgebied`) in
   plaats van alleen de lijst (`#projects-lijst`). Reden staat in het sjabloon: de werkbalk
   lag buiten het geswapte stuk, dus het vinkje bleef op de oude sortering staan en de
   zoekterm sprong terug.
2. Twee sorteeropties zijn eruit gehaald. Letterlijk in de commitboodschap: *"Meegenomen:
   de twee sorteeropties die niemand gebruikte zijn eruit."* In `PROJECT_SORTERINGEN`
   staan nu alleen `naam` en `naam-af`.

De drie toetsen waren daar niet in meegegaan: één vroeg om vier sorteerlinks, één toetste
de oude, kleinere swap, en het gedragsoppervlak had `?sort=deployments` en `?sort=teamleden`
vastgelegd. Ze sloegen dus aan op een verwijdering die met opzet gedaan was.

Empirisch bevestigd op het draaiende cluster, niet alleen in de test: op `/projects` komen
`/projects?sort=naam` en `?sort=naam-af` wel voor en `?sort=deployments` en `?sort=teamleden`
nul keer.

#### Wat eraan gedaan is

Commit `725762e2`. De eerste toets volgt nu de twee opties en toetst er bovendien bij dat de
andere twee **weg blijven** - anders vangt hij een terugkeer niet. De tweede toetst
`#projects-zoekgebied` (met `hx-select` en `hx-target`), en houdt de controle op
`#projects-lijst` erbij zodat de lijst zelf niet stilletjes kan verdwijnen.

Van het gedragsoppervlak is **alleen** de `/projects`-ingang bijgewerkt. Een volledige
herschrijving met `ZAD_SCHRIJF_OPPERVLAK=1` legde ook drift op zeven andere paden vast
(een nieuw `kerncijfer-pods` op `/dashboard`, een `services-info`-link op acht
projectpaden, en een extra `hx-post` zonder wizardtoken op `modal-edit-services`). Die
drift faalde niet - de toets bewaakt of het vastgelegde er nog **is**, niet of er iets bij
gekomen is - maar hem stilzwijgend vastpinnen zou betekenen dat ik dingen als "beoordeeld"
opschrijf die ik niet beoordeeld heb. Ze staan hier dus als waarneming en niet in de
baseline.

Na de reparatie: 401 passed, 0 failed.

### Sandbox

Zie de aparte kanttekening hieronder over wat `-m sandbox` werkelijk selecteert.

### Kanttekening: `-m sandbox` trekt de lange suites mee

Het plan schrijft voor taak 2 `uv run pytest -m sandbox -q` voor, en voor taak 3 apart
`-m reallife` en `-m punt14`. Dat werkt niet zoals bedoeld: **beide** lange suites dragen
óók de `sandbox`-marker.

```python
# tests/e2e/test_sandbox_reallife.py
pytestmark = [pytest.mark.e2e, pytest.mark.sandbox, pytest.mark.reallife, pytest.mark.serial]
# tests/e2e/test_sandbox_punt14.py
pytestmark = [pytest.mark.e2e, pytest.mark.sandbox, pytest.mark.punt14, pytest.mark.serial]
```

`-m sandbox` selecteert ze dus allebei, en dan draait taak 2 er ruim een uur aan taak-3-werk
bij - serieel bovendien, terwijl het plan die twee juist **gelijktijdig** wil hebben. Dat is
in de eerste poging ook echt gebeurd: de run begon aan `test_sandbox_punt14.py` en is
daarom afgebroken en opnieuw gestart.

De Taskfile heeft dit half ondervangen:

```
task test-e2e-sandbox -> uv run pytest tests/e2e/ -m "e2e and sandbox and not reallife" ...
```

`reallife` wordt uitgesloten, `punt14` niet - terwijl de markerbeschrijving in
`pyproject.toml` zegt dat punt14 "buiten de standaard sandboxrun" hoort. Voor deze doorloop
is daarom `-m "sandbox and not reallife and not punt14"` gebruikt, wat de gedocumenteerde
bedoeling volgt.

## Taak 3 - De reallife-suite

(volgt)

## Taak 4 - Wat deze release nieuw heeft, in de browser

Gedaan op een eigen project (`rk-qfc`, "RC113 Kijken") met een deployment `prod` en een
component `web`, met `scripts/kijk_sandbox.py` voor de schermafdrukken. Er is per punt
ook echt naar het plaatje gekeken.

### 1. De wizard slikt geen verzendingen meer - WERKT (cross-domain, modal-edit)

Dit is de fix uit `4225c610`: de hertekenhandler deed `htmx.trigger(form, 'submit')`
terwijl de velden die je juist gaat invullen nog leeg en verplicht waren, waarop de
browser stil weigerde - geen fout, geen verzoek, en de lijsten eronder bleven op "Kies
eerst een project" staan.

Nagelopen in de modal-edit van cross-domain, op een project met een echte buur
(`rb-47q`, met deployment `acc` en component `api`). Voor het kiezen:

```
inbound[0]/from/project     ['-- Kies een project --', 'invit-05n', 'p1431-9x9', 'rb-47q', ...]
inbound[0]/from/deployment  ['Kies eerst een project']
inbound[0]/from/component   ['Kies eerst een project']
```

Toen `rb-47q` gekozen, met de netwerkverzoeken meegeteld:

```
POSTs voor de keuze: 1
POSTs na de keuze  : 2   (nieuw: 1)
     POST /projects/rk-qfc/modal-wizard/modal-edit-cross-domain-config/step/cross-domain-access-config

NA: inbound[0]/from/deployment -> ['-- Elke deployment (per deployment invullen) --', 'acc']
NA: inbound[0]/from/component  -> ['-- Kies een component --', 'api']
```

Er gaat dus daadwerkelijk **een** verzoek uit, en de twee lijsten eronder komen gevuld
terug met de deployment en het component van het gekozen buurproject. Dat is precies wat
er eerst niet gebeurde.

**De bijlagenstap** is apart nagelopen, in de echte create-wizard (de stap verschijnt
zodra je de dienst Bijlagen aanvinkt):

```
>> BIJLAGENSTAP bereikt
--- toevoegen ---
  lijst na upload: bijlage1.txt (id: cert, nog niet opgeslagen)
--- verwijderen ---
  knoppen in de lijst: 1 ['Verwijderen']
  lijst na verwijderen: Nog geen bijlagen.
  'cert' weg: True
```

Toevoegen levert een regel in de lijst op, verwijderen haalt hem er weer uit. Ook hier
gaat er dus daadwerkelijk iets gebeuren waar eerst niets gebeurde.

Wat **niet** gemeten is: dezelfde cross-domain-stap in de create-wizard. Alleen de
modal-edit is gedreven. Zie "Wat er niet gemeten is".

### 4. Een bijlage vervangen met behoud van de id - WERKT (maar niet waar je hem zoekt)

Eerst het resultaat. Een bijlage `cert` met bestand `bijlage1.txt`, daarna vervangen door
`bijlage2.txt`:

```
voor:  id=cert filename=bijlage1.txt
PUT /api/v2/projects/rk-qfc/services/attachments/attachment/cert
       {"attachment":"cert","replaced":true,"component":null}   HTTP 200
na:    id=cert filename=bijlage2.txt
```

`replaced: true`, één regel in de catalogus, dezelfde id, ander bestand. Dat is precies
wat het punt vraagt.

**Maar dit is de API-weg en niet de UI-weg, en dat is een bevinding.** Het plan vraagt om
het vervangen *via de UI*, en die route is op de projectpagina's niet te vinden:

- Op het tabblad Services staat Bijlagen wel, maar als "Per component te kiezen", zonder
  Configureer-knop en zonder bestandsveld (`file-inputs: 0` op die pagina). De enige twee
  Configureer-knoppen daar zijn van sleep-mode en cross-domain.
- Op de tabbladen Services, Componenten en Overzicht komt `modal-edit-attachments`
  nergens voor als aanroep; wel `modal-edit-identity`, `modal-edit-services`,
  `modal-edit-component-N`, `modal-edit-sleep-mode-config` en
  `modal-edit-cross-domain-config`.
- In de create-wizard bestaat de bijlagenstap wel, maar die **vervangt** niet: dezelfde id
  opnieuw uploaden geeft daar "Er bestaat al een bijlage met de id 'cert'", en de oude
  regel blijft staan. Dat is voor een stapelstap ook logisch - je haalt hem weg en zet een
  nieuwe neer - maar het is geen vervangen.

Kortom: het vervangen zelf werkt en houdt de id vast, maar op een bestaand project lijkt
er geen schermweg naartoe. Dat is geen breuk in deze release (er is niets stuk gegaan),
wel een gat tussen wat het plan verwacht en wat het portaal aanbiedt. Eigen taak waard.

### 2. Aliassen als een blok - WERKT

In de bewerkdialoog van het component staat het aliasveld als leesbare tekst:

```
DB_HOST=$DATABASE_SERVER_HOST
DB_PORT=$DATABASE_SERVER_PORT
```

Op het scherm staat **geen** versleutelde tekst en **geen** redactiemarkering. Expliciet
gemeten op de gerenderde HTML van de dialoog:

```
BEGIN AGE ENCRYPTED FILE in HTML: False
redactiemarkering 'REDACTED'   : False
redactiemarkering '<redacted>' : False
redactiemarkering '***'        : False
```

En in de projectenrepository staat het als **een** blok, niet per sleutel:

```yaml
    aliases: |-
      -----BEGIN AGE ENCRYPTED FILE-----
      YWdlLWVuY3J5cHRpb24ub3JnL3YxCi0+IFgyNTUxOSBuUWx3TnNpUXU0bHZlQmVV
      ...
```

Eén `aliases:`-sleutel met één AGE-blok eronder. Op het tabblad Componenten worden de
aliassen gewoon leesbaar getoond (`DB_HOST -> $DATABASE_SERVER_HOST`).

### 3. Het dashboard - WERKT

"Gebruik per project" toont per project **geheugen en CPU**, met eronder de tekst
"Wat elk project gebruikt, gesorteerd op geheugen". De projectnaam ("RC113 Kijken") is
een link naar het project. Balken voor beide waarden, met absolute waarde en percentage
ernaast (`5 MiB / 0.5 GiB (1%)`, `0m / 500m cores (0%)`).

### 6. De schermen die van naam zijn veranderd - WERKT

- **Mijn projecten**: zowel het menu-item als de paginakop.
- **Services overzicht**: zowel het menu-item als de paginakop.
- Het zoek- en sorteergebied staat als één `<nldd-toolbar>` met het zoekveld in
  `slot="start"`, de sorteerknop in `slot="end"` en een `slot="overflow"` voor smalle
  schermen; het sorteermenu is een `<nldd-menu slot="popup">`.
- Het gebied ververst **als een geheel**: het formulier doet
  `hx-get="/projects"` met `hx-select="#projects-zoekgebied"` en
  `hx-target="#projects-zoekgebied"`, dus zoekterm, sorteerkeuze en resultaat komen uit
  één antwoord terug. Focusherstel loopt via `id="projects-zoekveld"` en
  `htmx-formgedrag.js`, omdat de echte `<input>` in de schaduwboom van
  `nldd-search-field` zit en `hx-preserve` daar niet werkt.

Let op: er zijn **twee** sorteeropties (Naam A-Z en Naam Z-A), niet vier. "Meeste
deployments" en "Meeste teamleden" zijn in `80da844c` bewust verwijderd. Drie toetsen
waren daar niet in meegegaan; zie taak 2.

### 7. De CLI- en Actions-pagina - WERKT

- **`/cli`**: de repositorylink ("Alle opdrachten en instellingen: zad-cli op GitHub")
  staat bovenaan, direct onder de paginatitel en boven het blok Installeren.
- **`/actions`**: dezelfde vorm ("Alle invoerwaarden en voorbeelden: zad-actions op
  GitHub") bovenaan. Het bijwerken van meerdere images in een keer staat er als een eigen
  kopje "Meerdere componenten in een keer", met een voorbeeld dat `components` gebruikt
  in plaats van `component`/`image`, en de zin "Zet je `components`, dan worden
  `component` en `image` genegeerd". Er staat expliciet bij dat het **een** uitrol is en
  niet een per component.

Kleinigheid, niet blokkerend: in het blok "Wat het is" op `/actions` staan de drie
actienamen (`deploy`, `cleanup`, `scheduled-cleanup`) links uitgelijnd terwijl hun
omschrijving rechts tegen de rand geplakt staat, met een groot gat ertussen. Dat leest
als een omschrijvingslijst die zijn opmaak kwijt is.

### 5. Een backup terugzetten - NIET GEMETEN

Dit punt is **niet** afgerond, en het is eerlijker om te zeggen waarom dan om het als een
half resultaat op te schrijven.

De opzet was er wel: persistent-storage op het component, project verwerkt, PVC gebonden
(`prod-web-data-pvc  Bound  100Mi`). Daarna liep het vast op de eigen opstelling. Voor punt
"sleep-mode" was op dit project sleep-mode aangezet met `sleep-after-deploy: 5m`, en de
deployment sliep dus (`prod-web 0/0`, waker ervoor). Om een backup te maken wilde ik hem
weer wakker hebben.

Wat ik deed werkte niet, en dat was mijn fout en geen fout van het portaal:
`DELETE /api/v2/projects/{p}/services/sleep-mode/config/project` haalt de **configuratie**
weg maar laat de **dienst** op het project staan. In het projectbestand stond `sleep-mode`
daarna nog gewoon in de dienstenlijst, en de wekkermanifesten werden dus opnieuw
gegenereerd (`web-waker-deployment.yaml`, `web-waker-config.yaml`,
`prod-web-waker-token-secret.sops.yaml`). Een project- en een deployment-refresh
veranderden daar niets aan, want er viel niets te veranderen.

Wakker maken via `POST /api/sleep-mode/{p}/{d}/wake` lukte ook niet: dat endpoint eist een
`X-Wake-Token`, die van de wekkerpagina zelf komt (`HTTP 401 X-Wake-Token header required`).
Terecht - een willekeurige aanroeper hoort een applicatie niet te kunnen wekken - maar het
betekende dat ik binnen de tijd geen wakkere deployment meer had om een backup van te maken.

Daarmee is punt 5 open blijven staan: **niet gemeten, niet aangetoond, en dus ook niet
groen gemeld.** Wat RC-111 beschreef (na een restore stond het project op slot) is in deze
doorloop dus niet opnieuw getoetst.

Wel een bijvangst die op zichzelf staat: **je kunt een dienst via de API wel toevoegen maar
niet verwijderen.** `POST /api/v2/projects/{p}/services` bestaat, een `DELETE` op datzelfde
pad niet - de enige routes zijn `get` en `post`. Dat is dezelfde soort asymmetrie als bij
het aanmaken/verwijderen van een project, en het raakt de CLI op dezelfde manier.

### Nieuw uit `ab6ae614`: sleep-mode wekt niet meer vanzelf - WERKT

Op een vers project sleep-mode aangezet **zonder `wake-mode` mee te geven**, zodat de
standaardwaarde het werk doet. Op beide plekken waar die standaard staat:

- **De API**: `/openapi.json` geeft voor `SleepModeConfig.wake-mode` nu
  `"default": "manual"` (was `auto`), met de enum `["auto","confirm","manual"]` er nog
  gewoon omheen.
- **Het formulier**: in de bewerkdialoog van sleep-mode staat
  `<option value="manual" selected>`; `auto` en `confirm` staan er als keuze naast, maar
  niet voorgeselecteerd.

Dat is precies wat `ab6ae614` beoogt, maar de standaardwaarde in een document bewijst nog
niet dat er niets meer vanzelf wekt. Daarom is het ook echt uitgevoerd: sleep-mode aan met
`sleep-after-deploy: 5m`, en gewacht tot de deployment werkelijk sliep.

```
deployment.apps/prod-web         0/0     0            0     24m
deployment.apps/prod-web-waker   1/1     1            1     6m22s
```

Daarna drie keer de publieke URL opgehaald. Onder de oude standaard `auto` had het
**eerste** verzoek de applicatie gewekt:

```
1: HTTP 200
2: HTTP 200
3: HTTP 200
na 3 GETs: replicas=0
```

De wekkerpagina antwoordt, en de applicatie blijft slapen. Ook met een echte browser
(JavaScript aan, dus de wekkerpagina kon zelf een verzoek doen):

```
ZICHTBARE TEKST: prod staat in slaapstand
                 Deze applicatie staat in slaapstand en moet door een beheerder
                 worden gestart.
                 Slaapstand - de applicatie start koud op, sessies blijven niet bewaard.
knop 'Applicatie starten': aanwezig=False
na browserbezoek: replicas=0
```

Let op bij het nameten: de wekkerpagina levert de teksten van **alle drie** de standen mee
in de HTML ("wordt gestart", "Applicatie starten", "moet door een beheerder worden
gestart"), en JavaScript kiest welke zichtbaar is. Wie op de ruwe HTML grept vindt dus ook
de knop "Applicatie starten" terwijl die niet getoond wordt. De poort is wat er
**zichtbaar** is, plus het aantal replicas erna.

## Taak 5 - De API-weg en de documentatie

### De doorloop met curl tegen `/api/v2`

Alle stappen op `e187015e`, tegen het draaiende cluster. Elke asynchrone stap is
afgewacht op zijn **taak**, niet op een klok.

| Stap | Aanroep | Uitkomst |
|---|---|---|
| Aanmaken | `POST /api/v2/projects` | 202, `project_name=rd-xyt`, taak `completed` |
| Opvragen | `GET /api/v2/projects/rd-xyt` | 200 |
| Dienst | `POST /api/v2/projects/rd-xyt/services` (`postgresql-database`) | 202, taak `completed` |
| Deployment | `POST /api/v2/projects/rd-xyt/:upsert-deployment` (`prod`) | 202, taak `completed` |
| Component | `POST /api/v2/projects/rd-xyt/components` (`web`, gekoppeld aan `prod`) | 202, taak `completed` |
| Opvragen | `GET /api/v2/projects/rd-xyt` | component `web` + deployment `prod` staan erin |
| Verwijderen | `DELETE /api/projects/rd-xyt` | 200, alle 17 opruimstappen `success` |

De uitrol is ook echt op het cluster gecontroleerd en niet alleen op het antwoord:

```
namespace  rig-rd-xyt              Active
pod        prod-web-...            1/1 Running   0 herstarts
argocd     rd-xyt-prod             Synced  Healthy
```

Na het verwijderen was de namespace weg.

### Bevinding 1 - `/openapi.json` noemt de verkeerde beveiliging voor het aanmaken

**Dit is de bevinding die de CLI raakt.**

`POST /api/v2/projects` is het enige endpoint dat geen projectsleutel kan gebruiken -
het project bestaat nog niet. De code eist daarom een SSO-token
(`@validate_user_token`, `Authorization: Bearer <token>`), en de docstring van het
endpoint legt dat ook netjes uit.

Het OpenAPI-document zegt iets anders:

```
$ jq '.paths["/api/v2/projects"].post.security' openapi.json
[ { "APIKeyHeader": [] } ]

$ jq '.components.securitySchemes | keys' openapi.json
[ "APIKeyHeader" ]
```

Er staat maar één beveiligingsschema in het hele document, en dat wordt aan alle
**95** v2-operaties gehangen, inclusief deze. Een bearer-schema komt in het document
niet voor. Wie zich op het document baseert - en dat is precies wat een gegenereerde
client doet - stuurt `X-API-Key` en krijgt:

```
HTTP 401 {"detail":"Authentication required - provide a valid Authorization: Bearer token"}
```

Empirisch bevestigd: met de `ADMIN_API_KEY` in de header geeft dit endpoint 401, met
een `zad-cli`-token met `aud: zad-api` geeft het 202.

Het is een fout in het **document**, niet in het gedrag: de API doet precies wat hij
hoort te doen. Maar het document is hier de machineleesbare afspraak, en die klopt niet.

### Bevinding 2 - aanmaken zit op v2, verwijderen niet

`POST /api/v2/projects` bestaat; `DELETE /api/v2/projects/{project_name}` niet. Het
verwijderen zit op de oude route `DELETE /api/projects/{project_name}`. Wie de v2-API
afloopt vindt geen manier om een project op te ruimen. Werkt allemaal, maar de
levenscyclus staat op twee plekken.

Kleinigheid daarbij: die DELETE eist een body (`{"confirmDeletion": true}`) en geeft
zonder body een 422 met `loc: ["body"]`. Correct, maar niet te raden zonder het
document erbij.

### De toegestane waarden in `/openapi.json` (nieuw in deze release)

Dit is het stuk dat expliciet getoetst moest worden, en het klopt.

**Een vaste keuzelijst krijgt een echte `enum`.** Niet alleen een zin in de
beschrijving. Bijvoorbeeld `SleepModeConfig.wake-mode`:

```json
"enum": ["auto", "confirm", "manual"],
"x-choices": [
  {"const": "auto",    "title": "Automatisch", "description": "Wekt bij het eerste bezoek; ..."},
  {"const": "confirm", "title": "Met bevestiging", ...},
  {"const": "manual",  "title": "Alleen handmatig", ...}
]
```

**Een veld waarvan de keuzes per project verschillen krijgt géén verzonnen `enum`,
maar een machineleesbare verwijzing.** 25 keer in het document, bijvoorbeeld
`SleepModeConfig.waker-component`:

```json
"x-choices-source": {
  "endpoint": "GET /api/v2/projects/{project_name}/components",
  "path": "components[].name",
  "description": "De componenten van dit project. ..."
}
```

Er staat dus een endpoint en een pad naar de waarden - genoeg om een client de lijst
zelf te laten ophalen. Dat is precies wat het plan vroeg.

Alle 12 velden met `x-choices` zijn nagelopen, met `$ref`/`anyOf` opgelost:

- waar een `enum` bestaat, is die **exact gelijk** aan de `x-choices` (4 velden:
  `provide-as`, `scheme`, `account-link`, `tls`, `wake-mode`);
- waar geen `enum` staat, accepteert het configmodel ook werkelijk vrije tekst
  (`domain-mode` is `str | None` en legacy, `keycloak/template` is een bestandsnaam
  die op schijf bestaat, de duur- en maatvelden zijn vrije Kubernetes-quantities).
  De lijst is daar een suggestie en geen grens, en dat is een verdedigbaar verschil.

### Het formulier tegenover het configmodel

Het plan zegt: wijkt een keuzelijst in het formulier af van wat het configmodel
toestaat, dan is **dat** de bevinding. Dat was bij de eerste meting op `418533e5`
precies het geval, en het is de reden dat deze doorloop opnieuw moest:

- `NamespacePostgresConfig.storage` had `default: "10Gi"`, terwijl de keuzelijst
  `["50Mi","100Mi","250Mi","500Mi","1Gi"]` is. De standaardwaarde stond dus niet in
  de lijst waaruit je hem kon kiezen, en het document sprak zichzelf tegen.

`e187015e` repareert dat. Opnieuw gemeten op het draaiende cluster, met een controle
die per veld de `default` tegen de `x-choices` legt:

```
velden met x-choices: 12   OK: 12   PROBLEEM: 0

default-language     default='nl'
template             default='sso-only'
storage              default='1Gi'      <- was 10Gi
sleep-after-deploy   default='48h'
sleep-after-wake     default='1h'
wake-mode            default='auto'
```

Elke standaardwaarde staat nu in zijn eigen keuzelijst. De commit zet er bovendien een
test op, zodat het niet stilletjes terug kan komen.
