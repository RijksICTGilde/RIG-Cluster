# De tweede generale: alles opnieuw meten na de mergeschade

Doorloop van `de-tweede-generale-alles-opnieuw-meten-na-de-merge` voor de merge naar `main`.

- **Gemeten commit**: `33468107` (de taaktip bij aanvang); de reparatie uit taak 3 staat als
  `e0d10d8f` erbovenop en is apart getoetst
- **Cluster**: `kind-rig-sandbox`, `sandboxed-local`, één node (4 vCPU, 16 GiB, pod-cap 110)
- **Datum**: 16 augustus 2026

De aanleiding staat in het plan: bij een merge is een deel van een reparatie verdwenen zonder
dat iets het merkte. De htmx-omzetting van de goedkeuringsdialoog viel weg, de unittests bleven
groen, en alleen de browsertests zagen het. Deze doorloop herstelt dat vertrouwen met metingen.

## Oordeel

**Deze tak vraagt eerst een review, en daarna een verse testronde door iemand anders.**

Er zijn acht fouten gevonden en gerepareerd, waarvan zes in de code. Dat is te veel
verandering om deze doorloop zelf als eindkeuring te laten gelden: de suites die hier groen
staan zijn gedraaid op tussenliggende versies, niet allemaal op de eindtip. De weg vooruit is
dus review -> merge-beslissing -> een nieuwe doorloop door een andere sessie, op een tak die
niet meer beweegt.

Alle acht gevonden fouten zijn gerepareerd, elk met een test die omvalt als je de fix
terugdraait. **Elke geautomatiseerde suite is groen gemeten** -- inclusief de sandboxsuite en
inclusief `-m reallife` en `-m punt14` gelijktijdig zoals het plan vroeg -- maar op
`4f483796`, dus vóór de merge met de basistak. Op de eindtip `eac1fc1c` staan de unitsuite
(9099 groen) en de statische poorten; de clustersuites vragen de verse ronde hierboven.

Taak 2 is deels gemeten: van de 47 voorbeeldprojecten zijn er **11 geldig gemeten** (4 healthy,
1 omgevingsgrens, 6 getroffen door een infrastructuurstoring). De metingen daarna zijn
weggegooid omdat een andere PR de sandbox overnam en er een andere image ging draaien -- zie
"Wat deze doorloop over zichzelf leerde".

Wat er WEL staat: de unitsuite en beide browsersuites zijn groen, en elk van de tien punten
uit taak 3 is apart aangetoond in plaats van aangenomen — de goedkeuringsdialoog inclusief
goedkeuren én afwijzen door de hele keten heen, tot aan de ingress die van adres wisselt. Vier
fouten die niet in het plan stonden zijn gevonden en gerepareerd; ze staan hieronder met zoveel
woorden, elk met een test die omvalt als je de fix terugdraait.

Een noot bij het plan: de vier "bekende rode" die het noemt bestaan niet op deze tak.
`tests/test_taken_voortgang_link.py` staat er niet en wordt niet gecollect. Er is dus niets
gemeld als bekend rood — de 9007 groene tests zijn de hele unitsuite.

---

## Bevinding vijf: een vals conflict in de domeinwizard

**Opgelost in `4f483796`, en nagemeten op het draaiende cluster.**

Bij het opslaan in de meerstaps domeinwizard: *"Project 'X' is gewijzigd sinds je begon met
bewerken, en die wijziging raakt hetzelfde onderdeel als dat van jou."* Opnieuw openen helpt
niet.

| meting | uitkomst |
|---|---|
| domeinwizard, project ZONDER versleuteld veld | slaat gewoon op |
| hetzelfde project, na EEN `user-env-vars` | conflict, elke keer |
| gewone projectgegevens-bewerking op dat versleutelde project | slaat gewoon op |
| bestand 45s stil, HEAD voor en na identiek | conflict toch |

Uitgesloten met metingen: er is geen concurrent schrijver; `read_version(version_of(pad))` en
`_read_committed()` geven in rust hetzelfde; de transient-strip werkt aantoonbaar (alle drie de
paden gezien en verwijderd); `base-domain:custom` staat in geen enkele commit; en een unittest
die twee saves uit een ProjectManager naspeelt geeft ook op de ongefixte code geen conflict.

Wat een tijdelijke diagnoseregel in `_reconcile_with_concurrent_write` wel opleverde:

```
base->current: type_changes root: dict -> ruamel.yaml.comments.CommentedMap
base->ours   : type_changes root['deployments'][0]: CommentedMap -> dict
  old_value bevat 'base-domain:custom'
```

De node diff't als TYPEWISSEL, dus als vervanging-in-zijn-geheel, en zo'n delta kan de
driewegmerge niet toepassen. Daarnaast draagt `base` een transient veld dat git nooit heeft
gezien.

**De oorzaak, uiteindelijk gevonden door de gemeten vorm exact na te bouwen.** ``theirs``
komt uit ``_read_committed`` en is een ruamel ``CommentedMap``; ``base`` kan dat ook zijn (de
cache na een YAML-herlaad) en droeg bovendien het transiente veld; ``ours`` is de
wizard-uitvoer, een platte ``dict``. Voor ``==`` maakt dat niets uit (``CommentedMap`` is een
dict-subklasse), maar **DeepDiff ziet het als ``type_changes`` op de root en stopt met
afdalen**: de hele wijziging wordt een vervanging-in-zijn-geheel, en zo'n delta verifieert
zijn oude waarde tegen ``theirs`` -- elk verschil laat hem weigeren. ``DeltaError`` -> ``None``
-> ConflictError, permanent, want de vormen veranderen nooit. Het versleutelde veld was de
trigger (dat dwingt de YAML-herlaadde cachevorm af), niet de oorzaak. Twee eerdere losse
repro's misten allebei de combinatie; met base als CommentedMap-met-transient en ours als
platte dict reproduceert het deterministisch, zonder cluster.

De reproductie liet ook zien dat het breder was dan de wizard: een NIET-overlappende
gelijktijdige wijziging (de ander een namespace, wij een domein) werd door de vergroving ook
een conflict -- precies het geval waarvoor deze structurele merge boven een git-merge is
verkozen.

**De fix** (`4f483796`, in de `ProjectStore` -- de enige waarheid; de manager leest alleen):
``_as_plain`` normaliseert de diff-invoer naar kale containers (dict-subklassen -> dict,
ruamel-scalars -> str/int/float), zodat de diff granulair blijft. Alleen de vorm wordt
gelijkgetrokken, geen enkele waarde: een echte botsing op hetzelfde veld blijft een conflict,
en `tests/test_store_merge_containervormen.py` staat vooral op die grenzen. Met de fix
teruggedraaid vallen precies de drie tests om die deze weg dekken; de 373 bestaande store-,
conflict- en mergetests blijven groen.

**Nagemeten op het cluster** (`4f483796`), op het zwaarst toegetakelde project en op het
project met versleutelde env-vars:

```
e2e71-jqm:  conflict=False | commit 2f0e0a9c->cb156198 | domein in bestand | transient niet gelekt
rc118-tls:  conflict=False | commit e9a74bbc->21387c13 | domein in bestand | transient niet gelekt
```

Onderweg is een andere fix in deze laag geprobeerd (ciphertext-uitlijning), gecommit,
uitgerold en weer teruggedraaid omdat het model de waarneming van de eigenaar tegensprak dat
dit op productie niet optreedt. De les staat er hier bij omdat hij de goede was: in een
chokepoint snijd je niet zonder reproducerende test.

---

## Taak 1 — de geautomatiseerde suites

De tabel hieronder is gemeten op `4f483796`, met de fixes aantoonbaar in de draaiende pod --
geverifieerd door de container zelf te bevragen, niet via `/version`.

**NA de merge met de basistak (`2f9ed46f`) is alleen de unitsuite hermeten**, op `eac1fc1c`:
**9099 passed, 7 skipped, 0 rood** in 10m34s (was 9036; de merge brengt er 63 mee). Samen met
`ruff check` (schoon), `ruff format --check` (schoon) en `pyright` (0 errors) zijn dat de
poorten die op de eindtip staan.

De clustersuites zijn NIET hermeten. De sandboxsuite is op `eac1fc1c` wél gestart, met
`/version` bevestigd, en na 2/69 groen bewust afgebroken: de volledige testronde wordt door
een aparte sessie gedaan op een tak die niet meer beweegt, en die ronde twee keer draaien
kost anders twee uur cluster voor niets.

| suite | uitslag | duur |
|---|---|---|
| `uv run pytest tests/ -q` | **9036 passed, 7 skipped, 0 failed** | 7m51s |
| `uv run pytest -m e2e -q` (1e) | **436 passed, 67 skipped, 1 xpassed, 0 failed** | 12m28s |
| `uv run pytest -m e2e -q` (2e) | **436 passed, 67 skipped, 1 xpassed, 0 failed** | 13m22s |
| `uv run pytest -m sandbox -q` | **66 passed, 1 xfailed**, 1 rood -> bevinding 6, daarna groen | 1h16m |
| `uv run pytest -m reallife -q` (gelijktijdig) | **7 passed, 1 xfailed, 0 failed** | 20m05s |
| `uv run pytest -m punt14 -q` (gelijktijdig) | 1 rood -> bevinding 6 | 11m04s |
| `uv run pytest -m punt14 -q` (na de fix) | **4 passed, 0 failed**, nul ReadTimeouts | 34m48s |

De unitsuite ging van 9007 naar 9036: +29 door de tests bij de fixes, en na de merge met de
basistak naar 9099.

De ene rode in de sandboxsuite en in de gelijktijdige punt14-run is dezelfde, en is bevinding
6 hieronder. Na de fix is `-m punt14` opnieuw gedraaid en volledig groen.

Het plan vroeg `-m reallife` en `-m punt14` GELIJKTIJDIG (de RC-112-vorm). Dat is zo gedaan,
als aparte processen naast elkaar. De 12 tests dragen ook de `sandbox`-marker en lopen dus al
mee in de suite, maar daar sequentieel; de gelijktijdige run is de meting met echte druk --
en die druk bracht bevinding 6 aan het licht.

De unitsuite is gedraaid met de eigen standaardaanroep, zonder eigen `-m`.

**Twee e2e-runs, twee keer hetzelfde beeld.** Dat is de reden dat het plan er twee vroeg: er
is deze week een geval geweest van twee tests die een browsersessie deelden. Beide runs geven
exact dezelfde aantallen en dezelfde ene `xpassed`. Geen wisselvalligheid gevonden.

**Over de "bekende rode".** Het plan meldt vier rode in `tests/test_taken_voortgang_link.py`
als onafgemaakt werk van een andere sessie. Dat bestand staat niet op deze tak; het wordt niet
gecollect en er is dus ook geen rood om te melden. De 9007 groene tests zijn de hele suite.

---

## Taak 2 — de voorbeeldprojecten

De set staat in `rig-cluster-projects-sandbox` (47 bestanden), niet in deze repo. Statisch
nagelopen: alle 47 lezen schoon, alle 47 dragen `clusters: [sandboxed-local]` en een `api-key`,
samen 90 componenten en **137 deployments** over 11 diensten. Schema-versies: 42x `2.2` en 5x
`2` -- geen enkele op de huidige `2.7`, dus de uitrol is meteen een upgrade-veiligheidsmeting.

De keten is bewezen op twee van de 47. `cot-zaq` (schema 2.2, 1 component, 1 deployment):
bestand geplaatst, `:reconcile`, `:refresh`, ArgoCD Synced+Healthy, pod Running, URL 200 --
in 9 tot 58 seconden. `algor-1ha` (3 componenten, 2 deployments): Healthy, repo klopt, in 85s.

**Een grens van de omgeving, geen platformfout.** Deze 47 zijn omgezette PRODUCTIEbestanden
en verwijzen naar prive-registries. `algor-odc` bleef hangen in `ImagePullBackOff`:

```
Failed to pull image "ghcr.io/rijksictgilde/algoritmeregister/postgresql-with-dictionaries:2024.11.19"
  ... failed to authorize: ... 403 Forbidden
```

De sandbox heeft die credentials niet. Daarvoor kent de omzetting `--probe-image`, die elke
workload vervangt door de e2e-probe; die optie is hier niet gebruikt omdat de bestanden kant
en klaar werden aangeleverd. Wie taak 2 afmaakt: gebruik die optie, of laat de meting een
`ImagePullBackOff` apart melden in plaats van er zeven minuten op te wachten -- anders kost
een handvol prive-images uren en zegt de uitslag niets over het platform.

De rest van de 47 is niet gemeten: de gedeelde sandbox ging naar een andere PR.

Opruimen tussen de projecten is geen keuze maar een grens: de node heeft een pod-cap van 110
en 137 deployments passen daar nooit tegelijk in.

Een waarneming voor wie dit afmaakt: `algor-1ha` werd Healthy met een kloppende repo, maar de
drie URL's gaven 404. Dat is geen platformfout -- dit zijn echte productieprojecten met echte
images, en die serveren lang niet allemaal iets op `/`. Rapporteer dus ArgoCD-status en
HTTP-code apart, en tel een 404 niet als mislukking van de keten.

---

## Taak 3 — wat er sinds de vorige generale bij is gekomen

### 1. De goedkeuringsdialoog — de reden van deze doorloop

**Werkt, en de mergeschade is werkelijk weg.**

De "Beheren"-knop draagt weer `hx-get="/admin/approvals/e2e71-jqm/modal-wizard/admin-approval"`,
met de echte projectnaam in de URL. In de pagina staat geen `openApprovalModal` en geen
`fetch(` meer — dat was de handbouw waar de merge op terugviel — en `#approval-loading` staat
er wél.

In de browser gemeten op het draaiende cluster:

| meting | uitkomst |
|---|---|
| dialoog opent | ja (`is-open` op backdrop én modal) |
| echte projectnaam in de URL | ja |
| aantal koppen in de dialoog | **1** (`Domeingoedkeuring - e2e71-jqm`) |
| leeg foutvak | **geen** (zichtbaar foutelement zonder tekst: niets) |
| Escape sluit | ja |

**Goedkeuren en afwijzen zijn door de hele keten gemeten, niet alleen op het scherm.** De
actie is een keuzelijst met `skip` / `approved` / `denied`:

- **Goedkeuren** van `mijn-eigen-domein.nl`: de gemelde URL klapte om van
  `https://frontend-productie-e2e71-jqm.sandbox.rijksapp.dev` naar
  `https://productie-e2e71-jqm.mijn-eigen-domein.nl`, de goedkeuringsmelding verdween, en de
  ingress in `rig-e2e71-jqm` werd opnieuw aangemaakt op het gevraagde domein.
- **Afwijzen**: de rij kreeg de tag `Afgewezen` in de kritieke (rode) vorm, met
  `door admin@sandbox.rijksapp.dev` erbij, en het adres viel terug op het clusteradres.

**De gedeelde schil doet het ook.** De bewerkdialoog van een project opent
(`edit-section-backdrop` + `edit-section-modal`), opslaan levert een `POST` naar
`/projects/{p}/modal-wizard/modal-edit-identity/step/identity-edit` met 200 en de tekst
"Wijzigingen opgeslagen", en Escape sluit hem. De dialoog blijft na opslaan bewust open met
die bevestiging — dat is geen fout; een eerdere meting van mij die "sluit na opslaan"
verwachtte was de verkeerde verwachting, niet het verkeerde gedrag.

### 2. Een niet-goedgekeurd domein

**Werkt, alle drie de delen.**

Met `base-domain: mijn-eigen-domein.nl` aangevraagd en nog niet goedgekeurd:

- de getoonde URL is `https://frontend-productie-e2e71-jqm.sandbox.rijksapp.dev`, dus het
  **clusteradres** en niet het aangevraagde;
- de waarschuwing staat erbij, letterlijk: *"Het domein mijn-eigen-domein.nl is aangevraagd en
  wacht op goedkeuring. Deze deployment is daarom bereikbaar op het standaard clusteradres."*;
- er bestaat **geen enkele ingress** op het aangevraagde domein. Alle ingress-hosts op het
  cluster zijn nagelopen: `mijn-eigen-domein.nl` komt er niet in voor.

Het clusteradres antwoordt ook echt: 200, met de inhoud van de e2e-allservices-probe.

### 3. `domain-format`

**Werkt, op beide wegen, en de waarden staan in het OpenAPI-document.**

- **API**: `PUT .../publish-on-web/config/deployment/productie` met `domain-format: onzin-formaat`
  geeft **422** met een `literal_error` die alle elf geldige waarden opsomt. Een geldige waarde
  geeft 202 en de taak eindigt `completed`.
- Op componentniveau is `domain-format` helemaal geen veld; daar komt een `extra_forbidden`.
  Ook een weigering, maar een strengere.
- **Formulier**: `tests/test_domain_format_gesloten_verzameling.py` dekt beide wegen —
  de veld-enforcer én het body-model — en is groen (26 tests).
- **`/openapi.json`**: de elf waarden staan als `enum` op twee schema's
  (`PublishOnWebDeploymentConfig.domain-format` en `UpsertDeploymentRequest.domain_format`).

### 4. De storage-config

**Werkt: PUT vervangt, PATCH voegt toe en verwijdert.**

| dienst | PUT | PATCH |
|---|---|---|
| `persistent-storage` | 202, taak `completed` | 202, `add` en `remove` allebei |
| `temp-storage` | 202, taak `completed` | 202 |
| `attachments` | 202, taak `completed` | 202 |

Dat PUT het blok werkelijk **vervangt** is aan het projectbestand gezien, niet aan het
antwoord: na `PUT [data 2Gi, extra 1Gi]`, `PATCH add data 3Gi` en `PATCH remove extra` stond
er precies één ingang over, `data` met `3Gi`. Bij `temp-storage` stonden na PUT + PATCH beide
ingangen er, dus daar voegt PATCH toe zonder de rest weg te gooien.

Twee dingen die onderweg misgingen en allebei terecht bleken:

- `attachments` gaf eerst 202 waarna de taak **faalde**. Reden: de dienst stond niet op het
  project. De taak zegt dat ook, volledig, in `error_message` en in `result.error` — mijn
  eerste lezing keek naar het lege veld `error` en concludeerde te snel "stil gefaald". Dat
  was mijn leesfout; de API legt het netjes uit.
- Een PUT met een **onbekende bijlage-referentie** geeft 202 en de taak faalt daarna met
  `Onbekende bijlage-referentie 'bestaat-echt-niet' gebruikt door: web`. De referentie wordt
  dus wel gecontroleerd, alleen in de taak en niet in het verzoek.

### 5. `check-subdomain` op zijn nieuwe pad

**Werkt.** `GET /api/v2/projects/{project}/subdomains/check/{subdomain}?base_domain=...` geeft
200 met `{"subdomain": ..., "base_domain": ..., "available": true, "validation_error": null}`.

De autorisatie eromheen is meegemeten, want een pad met een projectnaam erin nodigt daartoe uit:

- zonder sleutel: **401** (`X-API-Key header required`);
- met de sleutel van een **ánder** project: **401** (`Invalid API key`).

De verplichte `base_domain` wordt vóór de authenticatie gevalideerd — een verzoek zonder die
parameter geeft 422 in plaats van 401, ook zonder sleutel. Dat lekt niets (het antwoord is de
validatiefout van een queryparameter, geen projectgegeven), maar het is het vermelden waard.

### 6. De introductiepagina

**Werkt, alle drie de delen.**

- `/introductie` is **zonder inloggen** bereikbaar: 200, 77.577 bytes, titel
  "Introductie - Zelfservice Applicatie Deployment", kop "Jij bouwt de applicatie. Wij de rest."
- `/` stuurt een **anonieme** bezoeker door: 302 naar `https://zad.sandbox.rijksapp.dev/introductie`.
- Het menu-item staat onder **Platform** en boven **CLI**, met het puzzelstukje:
  `Platform` → `Introductie` (`puzzle-piece`) → `CLI`.

### 7. De metrics-explorer

**Werkt, in beide browsers.** Op `/metrics-explorer` (niet `/metrics`) staan twee keuzelijsten,
`service` en `metric`:

| browser | keuzelijsten | geometrie | overlap |
|---|---|---|---|
| Chromium 145 | 2 | `service` op x=432 w=376, `metric` op x=824 w=376, beide y=375 h=40 | **geen** |
| Firefox 146 | 2 | identiek | **geen** |

Ook op de schermafdruk gecontroleerd: de twee lijsten staan netjes naast elkaar in de kaart
"Kies een service en een metric", met de knop eronder.

### 8. De invitecode

**Werkt, en de maskering eromheen ook.**

- `PUT .../invite/config/project` met `{"active": {"key": ""}}` geeft 202, en de taak meldt de
  gegenereerde sleutel terug onder `generated`:
  `{"services/invite/config/active[0]/key": "wWfUCJO3VhjRZYrpsvDg5g"}`.
- `GET .../invite/config` geeft diezelfde sleutel terug — hij is dus **terug te lezen**.
- Env-varwaarden blijven wél gemaskeerd: na het schrijven van
  `GEHEIM_WACHTWOORD=supergeheim123` en `GEWOON=waarde` geeft de leesweg
  `{"GEHEIM_WACHTWOORD": "***", "GEWOON": "***"}`, en de plaintext komt er nergens in voor.

### 9. Sleep-mode

**Werkt.**

- **De twee endpoints accepteren de projectsleutel.** `GET /api/sleep-mode/{p}/{d}/status` en
  `POST /api/sleep-mode/{p}/{d}/wake` geven allebei 200 (`{"state": "ready"}` respectievelijk
  `{"state": "awake"}`), gemeten op een bestaand project én op een vers aangemaakt project.
  Zonder sleutel: 401 (`X-Wake-Token or X-API-Key header required`).
- **`wake-mode: confirm`** is de standaard. In `opi/services/catalog/sleep_mode/config_model.py`
  staat `wake_mode: WakeMode = Field(default="confirm", alias="wake-mode")`, met de reden erbij
  (wakker worden kost een halve minuut; bij `auto` doet elke crawler dat), en `/openapi.json`
  toont `"default": "confirm"`.

  Nuance die erbij hoort: de sleutel wordt **niet in het projectbestand gematerialiseerd**. Een
  vers project met sleep-mode aan levert `{"match": ["productie"], "enabled": true}` op — wie
  het YAML leest ziet geen `wake-mode`. De werkzame waarde is `confirm`, maar hij staat er niet.

### 10. De platformvelden

**Werkt.**

- Een PUT die `realms` meestuurt wordt geweigerd met **422** en een expliciete uitleg:
  *"'realms' of service 'keycloak' is written and owned by the platform and cannot be set or
  cleared through the API. Leave it out of the body: it is kept as it stands, and it is also
  left out of the read response for that reason."*
  Dit is gemeten met een **welgevormd** `realms`-blok (host, realm, username, password). Dat is
  het punt: een onvolledig blok geeft óók 422, maar dan op de ontbrekende velden, en dat zou
  de eigendomsregel niet bewijzen.
- Een PUT **zonder** `realms` geeft 202 en laat het blok staan.

---

## De reparaties die niet in het plan stonden

Het plan zet repareren buiten scope. Deze twee zijn tijdens het meten kapot gevonden en op
verzoek alsnog gerepareerd. Ze delen een vorm die het vermelden waard is: **allebei meldde de
code succes terwijl er niets gebeurde**, en allebei ontsnapten ze aan de tests doordat die de
toestand ná de handeling voeden in plaats van de toestand tijdens de handeling.

### Een dienst koppelen aan een component sloeg niets op

`POST /api/projects/{p}/services` met `components: ["web"]` antwoordde `status: success` met
`components_updated: ["web"]` — terwijl er **niets** werd geschreven. Geen commit in
`zad-projects`, en de serviceslijst van het component bleef leeg.

De oorzaak zit in de opslagpoort in `ProjectManager.add_service`, die alleen naar
`services_added` keek. Dat is niet hetzelfde als "er is iets veranderd": staat de dienst al op
projectniveau maar nog niet op het gevraagde component, dan is `services_added` leeg terwijl
`add_services_to_project` de componentlijst wél muteert. Die mutatie zat in dezelfde
`project_data` en werd zonder commit weggegooid, terwijl het antwoord hem uit datzelfde
resultaat als `components_updated` meldde.

Gerepareerd in `e0d10d8f`: de poort kijkt nu ook naar `components_updated`, en de
commitboodschap noemt wat er werkelijk gebeurde. `tests/test_add_service_component_persist.py`
pint beide kanten vast — dat er nu opgeslagen wordt, én dat er zonder mutatie nog steeds geen
commit komt. De poort is getoetst door de fix terug te draaien: dan vallen precies de drie
tests om die over deze weg gaan, en blijven de vier andere groen.

### Het domein-aanvraagvakje viel om bij een eigen domein

Gemeld tijdens de doorloop: je kiest een eigen domein, vinkt "Domein aanvragen" aan, slaat op,
en het vakje staat weer uit. De aanvraag werd ook nooit gedaan, dus de deployment bleef stil op
het clusteradres staan.

**Het zit in de volgorde, niet in het vakje.** Het keuzeveld slaat de schakelaar `__custom__`
op en deferet het echte domein naar het transiente veld `base-domain:custom`. Bij het
**renderen** is die deferral al opgelost, dus daar klopte alles — en dat is precies waarom het
zo lang onzichtbaar bleef. Bij het **verwerken** nog niet: `_resolve_deferrals` draait pas na
de veldenlus, en `_effective_base_domain` gaf op dat moment de sentinel terug. Beide
aanvraag-condities slaan af op `base_domain == "__custom__"`, dus gold het vakje als verborgen,
sloeg de verwerker het over via `should_render_editable`, en werd het aangevinkte vakje
weggegooid. Daarmee liep ook de `PRE_SAVE`-hook niet die de aanvraag doet.

Gereproduceerd op het draaiende cluster met de gemelde payload: het `nldd-checkbox-field` voor
`_request-domain` kwam zonder `checked` terug, terwijl het tekstveld ernaast zijn waarde
(`tweede-domein.nl`) wél vasthield.

Gerepareerd in `eb70adc0`: `_effective_base_domain` behandelt de sentinel nu als "niet
opgeslagen" en leest het transiente veld. Eén plek, en het dekt **beide** vakjes —
`DomainNeedsRequestCondition` en `SubdomainNeedsRequestCondition` vragen het allebei aan
dezelfde functie. De losse literals zijn vervangen door `CUSTOM_DOMAIN_SENTINEL` en
`CUSTOM_BASE_DOMAIN_KEY`.

`tests/test_domein_aanvraagvakje_blijft_staan.py` voedt de conditie nu de sentinel plus het
transiente veld — de vorm die de browser werkelijk instuurt. Met de fix teruggedraaid vallen
precies die twee tests om; de 746 bestaande domein-gerelateerde tests blijven groen.

**Wat hier níét kapot was:** de TLS-melding die bij dezelfde poging verschijnt.
`sandboxed-local` draagt `supports_custom_domain_certificates: False` — de sandbox serveert een
vooraf geïnstalleerd wildcard-certificaat en draait een neppe cert-manager-CRD zonder
controller, dus er wordt daar nooit iets uitgegeven. Elk eigen domein vraagt op dit cluster dus
om `tls: provided` met een certificaat als bijlage. Op `odcn-production` staat die vlag op
`True` en speelt dit niet.

---

## Wat er buiten viel

- **Repareren**, behalve de twee punten hierboven, die op verzoek alsnog gedaan zijn.
- **De mailrelay**: geparkeerd, zie `TODO_NEXT_RELEASE.md`. De bereikbaarheid naar de upstream
  is niet bewezen (poort 25, 587 en 465 lopen alle drie in een timeout).
- **Productie**: niets aangeraakt.


---

## Bevinding 7: een project zonder verbruik verdween van het dashboard

Gemeld door de eigenaar tijdens de doorloop: het dashboard toonde twee projecten terwijl
`/projects` er elf toonde, en dat zijn allebei lijsten die alles horen te tonen.

Het was niet de autorisatie -- de log liet zien dat hetzelfde dashboardverzoek **alle twaalf**
projecten autoriseerde. Het zat in `_dashboard-usage.html.j2`, in een guard
`{% if mem_mb or cpu %}`: een project zonder meetbaar verbruik kwam de lus niet door en
verdween stil. Niets vertelde dat er negen ontbraken.

Gerepareerd in `72d955c1`: elk project krijgt een regel, zonder verbruik staat er `0 MiB` en
`0m cores`. Twee bestaande tests pinden het oude gedrag vast en zijn meegekeerd.

**De kostenvraag die hierbij hoorde is gemeten, niet geschat.** De zorg was dat alle metrics
ophalen duur is. Dat is het niet: de cijfers per project komen uit **vier**
`by (namespace)`-queries over alle namespaces tegelijk, plus zeven aggregaten voor de totalen
(`collect_dashboard_metrics`). Dat aantal verandert niet met het aantal projecten -- de guard
gooide alleen weg wat al opgehaald was. De traagheid van de pagina zit dus elders en verdient
een eigen meting: welke van die queries traag is, en of ArgoCD meedoet.

---

## Bevinding 8: een waarschuwing op de LAATSTE wizardstap verdween

Gevonden bij de review van deze tak, en het is de rest van bevinding 4. Daar is de
waarschuwing uit de blokkeerpoort gehaald en laten meereizen naar de volgende stap. Maar
de laatste stap heeft geen volgende stap: die tak gaat naar `_render_modal_review`, en
daar viel de waarschuwing op de grond -- op precies het scherm waar de gebruiker besluit
te bevestigen.

Gerepareerd in `eac1fc1c`. De reviewpagina had al een `warnings`-kanaal (verwijderde
services, het overschrijven bij een restore), dus er komt geen mechanisme bij: de
waarschuwingen van de laatste stap gaan daar bij in. De tak zonder review
(`_modal_do_submit`) blijft ongemoeid -- die slaat direct op en rendert voortgang of
succes, dus de waarschuwing is daar achterhaald op het moment dat hij zou verschijnen.

---

## De basistak loste bevinding 1 zelf ook op

Deze tak liep lang genoeg door dat `release-augustus-2026` in de tussentijd dezelfde fout
repareerde: `e86037a3`, met dezelfde diagnose en dezelfde poort
(`services_added or components_updated`), maar in een andere vorm -- een
`_add_service_commit_message`-helper plus `tests/test_add_service_binds_existing.py`, en
met de uitrolpoort in `task_handlers_components` erbij.

Dat maakte de tak onmergebaar (twee conflicten in `project_manager.py`). Opgelost in
`2f9ed46f` door de vorm van de basistak te houden -- hij staat er al en de omliggende
wijzigingen horen erbij -- en uit deze tak alleen te bewaren wat de andere test niet dekt:
de commitboodschap, `component_names=None` op een dienst die al staat, en de gewone
toevoeging van een NIEUWE dienst.

**De les zit niet in de fix maar in de timing.** Twee sessies vonden onafhankelijk
dezelfde fout omdat het dezelfde week was. Bij een doorloop van meerdere dagen is
`git merge-tree` tegen `origin/<basis>` geen eindcontrole maar een dagelijkse: hoe later
je hem draait, hoe meer werk er dubbel blijkt.

---

## Wat deze doorloop over zichzelf leerde

Drie dingen die de volgende doorloop tijd besparen, en die geen van drieën over het product
gaan.

**1. Elke fix legde de volgende bloot.** Fix 2 maakte het aanvraagvakje werkend, waardoor de
geblokkeerde wizardstap zichtbaar werd; fix 4 opende die stap, waardoor de tweede opslag werd
bereikt en het valse conflict verscheen; de gelijktijdige reallife/punt14-run bracht de trage
statuspoll aan het licht. Vier van de acht waren onvindbaar zonder de vorige. Dat is het
argument voor een doorloop met echte handelingen boven lezen.

**2. De versie moet TIJDENS een lange meting bevestigd blijven.** Het plan zegt dit
("controleer dit ook halverwege opnieuw") en het is hier misgegaan: taak 2 draaide na 06:12:49
tegen de image van een andere PR, en dat is pas opgemerkt doordat de eigenaar het zei. Vijf
gemeten projecten zijn daarom weggegooid. Het slot beschermde dat niet -- een andere PR kon de
claim overnemen terwijl er nog een lease liep.

**3. Meet op het signaal, niet op de klok.** De eerste opzet van taak 2 wachtte op een
synchrone `:refresh` en kostte 20 minuten per project met een onbereikbare image. Wachten op
wat het cluster zelf zegt -- ArgoCD-health en podstatus -- bracht dat terug naar 24 tot 85
seconden. Dezelfde les gold voor de deploys: `/version` loog twee keer, en de betrouwbare
controle was de pod zelf vragen of de code erin zit.
