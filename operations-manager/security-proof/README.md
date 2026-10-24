# ZAD Attack Proxy: de aanvalsproef op onze eigen sandbox

Dit is een **verdedigingsinstrument**. Het legt een instantie van de Operations Manager (OPI)
onder bekende aanvalspatronen en faalt zodra OPI zwicht: een route zonder herkenning, een
upload die in het geheugen landt, invoer die op een argumentenlijst terechtkomt, een
foutmelding die te veel zegt, of een stortvloed die de pod plat legt.

Elke run levert een **groene lijst**: per laag staat elke regel die gemeten is erop met zijn
uitkomst, en elke afwijking staat erop met een uitleg (comply or explain). Een afwijking zonder
uitleg laat de run falen, ook als het hulpmiddel zelf het een waarschuwing noemt; een uitleg
die niets meer dekt laat de run ook falen.

## De twee doelen die de code kent

| doel | wat het is | hoe |
|---|---|---|
| **lokaal** | een wegwerp-instantie die dit harnas zelf opzet en afbreekt, op `127.0.0.1:8311` | `task -x security-proof` |
| **sandbox** | de geclaimde sandbox in het kind-cluster, met de echte keten: ingress, Keycloak, rig-db, object storage, registry | `task security-proof:sandbox`, dan `task -x security-proof` |

Staging is geen doel van de code: dat is een afgestemde handrun met zijn eigen runbestand, zie
`features/aanvalsproef-staging.md`. Productie is nooit een doel, ook niet via de grendel.

## Draaien

```bash
task -x security-proof         # alles: sandbox op, beproeven, rapport, sandbox af
task security-proof:up         # alleen de wegwerp-sandbox, op http://127.0.0.1:8311
task security-proof:fuzzers    # de gerichte proeven tegen een al draaiende sandbox
task security-proof:down       # de wegwerp-sandbox afbreken
task security-proof:sandbox    # maak de geclaimde sandbox het doel van deze run
task security-proof:sandbox-af # haal het proeftoken uit de sandbox en gooi het runbestand weg
```

De proef kan als gate draaien, en zegt met zijn exitcode wat eruit kwam:

| code | betekent |
|---|---|
| `0` | groen: alles is gemeten en er is geen bevinding buiten `bekende-bevindingen.tsv` |
| `1` | een bevinding: een gerichte proef faalde, of ZAP zag iets dat niet in de baseline staat |
| `3` | NIET GEMETEN: een laag kon niets beproeven (geen docker, een onbereikbaar doel, pytest zelf omgevallen). Geen uitspraak, dus ook geen groen |

Een bevinding gaat voor op NIET GEMETEN: is er een bevinding en kon daarnaast een laag niets
meten, dan is de code `1` en staan beide in de uitvoer en in het rapport.

De `-x` hoort erbij: `task` geeft zonder die vlag elke mislukte taak als zijn eigen 201 terug,
en dan zijn `1` en `3` niet te onderscheiden (niet-nul blijft het wel, dus NIET GEMETEN leest
nooit als groen). Draait de gate de module rechtstreeks, dan komen de codes er ook uit:
`cd operations-manager/python && uv run python -m security_tests.proef`. Het rapport komt in
`report/aanvalsproef.md` (git-ignored) en noemt per bevinding de BIV-as, de componenten die
hij raakt, en het commando om hem na te doen.

Een enkele proef los draaien, met de sandbox omhoog:

```bash
cd operations-manager/python
uv run pytest security_tests/test_auth_dekking.py -q
```

Zonder draaiende sandbox slaan de proeven zichzelf over met de reden; ze verzinnen geen doel.

## De grendel

`security_tests/doel.py` is de enige plek waar een doel-URL vandaan komt, en hij staat op drie
sloten:

1. de URL komt uit het runbestand dat `:up` of `:sandbox` schrijft, niet uit een argument of
   een env-var;
2. de host staat op de vaste lijst: de eigen machine (`127.0.0.1`, `localhost`, `::1`) of een
   host op `sandbox.rijksapp.dev`. Alles daarbuiten is een directe afbreek, zonder dat er een
   verzoek naartoe gaat. Op naam en niet op bereikbaarheid, dus een IP-adres van de sandbox
   komt er niet door, en `zad.sandbox.rijksapp.dev.aanvaller.example` ook niet;
3. de instantie moet het token van dat runbestand terugkaatsen in de header `X-Zad-Proef`. Dat
   token wordt per run gemunt. De wegwerp-sandbox zet die header op elk antwoord; een sandbox
   in het cluster doet het op `/health`, en alleen als `SECURITY_PROOF_TOKEN` gezet is.

### Het proeftoken in het cluster

`SECURITY_PROOF_TOKEN` (`opi/core/config.py`) maakt een instantie een doel van de proef, en OPI
**weigert te starten** met een gezet token op elk cluster behalve `sandboxed-local`. Het token
staat nergens in git: `task security-proof:sandbox` munt het per run en patcht het in het
env-secret `operations-manager-env-secrets`, dat geen ArgoCD-eigenaar heeft en dus niet uit git
komt. `task security-proof:sandbox-af` maakt het weer leeg en haalt het runbestand weg.

Wat er op het sandboxdoel te meten valt, hangt af van wat de run meekreeg: zonder
`ZAD_PROEF_SLEUTELS` (JSON van project naar API-sleutel) en `ZAD_PROEF_SESSIE_SECRET` plus
`ZAD_PROEF_ADMIN` slaan de proeven die een sleutel of een sessie nodig hebben zich over met de
reden, en staan ze als NIET GEMETEN in de groene lijst.

`uv run python -m security_tests.doel <url>` is de grendel als commando: niet-nul en een
melding zodra het doel niet mag. `security_tests/test_grendel.py` bewijst dat voor een reeks
externe adressen, inclusief dat er geen enkel verzoek wordt verstuurd.

## Wat de wegwerp-sandbox is

De echte FastAPI-app met de nagemaakte buitenwereld uit `tests/e2e/testserver.py`: geen
database, geen cluster, geen Keycloak. Daar komt bij wat een aanvalsproef nodig heeft:

* **Nagebootste binaries** in de PATH (`bin/`): `kubectl`, `sops`, `git`, `mc`, `kopia` en
  `skopeo` zijn symlinks naar `bin/_recorder.py`, dat zijn argumentenlijst wegschrijft en
  niets doet. Zo is te meten wat er werkelijk op een commandline terechtkomt, zonder dat er
  een cluster of een repo wordt geraakt.
* **Twee proefprojecten** met elk een eigen, per run gemunte `X-API-Key`: `proef-alfa` voor de
  gerichte proeven, `proef-beta` voor de brede fuzz. Zo is isolatie tussen projecten echt te
  beproeven (de sleutel van A mag niets in B).
* **Drie rollen** met een ondertekende sessie: een projectlid (admin van beide proefprojecten),
  een ZAD-admin, en een lid met rol `developer`. Die laatste is er voor de rolpoort op de
  geheimen van een project: een developer mag de privesleutel van zijn project niet zien, een
  admin wel.

Er staat geen sleutel in de repo: alles wordt per run gemunt en in het runbestand gezet, dat
git-ignored is.

## Wat het rapport zegt over een bevinding

Een assertie van een proef zegt WAT er niet klopte, niet waarom OPI zwichtte. Het rapport zet
daarom per bevinding de logregels van OPI erbij: de route die omviel, de traceback, en de
regels eromheen, met hun tijd. Die regels komen uit `.proef/opi.log`, het logboek dat
`security_tests/sandboxlog.py` tijdens de run opvangt.

Dat logboek is apart nodig omdat de sandbox in `task -x security-proof` **in hetzelfde proces
als de gate** draait: zonder opvang gaat zijn log naar de stdout van de gate en is hij daarna
niet meer per proef te scheiden. Elke proef markeert daarom zijn eigen venster in het logboek
(`pytest_runtest_logstart`/`logfinish` in `security_tests/conftest.py`), en het rapport leest
per bevinding alleen de regels uit dat venster. Elke regel gaat door
`opi/utils/logging_redact.py`, dus een sleutel, een wachtwoord of een uitnodigingssleutel in
een logregel staat gemaskeerd in het rapport.

Het pad van een verzoek komt van buiten, dus het logboek houdt het op een regel: de twee
herkenningen (de markering en de ASGI-fout) staan vast aan de hele kopregel, met tijd, niveau
en logger erbij. Anders schrijft een pad met een regeleinde zijn eigen kopregel, en leest die
als een proefvenster of als een bevinding die geen enkel verzoek veroorzaakte.

Valt de proef zelf om in plaats van een uitslag te geven (pytest eindigt op een andere code dan
0 of 1), dan staat dat in het rapport als crash, met de exitcode en de laatste logregels van
OPI ervoor.

### Exception in ASGI application

Zo'n regel van uvicorn hoort niet stil in de uitvoer te blijven staan, dus het rapport doet er
per keer een uitspraak over: **een bevinding**, of **de verwachte uitkomst van een
wegwerp-sandbox**. Verwacht is hij alleen als elke exceptievorm in de traceback hoort bij een
dienst die er in de wegwerp-sandbox niet is (`VERWACHTE_OORZAKEN` in `sandboxlog.py`:
`socket.gaierror`, `ConnectionRefusedError`, `InvalidCatalogNameError`, al dan niet in een
`ExceptionGroup`). In de praktijk is dat de database: een poging die de rate limiter voorbij
komt zoekt een subdomein op, en die database bestaat daar niet.

Alles daarbuiten is een bevinding, ook voor de exitcode: dan viel OPI om een andere reden om,
en dat is precies wat de proef zoekt.

## De proeven

| bestand | wat het beproeft | faalconditie |
|---|---|---|
| `test_grendel.py` | de grendel zelf | een doel buiten de eigen machine wordt beproefd |
| `test_auth_dekking.py` | elke route uit de OpenAPI, naamloos aangeroepen | een route die niet bewust publiek is, weigert niet |
| `test_uploads.py` | de bijlage- en image-routes | een te grote body wordt aangenomen, of de pod groeit |
| `test_injectie.py` | metatekens en pad-escapes via de API | een payload bereikt een argumentenlijst |
| `test_web_payloads.py` | XSS, CSRF, de rolpoort op projectgeheimen, en wat een foutmelding prijsgeeft | een payload komt ongeescapet terug, een mutatie zonder token slaagt, een pagina lekt of hangt, of een developer ziet de privesleutel van zijn project |
| `test_stortvloed.py` | foute sleutels en een ruwe stortvloed | elke poging wordt vol verwerkt, of de pod valt om |
| `test_sql_injectie.py` | booleaanse SQL-varianten op het filter van de takenlijst, met de sleutel van een proefproject | een variant geeft andere taken, een 5xx, of taken van een ander project |
| `test_schemathesis.py` | property-based verzoeken uit `/openapi.json` | een vreemde komt door, of een antwoord schendt zijn eigen schema |
| `test_zap.py` | de lezing van het ZAP-rapport | een bevinding boven de drempel valt weg |
| `test_sandboxdoel.py` | wat alleen in het cluster te meten is: het client-IP achter de ingress, en Keycloak, de registry, de object storage en ArgoCD vanaf de gebruikerskant | een vervalste `X-Forwarded-For` verschuift de limiter, of een dienst geeft een vreemde antwoord |

Elke proef heeft een **tegenproef of een negatief voorbeeld** naast zich: een kwetsbare
stand-in app, een verzonnen antwoord, of een route die de weigering wel heeft. Zonder die
tegenproef zou "geen bevinding" ook kunnen betekenen dat de proef niets kan zien.

### Het geheugenbeeld van de proefsandbox

`test_uploads.py` meet of de pod groeit van een geweigerde upload. Dat beeld komt via
`ps -o rss= -p <pid>` uit `conftest.py` en niet uit `/proc`, want macOS heeft geen `/proc`.
Geeft `ps` hier niets, dan slaat die proef zich over met de reden ("NIET GEMETEN"); een meting
die op dit platform niet kan is geen bevinding.

Dat geldt ook voor de **sessie** van een ingelogde sweep. `/auth/logout` haalt de gebruiker uit
de inloglijst en trekt zijn sessies in, dus een sweep die die route aanroept meet daarna de
anonieme oppervlakte: alles wat erna komt wordt een omleiding naar `/auth/login` en geen enkele
5xx of lek is nog te zien. De sweep slaat die routes daarom over (`SESSIEVERNIETIGERS`) en
controleert voor en na de lus dat `/dashboard` nog 200 geeft.

Dezelfde sessie kan ook op de **vorm van het koekje** sneuvelen. De wegwerp-sandbox serveert
http, en OPI zet zijn sessiekoekje terecht op `Secure`. Zodra een route in de sessie schrijft
(`POST /account/otp/start` bijvoorbeeld) herschrijft `SessionMiddleware` het koekje, en een
client stuurt een `Secure`-koekje niet over http terug. Daarom gaan alle aanroepers via
`proefclient()` in `conftest.py`, dat `Secure` van een koekje van de sandbox haalt. Dat is een
eigenschap van de proefopstelling, geen uitspraak over de koekjespolitiek van OPI.

### Een geheim in een antwoord: wie het opvraagt telt mee

`beoordeel_antwoord` beoordeelt wat **geen** aanroeper mag zien: een stacktrace, een pad uit de
repo. Een projectgeheim staat daar met opzet niet bij. De detailpagina van een project zet de
privesleutel, de API-key en de Keycloak-wachtwoorden in de HTML voor een admin of owner; dat is
de rolpoort `PROJECT_EDIT_ROLES` in `opi/web/router.py` en geen lek. Daarom meet
`test_de_sleutels_van_een_project_blijven_achter_de_rolpoort` het **verschil** tussen de rollen
in plaats van de aanwezigheid van de sleutel: de admin ziet hem (anders kan de proef niets zien),
de developer niet.

### De ZAP-basislaag

`security_tests/zap_scan.py` draait OWASP ZAP (als container) in drie passen tegen de
sandbox: ongeauthenticeerd, als projectlid en als ZAD-admin, met de OpenAPI als zaad. Zonder
docker meldt die laag **niet gemeten** met de reden, in plaats van groen te zeggen. De lezing
van het rapport en de vergelijking met de baseline worden wel zonder docker getoetst.

Een pas die geen rapport schrijft is niet zonder meer een pas die niets zag, dus de laag zegt
erbij waarom: de hele uitvoer van ZAP staat in `report/zap-<pas>.log`, want ZAP schrijft zijn
diagnose en een stacktrace uit zijn eigen netwerklaag op stdout. Dat de container het doel wel
zag staat dan al vast, want `verifieer_containerdoel()` liep voor de eerste pas.

ZAP schrijft in de container als zijn eigen gebruiker, dus zijn rapport staat in `report/zap/`
en alleen die map gaat wereld-schrijfbaar open. De log, de junit en `aanvalsproef.md` blijven
erbuiten: in een open map volgt een write de symlink die een andere lokale gebruiker op een nog
vrije naam neerlegt, en de sticky bit houdt alleen een vervanging tegen.

Elk rapport gaat weg voor zijn pas begint. Een rapport van een vorige run, of een rapport dat
een andere lokale gebruiker in die open map neerlegt, zou anders als de meting van nu lezen
terwijl ZAP niets schreef. Kan dat rapport niet weg, dan is dat NIET GEMETEN.

#### Hoe de container bij de sandbox komt

De sandbox luistert met opzet alleen op `127.0.0.1`: een instantie die we bewust aanvallen
hoort niet op het netwerk te staan. Een container komt daar per platform anders bij, en
`containerdoel()` is de enige plek waar dat verschil staat:

* **Linux**: `--network host`, en dan is `127.0.0.1:8311` in de container de sandbox zelf.
  Host-netwerk is een voorziening van Linux, dus dit is de enige plek waar het werkt.
* **Docker Desktop (macOS, Windows)**: daar is `--network host` geen host-netwerk, dus
  127.0.0.1 is de container zelf en ZAP viel om in `org.zaproxy.addon.network`. Daar wordt
  **alleen de host** vertaald naar `host.docker.internal`, op dezelfde poort en hetzelfde
  pad. Docker Desktop zet die naam door naar de host, inclusief diensten die alleen op de
  loopback van de host gebonden zijn.

Dat is geen aanname: voor de eerste pas haalt `verifieer_containerdoel()` van binnen de
container `/health` op en vergelijkt de header `X-Zad-Proef` met het token uit het runbestand.
Komt dat token niet terug, dan draait er geen enkele pas en meldt de laag NIET GEMETEN met de
reden. De grendel uit `doel.py` blijft onaangeroerd op de loopback; de vertaling geldt alleen
voor de container, en het token moet er ook daar terugkomen.

## De groene lijst

Het rapport bevat per laag een tabel met elke regel die gemeten is en wat eruit kwam:

| uitkomst | betekent |
|---|---|
| groen | gemeten, en in orde |
| afwijking met uitleg | gemeten, wijkt af, en staat met een reden in `bekende-bevindingen.tsv` |
| bevinding | gemeten, wijkt af, en er is geen uitleg: de run faalt |
| niet gemeten | de laag of de toets kwam er niet aan toe, met de reden erbij |

Een overgeslagen toets staat er dus als "niet gemeten" met de reden van de skip, en niet als
groen: dat is het verschil tussen een proef die iets beproefd heeft en een proef die er niet aan
toe kwam. De regels komen uit het junit-rapport van de lagen zelf en uit de alerts van ZAP, dus
er is geen tweede lijst die kan afwijken van wat er gedraaid is.

### De WARN's van ZAP

Elke alert van ZAP vanaf riskcode 1 (WARN) is een bevinding, tenzij hij in de baseline staat:
proefnaam `zap-<pas>` en kenmerk `<pluginid>:<route>`, met het pad en niet de volledige URL, zodat
dezelfde regel geldt achter een ingress. Een INFO (riskcode 0) blijft informatie en staat in de
lijst als zodanig.

Dat was eerder anders: de drempel stond op risico midden, dus een WARN liet de gate niet falen
en stond nergens als geaccepteerd. Dat is precies de stille waarschuwing die de groene lijst
uitsluit.

## Bekende bevindingen

`bekende-bevindingen.tsv` is de ingecheckte lijst met wat we nu accepteren, per proef, per doel
en per kenmerk, met de reden en de bron. Twee richtingen:

* een bevinding die **niet** in de lijst staat laat de proef falen;
* een regel die **niets meer dekt** laat de proef ook falen, met "OPGELOST: haal de regel
  weg". Zo loopt de lijst niet vol met regels voor problemen die al verholpen zijn.

De kolom `doel` begrenst een regel tot een soort doel: leeg geldt op elk doel, `lokaal` alleen
in de wegwerp-sandbox en `sandbox` alleen tegen een geclaimde sandbox in het cluster. Een reden
als "draait niet in de wegwerp-sandbox" staat dus op `lokaal`, en neemt tegen de cluster-sandbox
geen bevinding weg en levert daar ook geen OPGELOST op.

Wat hier bij komt, is wat we bewust open laten staan: dat is de plek waar een reviewer naar
kijkt.

## Een nieuwe aanvalsproef toevoegen

1. Zet de proef in `operations-manager/python/security_tests/test_<onderwerp>.py`, met de
   faalconditie in de docstring en een fixture uit `conftest.py` als aanroeper (`anoniem`,
   `projectlid`, `zad_admin`, of `sleutelclient(doel, project)`). Zo loopt hij langs de
   grendel.
2. Schrijf de beoordeling als een **losse functie** die een status of een antwoord beoordeelt,
   en toets die functie op een negatief voorbeeld. Een proef zonder tegenproef kan stil groen
   staan.
3. Zet het bestand in `PROEVEN` in `security_tests/rapport.py`, met de BIV-as, de componenten
   uit `architecture/biv-inventaris.yaml` die hij raakt, en de impact in één regel. Het rapport
   en de dekkingstabel lezen die koppeling daar.
4. Is de bevinding nu nog echt aanwezig en wordt hij elders opgelost? Zet hem in
   `bekende-bevindingen.tsv` met de reden en de bron, zodat de gate groen staat op wat we
   weten en rood op wat nieuw is.

## Grenzen

* Dit harnas beproeft **OPI en de platformmechanismen**, niet de applicaties van afnemers en
  niet de onderliggende infrastructuur, SSO-Rijk of de third-party diensten. Hoe OPI daarmee
  koppelt is wel aanvalsoppervlak, en dat wordt met de nagebootste binaries gemeten.
* In de wegwerp-sandbox is er **geen database, geen cluster, geen registry en geen Keycloak**.
  Een route die daarop leunt valt daar om, en dat is geen uitspraak over zijn veiligheid. Voor
  de herkende aanroeper wordt daarom het responsschema en een lek beoordeeld, niet de 5xx;
  voor de naamloze of vreemde aanroeper juist wel, want die hoort de applicatie nooit te
  raken.
* De vier voortgangsroutes (`.../progress/{task_id}`, `.../task-progress/{task_id}`) zoeken de
  taakdienst op en geven in de wegwerp-sandbox 503. Ze staan daarom in
  `bekende-bevindingen.tsv`: 503 is daar de goede uitkomst en zegt niets over hun veiligheid.
* De gedeelde sandbox is wel een doel, en wordt per run geclaimd; staging en productie niet.
  Een stagingrun is een afgestemde activiteit met een venster en een aanspreekpunt, buiten
  deze code, en de grendel houdt hem daar: `features/aanvalsproef-staging.md`.
* In de wegwerp-sandbox is er geen database, dus de routes die er een opzoeken (de vier
  voortgangsroutes en `POST /step-up`) geven daar 500 of 503. Tegen een sandbox in het cluster
  zijn dat wel echte metingen, en daarom staan die zes baselineregels op doel `lokaal`: daar
  nemen ze geen bevinding weg.
