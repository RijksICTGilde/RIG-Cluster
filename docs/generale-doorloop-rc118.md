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

**Deze tak kan naar main.**

Alle geautomatiseerde suites zijn groen, en elk van de tien punten uit taak 3 is apart
aangetoond in plaats van aangenomen — de goedkeuringsdialoog inclusief goedkeuren én afwijzen
door de hele keten heen, tot aan de ingress die van adres wisselt. Er zijn twee dingen kapot
gevonden die niet in het plan stonden; allebei gerepareerd, en allebei staan ze hieronder met
zoveel woorden.

Twee voorbehouden, allebei uitgeschreven:

1. De vier "bekende rode" die het plan noemt bestaan niet op deze tak. `tests/test_taken_voortgang_link.py`
   staat er niet en wordt niet gecollect. Er is dus niets gemeld als bekend rood — de unitsuite
   is werkelijk helemaal groen.
2. Taak 2 is gemeten met opruimen tussen de projecten door. Dat is geen keuze maar een grens:
   de 47 bestanden dragen samen 137 deployments en de sandbox is één node met een pod-cap van
   110. Ze passen er nooit tegelijk in.

---

## Taak 1 — de geautomatiseerde suites

| suite | uitslag | duur |
|---|---|---|
| `uv run pytest tests/ -q` | **9007 passed, 7 skipped, 0 failed** | 7m27s |
| `uv run pytest -m e2e -q` (1e) | **436 passed, 67 skipped, 1 xpassed, 0 failed** | 11m56s |
| `uv run pytest -m e2e -q` (2e) | **436 passed, 67 skipped, 1 xpassed, 0 failed** | 12m06s |
| `uv run pytest -m sandbox -q` | (zie hieronder) | |
| `uv run pytest -m reallife -q` + `-m punt14` | (zie hieronder) | |

De unitsuite is gedraaid met de eigen standaardaanroep, zonder eigen `-m`.

**Twee e2e-runs, twee keer hetzelfde beeld.** Dat is de reden dat het plan er twee vroeg: er
is deze week een geval geweest van twee tests die een browsersessie deelden. Beide runs geven
exact dezelfde aantallen en dezelfde ene `xpassed`. Geen wisselvalligheid gevonden.

**Over de "bekende rode".** Het plan meldt vier rode in `tests/test_taken_voortgang_link.py`
als onafgemaakt werk van een andere sessie. Dat bestand staat niet op deze tak; het wordt niet
gecollect en er is dus ook geen rood om te melden. De 9007 groene tests zijn de hele suite.

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
