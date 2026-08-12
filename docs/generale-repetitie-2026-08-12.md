# Generale repetitie: van projectbestand tot draaiende deployment

Datum: 12 augustus 2026. Taak: RC-86. Uitgevoerd op de gedeelde sandbox op de dev-server,
op commit `636db488` van de tak `een-generale-repetitie-van-projectbestand-tot-draa`.

Dit is een **toets**, geen bouwtaak. Wat hier staat is de uitkomst per stap en een oordeel,
plus wat er onderweg kapot bleek. Het plan staat in
`plans/een-generale-repetitie-voor-productie.md`.

## Uitgangspositie

| Wat | Uitkomst |
|---|---|
| Draaiende versie | `GET /version` -> `636db488`, tak `een-generale-repetitie-van-projectbestand-tot-draa` |
| Schone projecttoestand | 4 verweesde e2e-projecten van eerdere runs verwijderd voor de start |
| Sleutel | sandbox draait op zijn eigen `SECRET_KEY` uit de `sandboxed-local`-configmap, niet de productiesleutel |
| Domein | `*.sandbox.rijksapp.dev`, bevestigd |

**Afwijking van het plan.** Het plan schrijft `task sandbox:setup` voor. Dat is hier NIET
gedraaid: `workflow/sandbox.md` verbiedt dat expliciet in een sessie op de dev-server, want
die taak hoort bij een volledige lokale dev-opzet en rolt een registry-image uit in plaats
van de build van deze tak. In plaats daarvan is de eigen build via `sandbox-deploy`
uitgerold en is de projecttoestand schoongemaakt door de achtergebleven projecten te
verwijderen. De cluster-infrastructuur zelf (Forgejo, Keycloak, ArgoCD, CNPG, MinIO) is niet
opnieuw opgebouwd.

## Per stap

### 1. Conversie van bestaande projectbestanden - GESLAAGD

De sandbox-repo bevatte alleen vier verweesde e2e-projecten, dus dat is geen zinnige
steekproef. De toets is daarom gedraaid op de **47 echte productiebestanden** uit
`rig-cluster-projects-github`, met de bestaande laag-1 test:

```
RIG_PROJECTS_DIR=<klon>/projects uv run pytest tests/test_upgrade_safety_replay.py -q
-> 9 passed
```

Alle 47 migreren en valideren daarna. Uitgesplitst:

| Van | Aantal | Naar |
|---|---|---|
| 2.0 | 5 | 2.7 |
| 2.2 | 41 | 2.7 |
| 2.2 (geen migratie nodig) | 1 | 2.2 |

De dp-bn7-valkuil is meteen meetbaar: **46 van de 47 bestanden valideren NIET op de rauwe
gegevens**, en wel na migratie. Valideren op het rauwe bestand in plaats van op de
gemigreerde zou dus vrijwel het hele bestand kapotmaken, precies zoals het plan waarschuwt.

### 2. Project via de wizard - GESLAAGD

Gedekt door de sandbox-suite (`test_sandbox_flows.py::test_create_project_via_ui` en
`test_sandbox_all_services.py`), die het project door de wizard aanmaakt en daarna het
projectbestand in Forgejo terugleest. Zie "De e2e-suites" hieronder.

### 3. Hetzelfde via de API, inclusief impliciete dienstselectie - GESLAAGD

Project `rra-baj` aangemaakt met `POST /api/v2/projects`. Deze route wil een SSO-bearer,
geen API-sleutel; het token is gehaald via dezelfde auth-code+PKCE-weg als de CLI, met de
publieke client `zad-cli`. De `aud` is `zad-api`, zoals `CLI_TOKEN_AUDIENCE` eist. Een
token van de portal-client (`aud: account`) wordt terecht geweigerd.

Impliciete dienstselectie (RC-84), gemeten in twee richtingen:

- **Mag wel.** Een component met `services: ["postgresql-database"]` op een project dat die
  dienst niet had: de dienst meldt zichzelf aan op projectniveau. De dienstenlijst toont hem
  daarna met een gebruik op `project` en een op `component`, en het projectbestand valideert.
  De draaiende pod meldt `postgres bound=true ok=true` op de echt aangemaakte database
  `rra_baj_prod` - de dienst is dus niet alleen bijgeschreven maar ook geprovisioneerd.
- **Mag niet.** Datzelfde met `publish-on-web` erbij wordt geweigerd met: *"Services that
  must be enabled at project level first: ['publish-on-web']. They need project-level
  configuration that cannot be assumed, so they are not added automatically."* Dat is een
  begrijpelijke fout die zegt wat er moet gebeuren.

### 4. Een tweede deployment - GESLAAGD

`prod` en `staging` draaien allebei op `rra-baj`, elk met een eigen database
(`rra_baj_prod` / `rra_baj_staging`), een eigen ingress en een eigen hostnaam. Beide melden
`Healthy` en `Synced`, en de werklast zelf meldt `all_ok: true`.

RC-83 is zichtbaar in de voortgang: de stappen zijn Nederlands en dragen hun onderwerp,
bijvoorbeeld `Database klaarmaken | onderwerp: prod` en `Database klaarmaken |
onderwerp: staging` naast elkaar in dezelfde herverwerking, en
`staging: uitgerold en gezond`.

De deploymenttabel op de projectpagina toont beide deployments met cluster, status en
laatste sync (zie de schermafbeelding in de bijlage).

**Niet gedekt:** de TLS-override per deployment-component (RC-78) is niet uitgeoefend. Die
is niet via de component-API te zetten - hij hoort in de dienstconfiguratie van
publish-on-web op de deployment-component-laag, in `modal-edit-deployment-<n>` - en er was
geen certificaat-bijlage om `provided` mee te toetsen. Dit blijft dus open.

### 5. Backup en terugzetten - GESLAAGD

Met echte gegevens, niet alleen met statuscodes:

1. Tabel `repetitie` met een rij aangemaakt in `rra_baj_prod`.
2. `POST /api/v1/backup/project/rra-baj/deployment/prod` -> 1 database geback-upt.
3. Tabel weggegooid.
4. `POST /api/v1/restore/database/.../prod-postgresql?project_name=rra-baj` met **een leeg
   lichaam** -> `success`, teruggezet in de eigen database.
5. De rij staat er weer. Het terugzetten draagt dus werkelijk gegevens over.

De drie gevallen uit het plan:

| Geval | Uitkomst |
|---|---|
| Zonder doelvelden | 200, teruggezet in de eigen dienst van het project bij de sleutel (RC-81) |
| Half ingevuld (3 van de 4) | 422 met *"Specify all target fields or none of them ... Missing: target_database_password"* - noemt het ontbrekende veld |
| Bestemming die niet resolvet | **400 met `error_category: InvalidTarget`** (RC-82), zoals de toets eist |

### 6. Reprocess van een bestaand project - GESLAAGD

`POST /api/v2/projects/rra-baj/:refresh` verwerkt beide deployments opnieuw en doet dat
zichtbaar: elke deployment krijgt zijn eigen stappen met onderwerp, en de taak eindigt op
`completed` met `prod: uitgerold en gezond` en `staging: uitgerold en gezond`. Geen stille
mislukking.

## De e2e-suites

Beide suites zijn volledig gedraaid, zoals het plan vraagt.

| Suite | Uitkomst |
|---|---|
| Lokaal (`-m "e2e and not sandbox"`) | **7 gefaald, 356 geslaagd, 1 overgeslagen, 3 xfailed** in 10m49 |
| Sandbox (`-m "e2e and sandbox"`) | **9 gefaald, 16 geslaagd, 25 fouten bij het opzetten** in 20m24 |

Van de zeven lokale fouten was er **een** vooraf bekend. De andere zes zijn de opbrengst van
deze doorloop en staan hieronder. De sandbox-suite is er slechter aan toe, en die uitkomst
heeft één oorzaak met een nare bijwerking: bevinding 7.

## Bevindingen

### 1. Bekend en verwacht: paginamarge

`test_lotc_paginamarge.py::test_de_kolom_wordt_niet_eindeloos_breed` bewaakt een
kolombreedte onder 1400 terwijl de gekozen bovengrens 1440 oplevert. Dat is een getal en
geen fout, precies zoals het plan aankondigt. Niet aangeraakt.

### 2. Elk aanvinkvakje levert twee elementen met dezelfde `id` op

Twee gefaalde tests (`test_aanvinkvakje.py`, beide). In de browser gemeten staan er voor
een enkel aanvinkvakje **twee elementen in de lichte boom met dezelfde `id`**:

```
DIV.lotc-checkbox-field   id=_services-config/keycloak/config/restrict-access/enabled
NLDD-CHECKBOX-FIELD       id=_services-config/keycloak/config/restrict-access/enabled
```

De oorzaak zit in het samenspel van twee kanten. De NLDD-tak van het LOTC-component
`checkbox-field` zet de `id` op een **omhullende div** (en leidt daar `-help` en `-error`
van af) en geeft daarnaast de attribuutbundel door aan het binnenste
`<nldd-checkbox-field>`. Ons sjabloon `widgets/checkbox.html.j2` zet de `id` in diezelfde
bundel (`:attrs="dict(field_attrs(field), id=field.path)"`), juist omdat de `id` op het
BESTURINGSELEMENT moet landen - dat is het element dat `.checked` draagt. Onder het oude
RVO-thema landden beide op hetzelfde `<input>` en viel het niet op; onder NLDD zijn het
twee elementen.

Gevolg: dubbele `id`'s in het document (ongeldige HTML, en een risico voor `aria-describedby`
en voor elke `label for=`), en `[id='<pad>']` levert er twee op in plaats van een.

**Niet gerepareerd.** De keuze is niet triviaal: de `id` weghalen bij het component kost de
afgeleide `-help`/`-error`-id's, en weghalen uit de bundel kost de vindbaarheid van het
besturingselement. De echte oplossing zit in het LOTC-component en dat is een besluit, niet
een tikfout. Dit raakt **elk** enkel aanvinkvakje in de applicatie, niet alleen dit veld.

### 3. Het projecttabblad: drie tests toetsen een bewust verdwenen knop

Drie gefaalde tests (`test_lotc_project_tab.py`). Ze eisen de aanroep
`copyToClipboard('.config-code', event, '.config-item')` en een `nldd-button.copy-btn`.
Die zijn er niet meer, en dat is **met opzet**: in `bg/project-tabs.html.j2` is het
kopieerknopje vervangen door `<c-secret-field ... show-copy />`, dat het klembord in het
veld zelf heeft. Op de schermafbeelding van de projectpagina is te zien dat elk veld onder
"Configuratie & Secrets" zijn eigen kopieerknopje heeft, dus het vermogen is niet verloren.

De tests zijn dus achterhaald, niet de code. Wel blijft er een spoor van de omzetting
liggen: `bg/project-tabs.html.j2` zet nog `{% set kopieer = "copyToClipboard(...)" %}`
zonder die variabele nog te gebruiken, en `project-details/section-config.html.j2` heeft
hetzelfde patroon (`{% set lotc_onclick_1 %}...{% endset %}` naast een `<c-button>` die er
niet naar verwijst). Dat is dode code.

**Niet gerepareerd.** Of die drie tests herschreven of geschrapt horen te worden is een
keuze over wat de vangrail moet bewaken, en die hoort bij degene die de omzetting deed.

### 4. `test_gedragsoppervlak` op de projectdetailpagina

Een gefaalde test, met dezelfde oorzaak als hierboven: hij toetst het vastgelegde
gedragsoppervlak van `/projects/details/test-project-detail`, en de verdwenen
`copyToClipboard`-aanroep hoort daarbij.

### 5. Een taak met een MISLUKTE subtaak meldt toch `completed`

Gemeten op de afgewezen dienstselectie uit stap 3 (taak `0d504be5`):

```
"status": "completed",
"progress_percent": 100,
"subtasks": [ ... {"name": "Component toevoegen", "status": "failed", "error": "..."} ],
"result": {"status": "failed", "error_type": "invalid_services"},
"error_message": "Services that must be enabled at project level first: ..."
```

De fout staat er wel in - in `error_message`, in `result.status` en op de subtaak - maar het
veld waar een client als eerste op kijkt, `status`, zegt `completed`. Een aanroeper die op
`status` polt ziet een afgewezen wijziging aan voor een geslaagde. Dat is precies de stille
klasse fout die deze doorloop moet vangen.

**Niet gerepareerd.** Wat `status` hoort te betekenen als een subtaak faalt is een besluit
over de taak-API, met gevolgen voor de CLI en voor elke bestaande aanroeper.

### 6. Gerepareerd: `sandbox_project_tool.py` was stuk

Buiten de opdracht, maar evident kapot en nodig om de sandbox schoon te krijgen. Het
gereedschap schraapte de API-sleutel met `roos-secret-field__value`. Sinds het NLDD-thema
heet die klasse `lotc-secret__value` en staat de waarde in `data-value` in plaats van in de
tekst. Alle drie de subcommando's (`api-key`, `delete`, `set-config`) liepen daarop stuk.
Aangepast naar de nieuwe klasse en het attribuut; daarna werkte het opruimen meteen.

### 7. De opruiming van de sandbox-suite sloopt de sandbox als het opzetten faalt

Dit is de ernstigste bevinding, en hij is tijdens de run zichtbaar geworden doordat de
portal er na de sandbox-suite kapot uitzag.

**Wat er gebeurde.** 25 tests strandden al bij het opzetten: het aanmaken van het project
via de wizard komt niet voorbij `submit_wizard()`, waar `_wait_for_htmx()` op
`networkidle` plus `#wizard-step-inner` wacht en na 10 seconden opgeeft. Bij de laatste
verzendknop verlaat de wizard die pagina juist, en `networkidle` is op deze sandbox al
vaker het probleem geweest. Het is geen constante: `test_create_project_via_ui` gebruikt
dezelfde hulpfunctie en slaagde wel. Dit is dus een wankele fixture, geen kapotte wizard.

**Waarom dat de omgeving beschadigt.** Als het opzetten faalt is er geen API-sleutel, en de
afbraak roept `DELETE /api/projects/<naam>` dan aan met een lege sleutel. Dat geeft **401**.
In de log staan er vijf op een rij. Maar `delete_project_via_api` riep daarna
**onvoorwaardelijk** `cluster.force_cleanup_project()` aan, en die verwijdert met kale
kubectl de ArgoCD-applicaties (inclusief het wissen van de `resources-finalizer`), het
AppProject en de namespaces.

Resultaat: het project is uit het cluster gerukt terwijl het in ZAD en in de projects-repo
gewoon blijft staan. Dat is de slechtst denkbare toestand - half kapot in ArgoCD, nog
aanwezig in de portal - en hij heelt niet vanzelf. Na de run stonden er **12 verweesde
projecten** in `zad-projects`, plus een ArgoCD-applicatie en een AppProject van een run van
de dag ervoor waarvan de map nog in `zad-argo-user-applications` stond, waardoor ArgoCD hem
telkens opnieuw aanmaakte.

**Opgeruimd.** Alle 12 zijn alsnog netjes via OPI's eigen delete-endpoint verwijderd (dus
mét de echte afbraak), en de map van `e2e56-3cf` is uit de argo-repo gehaald. Eindstand:
alleen `rra-baj` en de infrastructuur, alles `Synced`/`Healthy`.

**Gerepareerd.** `force_cleanup_project()` wordt nu alleen nog aangeroepen als OPI de delete
werkelijk heeft geaccepteerd (`response.is_success`). Bij een 401/403/404 blijft het cluster
met rust, zodat de toestand hooguit blijft staan in plaats van uiteen te vallen. De wankele
wizard-fixture zelf is NIET aangepast: dat is een echte reparatie aan de e2e-opzet en die
hoort een eigen taak te zijn.

## Het oordeel

**De keten zelf is gezond. De schil is dat op twee punten niet.**

Alles wat het plan als toets noemt en dat over de KETEN gaat, is geslaagd: elk
productiebestand migreert en valideert, de API-weg levert een geldig projectbestand op,
impliciete dienstselectie doet wat hij belooft en weigert netjes wat hij niet mag, twee
deployments draaien naast elkaar met hun eigen database, backup en terugzetten dragen echte
gegevens over, een onbereikbare bestemming geeft de beloofde 400 met `InvalidTarget`, en
reprocess faalt niet stil.

Wat er tegenover staat is de verbouwde paginaschil. Bevinding 2 raakt elk aanvinkvakje in de
applicatie met dubbele `id`'s, en dat is een toegankelijkheids- en geldigheidsprobleem dat
niet in productie hoort. Bevinding 5 is een API-belofte die niet klopt en die de CLI kan
misleiden.

Daar komt bij dat de sandbox-suite in deze staat geen bruikbare poort is: 25 van de tests
komen niet eens aan hun eigen toets toe, en tot deze doorloop lieten ze bij elke mislukking
een half gesloopte omgeving achter (bevinding 7).

**Advies: nog niet uitrollen.** Niet omdat de keten het niet doet - die is hier van
projectbestand tot draaiende pod aangetoond - maar omdat er drie dingen open staan die
allemaal klein zijn en allemaal een besluit vragen dat in deze taak niet genomen mag worden:
bevinding 2 (dubbele `id` op elk aanvinkvakje), bevinding 5 (een mislukte subtaak die
`completed` meldt) en de wankele wizard-fixture uit bevinding 7. Zijn die weg, en zijn de
vier achterhaalde tests uit bevinding 3 en 4 herschreven of geschrapt, dan staat er niets
meer in de weg - deze doorloop heeft de rest van de keten dan al aangetoond.

Wat expliciet NIET getoetst is en dat wel verdient voor de uitrol: de TLS-override per
deployment-component (RC-78).
