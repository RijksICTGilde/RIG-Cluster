# Het beheerdeel van ZAD: de inventarisatie

**Geschreven op**: 28 augustus 2026, tegen commit `d32fb07e` op de tak
`het-beheerdeel-van-zad-rollen-overzicht-en-wat-een`. Elk anker hieronder is nagelopen tegen
de code van die commit. Wat er niet is staat als **bestaat niet**, en niet als aanname.

Dit is deel 1 van twee. Deel 2 (het rollenmodel, de beheerdersstartpagina, de grensregel en
de fasering) staat in `plans/beheer-in-zad-plan-van-aanpak.md`.

## Waarom dit document er is

RC-148 (`plans/meldingen-inventarisatie.md`, `plans/meldingen-oplossingsrichtingen.md`,
`plans/meldingen-plan-van-aanpak.md`) beschrijft een meldingssysteem en zet de standaard voor
een platformbeheerder op "alles, inclusief type 12", met filters op de lijst als enige rem.
De opdrachtgever wil dat niet: hij hoeft niet per se voor alle projecten alles te zien. En hij
voegt eraan toe dat het beheerdeel van ZAD sowieso nog niet lekker is uitgewerkt.

Dat tweede is de eigenlijke opdracht. De firehose in het postvak is een symptoom. Als er een
behoorlijk beheerdersoverzicht was, was de helft van die meldingen daar een regel geweest en
geen postvakrij. Dit document meet daarom eerst wat het beheerdeel vandaag is, want zonder dat
is elke uitspraak over wat er in het postvak hoort een mening.

**Het datamodel van RC-148 blijft staan.** Wat dit document raakt is niet de opslag maar de
verdeling: wie krijgt wat, en waar landt het.

## Hoe je dit document leest

| Kolom | Betekenis |
|---|---|
| wat | de handeling of het gegeven, in gewone taal |
| waar | het codeanker, of **bestaat niet** |
| vorm | UI, API, CLI, Taskfile, of alleen `kubectl` |
| wie | welke grendel ervoor staat |

Namen die ik zelf verzin staan als **voorstel** gemarkeerd. Alles zonder die markering is een
naam die in de code staat.

---

## 1. Het beheermenu: vijf ingangen

Het menu voegt vijf items toe zodra `is_platform_admin` waar is (`opi/web/menu.py:56-70`).
In de nieuwe schil staan diezelfde vijf paden in de groep "Beheer"
(`opi/web/navigation_lotc.py:126`). Achter die vijf ingangen zitten **zestien routes**, en op
alle zestien staat `require_platform_admin` (`opi/core/auth_decorators.py:65-79`). Dat is
netjes: de grendel staat op de route en niet alleen op het menu-item, ook op de fragmenten die
een pagina nalaadt.

| Menu-item | Pad | Routes | Bron |
|---|---|---|---|
| Metrics | `/metrics-explorer` | 2 | `opi/web/metrics_explorer_router.py:78`, `:106` |
| Gebruikersbeheer | `/admin/users` | 6 | `opi/web/router_user_admin.py:103`, `:132`, `:146`, `:200`, `:220`, `:287` |
| Gebruik & Kosten | `/admin/usage` | 1 | `opi/web/router_usage.py:155` |
| Aanvragen | `/admin/approvals` | 3 | `opi/web/router_approvals.py:264`, `:314`, `:361` |
| Services status | `/admin/diensten` | 4 | `opi/web/router_shared_services.py:38`, `:57`, `:71`, `:85` |

### Gebruikersbeheer (`/admin/users`)

**Wat je kunt doen**: een gebruiker aanmaken, bewerken en verwijderen. Dit is de enige van de
vijf pagina's met volledige CRUD op iets dat wordt opgeslagen; `/admin/approvals` legt wel een
oordeel vast maar maakt niets aan en verwijdert niets.

**Wat de tabel toont**: drie gegevenskolommen, volledige naam, e-mailadres en aanmaakdatum,
plus een knoppenkolom (de vier kopregels op `opi/templates_lotc/bg/admin-users.html.j2:42-45`,
de `created_at`-cel op `:51`, de knoppencel vanaf `:52`). De ORM-tabel heeft ook niet
meer: `id`, `email`, `full_name`, `created_at`, `updated_at`
(`opi/services/persistence/users.py:22-28`).

**Wat er ontbreekt, en dit is het belangrijkste van deze pagina:**

- **Er is geen rolkolom.** De `users`-tabel kent geen rol. Deze pagina beheert dus geen
  rechten, hij beheert een toegangslijst: wie in de tabel staat, mag inloggen.
- **Je kunt hier geen platformbeheerder maken of afvoeren.** Dat kan nergens in de
  applicatie, zie paragraaf 2.
- **Er staat niet bij wie platformbeheerder is.** Ook niet als leesbare kolom. Wie de vraag
  "wie heeft er verhoogde rechten op dit platform" wil beantwoorden, leest de ConfigMap.
- **Er is geen koppeling met Keycloak.** `UserAdminService` doet CRUD op de tabel en niets
  anders; er komt geen enkele Keycloak-aanroep in voor
  (`opi/services/user_admin_service.py`, 72 regels, geen import van een Keycloak-connector).
  Een gebruiker die hier verdwijnt, bestaat in Keycloak nog.
- **Er staat niet bij bij welke projecten iemand hoort.** Dat gegeven zit in de
  projectbestanden, niet in deze tabel, en wordt hier niet opgehaald.

**Wat wel goed zit**: de in-geheugen toegangslijst wordt bij elke mutatie meebewogen
(`opi/web/router_user_admin.py:192`, `:277-279`, `:304`), dus een verwijderde gebruiker is
meteen buiten en niet pas na een herstart.

### Gebruik & Kosten (`/admin/usage`)

**Wat je kunt doen**: kijken, en drie dingen kiezen via de queryreeks: jaar, namespace en
prijs per GiB (`opi/web/router_usage.py:161-163`). Verder read-only.

**Wat het meet**: uitsluitend geheugen. De pagina draait `RECORDED_USAGE_QUERY`
(`opi/web/router_usage.py:26-34`) over de opnameregel `rig:namespace_memory_billed_bytes`, of
bij ontbreken daarvan de zware terugval over de ruwe metrieken (`:38-69`). Er is geen CPU, geen
opslag en geen netwerk. De prijs is een vaste `DEFAULT_PRICE_PER_GIB = 27.0`
(`opi/web/router_usage.py:24`), overschrijfbaar per verzoek maar nergens opgeslagen.

**Wat er ontbreekt**:

- **Geen kolom per project, alleen per namespace.** De keuzelijst wordt opgebouwd uit de
  projecten (`_get_available_namespaces`, `:100-109`), maar de tabel toont maanden als rijen
  en één gekozen namespace. Wie wil weten welk project het duurst is, kiest 47 keer
  achter elkaar een namespace.
- **Geen export.** Geen CSV, geen JSON-endpoint.
- **Geen grens en geen signaal.** Er is geen bedrag waarboven iets opvalt.

### Aanvragen (`/admin/approvals`)

**Wat je kunt doen**: de openstaande aanvragen zien en er via een modale wizard een oordeel
over vellen (`opi/web/router_approvals.py:314`, `:361`). Dit is de enige pagina van de vijf
waar een beheerder een besluit neemt dat iets in beweging zet.

**Wat erachter zit**: drie goedkeuringsdeclaraties in de dienstencatalogus. Twee bij
`publish-on-web` (`domain` en `subdomain`,
`opi/services/catalog/publish_on_web/__init__.py:411-428`) en één bij `send-email`
(`opi/services/catalog/send_email/__init__.py:88`, via `service_use_approval`).

**Wat er ontbreekt**:

- **Er staat wel een datum, maar geen leeftijd en geen escalatie.** De tabel toont de datum
  van de laatste history-regel, zonder tijd
  (`opi/templates_lotc/bg/admin-approvals.html.j2:192-194`). De lijst is gesorteerd op
  projectnaam (`opi/web/router_approvals.py:166`), niet op ouderdom, er is geen filter op
  ouderdom (de filters gaan over status, `APPROVAL_STATUSSEN`, `:193`) en er is geen grens
  waarboven iets opvalt. "Wat ligt hier het langst" is dus wel af te leiden, maar alleen door
  de hele lijst te lezen.
- **En bij een van de drie soorten staat die datum er niet.** Domein- en
  subdomeinaanvragen schrijven bij het aanvragen een history-regel met tijdstip
  (`opi/connectors/subdomain.py:511` en `:552`,
  `{"date": now, "status": "requested"}`). De generieke dienstgebruik-goedkeuring, die
  `send-email` gebruikt, schrijft een **lege** history
  (`opi/services/catalog/approval.py:303`). Het sjabloon toont de datum alleen als er een
  history is (`{% if item.history %}`), dus bij een `send-email`-aanvraag staat de kolom leeg
  en is de ouderdom nergens vandaan te halen.
- **Geen melding.** Niemand hoort dat er een aanvraag is. Dit is precies de pijn waarmee
  RC-148 zijn fase 1 verdedigt (`plans/meldingen-plan-van-aanpak.md`, "Fase 1").
- **De aanvraag kan niet naar een ander.** `ApproverScope` kent drie waarden
  (`opi/services/catalog/approval.py:45-56`): `PLATFORM_ADMIN`, `PROJECT_ADMIN` en
  `PROJECT_MEMBER`. Alle drie de specs zetten `PLATFORM_ADMIN`, en het veld `approver` wordt
  **nergens uitgelezen**: een zoektocht op `.approver` in `opi/` levert nul treffers. De
  scheiding staat dus in de declaratie en doet niets.

### Services status (`/admin/diensten`)

**Wat je kunt doen**: kijken. Vier routes, alle vier GET: het kader plus drie blokken die lui
worden nageladen (`opi/web/router_shared_services.py:38`, `:57`, `:71`, `:85`).

**Wat het toont**: de toestand van de gedeelde diensten, met drempels op een plek
(`DREMPELS` in `opi/services/gedeelde_diensten.py:75`) zodat een latere alarmering dezelfde
grenzen kan gebruiken. Wat niet meetbaar is staat er benoemd
(`ONGEMETEN_DIENSTEN`): Redis en MinIO hebben geen exporter
(`opi/services/gedeelde_diensten.py:20-25`).

**Wat er ontbreekt**:

- **Het gaat over de gedeelde infrastructuur, niet over de projecten.** Postgres, Keycloak,
  opslag. De vraag "welk project is ongezond" wordt hier niet beantwoord.
- **Er is geen actie.** Een kritieke drempel toont een kleur; wat je eraan doet, doe je met
  `kubectl`.

### Metrics (`/metrics-explorer`)

**Wat je kunt doen**: een van zes vast ingebouwde diensten kiezen
(`MONITORED_SERVICES`, `opi/web/metrics_explorer_router.py:27`, met de id's
`cloudnative-pg`, `minio`, `keycloak`, `operations-manager`, `kubernetes-pods`,
`prometheus`), de metriekennamen daarvan opvragen, en de grafiek in een iframe van de
Prometheus-UI bekijken (`_get_prometheus_external_url`, `:73`).

**Wat er ontbreekt**: dit is geen ZAD-pagina, dit is een deurtje naar Prometheus. De lijst van
zes staat hard in de code en groeit niet mee met de diensten die het platform aanbiedt.

### Wat opvalt als je de vijf naast elkaar zet

**Drie van de vijf zijn volledig read-only**: gebruik en kosten, dienstenstatus en de
metrics-explorer, samen zeven van de zestien routes en alle zeven GET. Van de twee die wel iets
doen, beheert er één een toegangslijst zonder rollen en legt de ander een oordeel vast over een
aanvraag.

**Er is geen enkele pagina die over het platform als geheel gaat**: geen lijst van projecten met
hun toestand, geen lijst van wat er vandaag mis ging, geen lijst van wat er op iemand wacht. De
vijf pagina's zijn vijf losse antwoorden op vijf losse vragen, en de vraag "hoe staat het
ervoor" zit er niet bij.

---

## 2. De rol: een vinkje, en wat er achter zit

### Er is er één, en hij is plat

```python
def is_platform_admin(self, email: str) -> bool:
    """Whether this email has platform-admin rights."""
    return bool(email) and email.lower().strip() in self._platform_admin_emails
```

`opi/services/user_service.py:279-281`. De verzameling erachter is een gewone Python-set die
bij het aanmaken van de dienst leeg is (`opi/services/user_service.py:28`).

### Waar de lijst vandaan komt

Twee bronnen, allebei bij het opstarten, allebei via `add_platform_admins`
(`opi/services/user_service.py:270-277`):

1. **Een lijst in de broncode.** `opi/core/startup.py:559-562` zet
   `robbert.uittenbroek@rijksoverheid.nl` erin, onvoorwaardelijk, op elk cluster.
2. **De instelling `ADMIN_EMAILS`**, komma-gescheiden (`opi/core/config.py:217`,
   toegepast in `opi/core/startup.py:564-567`).

Op productie staat `ADMIN_EMAILS=robbert.bos@rijksoverheid.nl`
(`bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/configmap.yaml:46`).
**Op productie zijn er dus twee platformbeheerders**, en dat is een getal dat je alleen kunt
kennen door twee bestanden te lezen die geen van beide in de applicatie zichtbaar zijn.

Op de sandbox staat `ADMIN_EMAILS=admin@sandbox.rijksapp.dev`
(`bootstrap/rig-system/kustomize/operations-manager/overlays/sandboxed-local/configmap.yaml:55`), plus dezelfde vaste eerste. Ook twee.

### Hoe de lijst zich verhoudt tot de `users`-tabel

**Niet.** De `users`-tabel voedt bij het opstarten uitsluitend de **toegangslijst**, via
`add_allowed_emails` (`opi/core/startup.py:546-556`), en die lijst beantwoordt een andere
vraag: mag je inloggen (`is_email_allowed`, `opi/services/user_service.py:319-334`). De
verzameling platformbeheerders wordt daar niet uit gevuld.

Gevolg: **een platformbeheerder hoeft niet in de `users`-tabel te staan**, en een gebruiker in
de `users`-tabel wordt nooit platformbeheerder. De twee lijsten kunnen elkaar volledig missen.
`add_platform_admins` zet zijn adressen wel óók op de toegangslijst
(`opi/services/user_service.py:276`), dus andersom klopt het wel: een beheerder mag altijd
inloggen.

De toegangslijst heeft nog een derde bron: projectleden. Bij het laden en verversen van de
projecten worden hun e-mailadressen toegevoegd (`opi/services/project_store.py:1178`,
`opi/services/project_service.py:105`).

### Hoe de lijst zich verhoudt tot Keycloak

**Niet.** Keycloak levert de authenticatie (OIDC-discovery in de ConfigMap, realm
`rig-platform`). Wie je bent komt daarvandaan; wat je mag komt uit deze twee Python-sets. Er
is geen Keycloak-rol, geen groep en geen claim die op `is_platform_admin` wordt afgebeeld.

**Wat dat kost.** Beheerdersrechten worden niet toegekend maar uitgerold: iemand toevoegen is
een wijziging in een ConfigMap, een commit, een ArgoCD-sync en een herstart van OPI. Er is
geen handeling in de applicatie, dus ook geen actor, geen tijdstip en geen spoor. RC-148 heeft
dit al gezien en zet het in zijn eventcatalogus als "**bestaat nog niet**"
(`plans/meldingen-inventarisatie.md`, paragraaf 7, de regel "Iemand is platformbeheerder
geworden of afgevoerd").

### Waar het vinkje wordt uitgelezen

**Vier plekken in productiecode, en niet zes.** De opdracht zegt "zes plekken", en dat getal is
te herleiden: `grep -rn is_platform_admin --include="*.py" opi/` geeft **zes regels**, maar twee
daarvan zijn geen aanroep. `opi/services/user_service.py:279` is de definitie zelf en
`opi/web/router.py:2534` is een codecommentaar dat de naam noemt. De vier die overblijven:

| Waar | Wat het doet |
|---|---|
| `opi/core/auth_decorators.py:77` | `require_platform_admin`: 401 zonder sessie, 403 zonder recht |
| `opi/web/menu.py:59` | de vijf menu-items tonen |
| `opi/services/project_authorization.py:42` | toegang tot elk project |
| `opi/services/project_authorization.py:62` | de rol `admin` in elk project |

De eerste is de grendel op de zestien beheerroutes. De tweede is presentatie. **De laatste
twee zijn de grote.**

### De impliciete bevoegdheid, en die is fors

```python
def is_user_authorized_for_project(project_name: str, user_email: str) -> bool:
    """Check whether a user may access a project. Platform admins always may."""
    if get_user_service().is_platform_admin(user_email):
        logger.debug(f"User {user_email} authorized for project {project_name} (admin)")
        return True
    ...

def get_user_role_for_project(project_name: str, user_email: str) -> str | None:
    """Return a user's role in a project. Platform admins always get "admin"."""
    if get_user_service().is_platform_admin(user_email):
        return "admin"
    ...
```

`opi/services/project_authorization.py:40-44` en `:60-63`.

Die twee functies worden samen **36 keer** aangeroepen in productiecode: 23 keer
`is_user_authorized_for_project` en 13 keer `get_user_role_for_project`, verspreid over
`opi/web/router.py` (17 plus 11), `opi/api/v2/router.py`, `opi/web/project_edit_security.py`,
`opi/web/router_detail_edit.py`, `opi/api/logs_websocket_router.py`,
`opi/services/catalog/cross_domain_access/context.py` en
`opi/services/catalog/shared/backups.py`. Op elk van die 36 plekken zegt het vinkje ja.

Wat daar concreet uit volgt:

- **"Mijn projecten" is voor een beheerder alle projecten.** `_projects_for_user`
  (`opi/web/router.py:2481-2513`) loopt alle projecten langs en houdt er precies die over
  waarvoor `is_user_authorized_for_project` waar is. Voor een beheerder is dat de hele lijst,
  met `user_role` op `admin` in elke rij.
- **Het dashboard idem** (`opi/web/router.py:1210-1213`).
- **Bewerken mag overal.** `require_project_edit_access`
  (`opi/web/project_edit_security.py:42-47`) toetst achtereenvolgens toegang en rol tegen
  `PROJECT_EDIT_ROLES = ("admin", "owner")` (`opi/services/project_authorization.py:27`). Een
  beheerder haalt allebei.
- **De projectsleutel ligt op de pagina.** Het paneel "Configuratie & Secrets" met de
  `api-key` staat achter `{% if user_role in ["admin", "owner"] %}`
  (`opi/templates_lotc/bg/project-tabs.html.j2:163`, het veld op `:177`). Dat is de enige
  plek waar die grendel om de sleutel staat: het tweede sjabloon dat de `api-key` toont,
  `opi/templates_lotc/bg/project-details.html.j2:89`, bevat geen enkele `user_role`-verwijzing,
  maar dat is geen productiepagina. Geen route in `opi/web/` rendert dat sjabloon; het is
  alleen bereikbaar via de publieke proefopstelling `/lotc/bg/project-details` en die vult het
  met verzonnen gegevens (`opi/web/lotc_fixtures.py:521`). Die sleutel opent volgens het
  commentaar bij `PROJECT_EDIT_ROLES` "every mutating per-project API route and carries no
  role of its own" (`opi/services/project_authorization.py:24-27`). Een beheerder kan hem dus
  van elk van de 47 projectpagina's kopiëren.
- **De logstroom mag ook** (`opi/api/logs_websocket_router.py:355`).

**En er staat geen enkele meting tegenover.** Geen logregel op INFO, geen gebeurtenis, geen
teller. Alleen de eerste van de twee functies logt haar beslissing, en dan op DEBUG
(`opi/services/project_authorization.py:43`). `get_user_role_for_project` (`:60-73`) bevat geen
enkele logaanroep: de platformbeheerderstak is `return "admin"` en verder niets. En op productie
staat `LOG_TO_FILE=false`, dus ook die DEBUG-regels landen nergens. Wie wil weten of een
beheerder ooit in een projectbestand heeft gekeken dat niet van hem is, kan dat niet nagaan.

**Dat is één vinkje met 36 gevolgen, zonder spoor.** Dat is de zwaarste bevinding van dit
document, en deel 2 gaat erover.

### De tweede beheerderssleutel, die niemand een naam heeft

Naast de allowlist bestaat er een **tweede, volstrekt losstaande beheerdersweg**: de
instelling `ADMIN_API_KEY` (`opi/core/config.py:264`, standaard `None`). Zeven endpoints
staan erachter, alle zeven met `@validate_admin_api_key`
(`opi/api/endpoint_util.py:103-141`):

| Endpoint | Bron |
|---|---|
| `GET /api/v2/admin/marked-for-deletion` | `opi/api/admin_router.py:42` |
| `POST /api/v2/admin/cleanup/trigger` | `:75` |
| `POST /api/v2/admin/reconciliation/trigger` | `:109` |
| `DELETE /api/v2/admin/marked-for-deletion/{mark_id}` | `:155` |
| `GET /api/v2/admin/orphans/report` | `:185` |
| `POST /api/v2/admin/orphans/confirm` | `:208` |
| `POST /api/v2/admin/projects/:reconcile` | `:295` |

Drie dingen kloppen hier niet met elkaar:

1. **Die sleutel draagt geen identiteit.** Het is één gedeeld geheim, vergeleken met
   `secrets.compare_digest` (`opi/api/endpoint_util.py:134`). Wie hem gebruikt is niet vast te
   stellen. Voor het meldingssysteem betekent dat: gebeurtenissen uit deze zeven endpoints
   hebben geen `actor`.
2. **Hij staat volledig los van `is_platform_admin`.** Een platformbeheerder heeft hem niet, en
   wie hem heeft is geen platformbeheerder. Twee beheerdersrollen dus, die elkaar niet kennen.
3. **Op productie is hij niet gezet.** `ADMIN_API_KEY` komt in `bootstrap/rig-system/` maar
   één keer voor, en dat is de sandbox
   (`bootstrap/rig-system/kustomize/operations-manager/overlays/sandboxed-local/configmap.yaml:18`). In de productie-ConfigMap staat hij
   niet, en in het versleutelde env-secret ook niet: dat bestand draagt 21 sleutels, met hun
   NAMEN in leesbare vorm (SOPS versleutelt de waarden, niet de sleutels), en `ADMIN_API_KEY`
   zit er niet bij
   (`bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/operations-manager-env-secrets.yaml`).

**Gevolg**: op productie antwoorden alle zeven met **501**
("This endpoint requires ADMIN_API_KEY to be configured",
`opi/api/endpoint_util.py:127-132`). De weesopruiming die `features/service-orphan-reconciliation.md:65`
en `:84` documenteert (`GET /orphans/report`, daarna `POST /orphans/confirm`) is op productie
dus niet uit te voeren. Zie paragraaf 3.

---

## 3. Beheerwerk dat buiten het beheerdeel valt

Alles hieronder is werk dat een platformbeheerder doet of moet kunnen doen, en dat niet achter
een van de vijf menu-items zit.

**Over de kolom "vorm"**: de opdracht noemt UI, API, CLI, Taskfile en `kubectl` als
mogelijkheden. **"CLI" komt in de tabel niet voor**, en dat verdient een woord. De `zad-cli`
staat niet in deze repository, dus wat hij kan is hier niet te meten; wat wel te meten is, is
waar hij mee praat. Volgens `workflow/build.md:93` is dat de API, en de plannen die over hem
gaan (`plans/een-project-aanmaken-vanaf-de-cli.md`, `plans/de-cli-vindt-zijn-eigen-projecten.md`,
`plans/vragen-uit-zad-cli.md`) gaan alle drie over projectwerk. Voor beheerwerk is `Taskfile.yaml`
wat die rol vervult, met 113 taken.

| Wat | Waar het vandaag zit | Vorm | Wie mag |
|---|---|---|---|
| Een cluster opzetten, bijwerken, afbreken | `Taskfile.yaml` (113 taken) | Taskfile | wie de repository en de AGE-sleutel heeft |
| Het OPI-image bouwen en uitrollen | `Taskfile.yaml`, `update-operations-manager`, `publish-operations-manager` | Taskfile | idem |
| Iemand platformbeheerder maken | ConfigMap `ADMIN_EMAILS` plus een herstart | Git + ArgoCD + herstart | wie de repository heeft |
| Wezen opsporen | `opi/jobs/service_orphan_sweep.py` via `GET /api/v2/admin/orphans/report` | API | `ADMIN_API_KEY`, **op productie 501** |
| Wezen bevestigen als te verwijderen | `POST /api/v2/admin/orphans/confirm` (`opi/api/admin_router.py:208`) | API | idem |
| De markeringen inzien | `MarkedForDeletionService.get_all_marks` (`opi/services/marked_for_deletion_service.py:163`) | API | idem, en verder alleen de achtergrondtaken |
| Reconciliatie handmatig starten | `POST /api/v2/admin/reconciliation/trigger` | API | idem |
| Reconciliatie automatisch | `opi/core/reconciliation_scheduler.py`, elke nacht om `RECONCILIATION_HOUR = 3` (`opi/core/config.py:359`) | achtergrondtaak | niemand, hij loopt |
| De automatische stemmer | `opi/core/resource_tuning_scheduler.py`, elke nacht om `hour: 1` (`opi/services/catalog/resource_tuning/config.py:28`) | achtergrondtaak | niemand |
| Slaapstand | `opi/services/catalog/sleep_mode/scheduler.py`, elke `SLEEP_MODE_SWEEP_MINUTES = 30` (`opi/core/config.py:526`) | achtergrondtaak | niemand |
| Backupplanning | `opi/core/backup_scheduler.py`, controle elke `BACKUP_SCHEDULER_INTERVAL = 600` seconden (`opi/core/config.py:497`) | achtergrondtaak, per deployment een RRULE | de projectbeheerder stelt hem in |
| Bewaartermijn van backups | `BACKUP_RETENTION_KEEP_*` (`opi/core/config.py:488-491`) | instelling | wie de ConfigMap heeft |
| Opruimen van weesbackups | `BACKUP_SWEEP_ENABLED` / `BACKUP_SWEEP_DRY_RUN` (`opi/core/config.py:517-519`) | achtergrondtaak | idem |
| De logbewaker naar ntfy | `opi/services/log_watcher.py`, `opi/core/logwatcher_scheduler.py`, elke `LOGWATCHER_INTERVAL_SECONDS = 1800` (`opi/core/config.py:442`) | achtergrondtaak naar ntfy | wie het ntfy-topic kent |
| Clusterconfiguratie | `opi/core/cluster_config.py` plus de ConfigMap | Git + ArgoCD | wie de repository heeft |
| Wat er in de database staat | geen enkele weg vanuit ZAD | alleen `kubectl exec` + `psql` | wie clustertoegang heeft |
| Alle taken over alle projecten | **bestaat niet** | - | - |

Vier dingen springen eruit.

**Ten eerste: het beheerdeel is niet de plek waar het beheer gebeurt.** Van de zeventien
regels hierboven zit er geen enkele in het menu. Geteld uit de kolom "vorm": **zes** lopen als
achtergrondtaak zonder dat iemand ze aanstuurt of ziet, **vier** via de Taskfile of git, en
**vier** via een API die op productie niet aan staat. De drie die dan nog overblijven zijn een
instelling in de ConfigMap (de bewaartermijn van backups), `kubectl exec` plus `psql` (wat er
in de database staat) en de regel die als "bestaat niet" in de tabel staat (alle taken over
alle projecten). Zes plus vier plus vier plus drie is zeventien.

**Ten tweede: de weesopruiming is een keten met een gat erin.** De reconciliatie is
uitdrukkelijk report-first: stap 3 in `opi/jobs/reconciliation.py:391-398` zegt met zoveel
woorden dat automatisch markeren op basis van een scan verboden is, met een verwijzing naar de
`waggl-9et`-bijnaramp. Het menselijke oordeel daartussen loopt via
`POST /api/v2/admin/orphans/confirm`. Die route geeft op productie 501. **De keten is dus op
productie niet af te lopen**, terwijl `features/service-orphan-reconciliation.md` hem als de
werkwijze beschrijft.

**Ten derde: reconciliatie staat op productie in de proefstand.**
`RECONCILIATION_DRY_RUN` is standaard `True` (`opi/core/config.py:360`) en wordt in de
productie-ConfigMap niet overschreven, en staat ook niet tussen de 21 sleutels van het
versleutelde env-secret. De nachtelijke run logt dus wat hij zou doen en doet het niet. Dat is
een verdedigbare keuze, maar hij is nergens zichtbaar: de uitkomst gaat naar de logregel
`Reconciliation complete: purged=%d, marked=%d, unmarked=%d, errors=%d`
(`opi/jobs/reconciliation.py:400`) en verder nergens heen.

**Ten vierde: de slaapstand staat op productie uit.** De clusterstandaard geeft alleen
`sandboxed-local` de waarde `enabled: True`
(`opi/services/catalog/sleep_mode/config.py:22-24`), en de productie-ConfigMap zegt het er
zelf bij ("Sleep-mode itself stays off in production (cluster default) until validated",
`bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/configmap.yaml`). Dat scheelt een hele klasse gebeurtenissen, en
dat is relevant voor de schatting in paragraaf 5.

---

## 4. Wat een beheerder vandaag niet kan zien

Dit is de belangrijkste paragraaf van dit document. Per vraag: kan hij hem beantwoorden, en zo
ja hoe.

### "Welke projecten zijn ongezond?"

**Niet in één scherm.** De ArgoCD-stand wordt per project opgehaald op de projectdetailpagina
(`get_project_argocd_statuses`, `opi/services/argocd_overview.py:92`, aangeroepen vanuit
`opi/web/router.py:1819`). Wie 47 projecten wil beoordelen, opent 47 pagina's.

**En dat is wrang, want de gegevens worden al opgehaald.** De docstring van die module zegt
het zelf: de bevraging haalt alle applicaties op en gooit die van andere projecten weg
("De applicaties van andere projecten komen wel over de lijn en worden hier weggegooid",
`opi/services/argocd_overview.py:13-15`). Een platformbreed gezondheidsoverzicht kost dus
**geen extra bevraging**, alleen een pagina die niet weggooit.

### "Wat wacht er op mij?"

**Half.** `/admin/approvals` toont de openstaande aanvragen, en dat is de enige soort werk die
op een beheerder kan wachten en ook echt getoond wordt. Maar:

- er staat een datum bij, maar de lijst is niet op ouderdom te sorteren of te filteren, en
  bij een `send-email`-aanvraag ontbreekt die datum helemaal (paragraaf 1);
- niets meldt dat het er is;
- de bevestigingsstap van de weesopruiming, die óók op een beheerder wacht, staat er niet
  bij en is op productie zelfs niet uitvoerbaar (paragraaf 3).

### "Wat heeft het platform vannacht zelf veranderd?"

**Niet.** Drie achtergrondtaken veranderen 's nachts dingen zonder dat iemand erom vroeg, en
geen van drieën heeft een scherm:

- de automatische stemmer (01:00) schrijft geheugenwaarden in projectbestanden, met een
  `history`-blok inclusief tijdstip, bron en reden;
- de reconciliatie (03:00) trekt markeringen terug of ruimt op (op productie: in proefstand);
- de logbewaker draait elk halfuur en duwt ERROR-regels naar ntfy.

De stemmer laat als enige een duurzaam spoor achter dat een mens kan lezen, en dat spoor staat
in de YAML van het project (`plans/meldingen-inventarisatie.md`, paragraaf 4, "De automatische
stemmer verdient een aparte opmerking"). Er is geen pagina die de `history`-blokken van alle
projecten naast elkaar zet.

### "Wat is er vandaag misgegaan?"

**Niet, en het is na een uur zelfs onmogelijk.** `TASK_WORKER_CLEANUP_RETENTION_HOURS` staat
op `1` (`opi/core/config.py:369`); de opruimlus verwijdert elke taak in een eindtoestand
waarvan `completed_at` ouder is (`opi/core/task_worker.py:420`,
`opi/core/async_task_service.py:670`). Daarbovenop is de takentabel per project
(`opi/web/router_tasks.py:113`, `GET /{project_name}/tasks`); er is geen platformbrede
takenlijst.

Dit is dezelfde bevinding die RC-148 als kern van zijn opdracht neerzet
(`plans/meldingen-inventarisatie.md`, "Waarom 'op het scherm kijken' hier niet genoeg is, in
één getal"). Hij staat hier opnieuw omdat hij niet alleen een meldingsprobleem is: ook mét
meldingen blijft er geen scherm waar je een dag kunt overzien.

### "Wat staat er te wachten om verwijderd te worden?"

**Niet in de UI.** `get_all_marks` heeft precies drie lezers en geen daarvan is een pagina:
`opi/api/admin_router.py:63` (501 op productie), `opi/jobs/service_orphan_sweep.py:348` en
`opi/jobs/reconciliation.py:370`.

### "Wie heeft er verhoogde rechten?"

**Niet in de applicatie.** Zie paragraaf 2. Dit is de vraag waar de BIO expliciet iets over
zegt, en deel 2 gaat erop in.

### "Verloopt er binnenkort iets?"

**Niet.** Certificaten: het contactadres in de clusterconfiguratie is dat van de
ACME-account, dus Let's Encrypt mailt rechtstreeks en ZAD weet er niets van
(`plans/meldingen-inventarisatie.md`, paragraaf 7). Images: `features/image-version-audit.md`
is een handmatig onderzoek van februari 2026, geen controle die draait. Backups: er is niets
dat "al N dagen geen backup" waarneemt.

### "Hoeveel kost dit project?"

**Half.** `/admin/usage` geeft geheugen per namespace per maand. Niet per project naast
elkaar, niet met CPU of opslag erbij, niet exporteerbaar.

### Samengevat

| Vraag | In één scherm? |
|---|---|
| Welke projecten zijn ongezond | nee (gegevens zijn er wel, worden weggegooid) |
| Wat wacht er op mij | half (alleen aanvragen, zonder leeftijd) |
| Wat heeft het platform zelf veranderd | nee |
| Wat ging er vandaag mis | nee (en na een uur is het weg) |
| Wat staat er op de nominatie om te verdwijnen | nee |
| Wie heeft er verhoogde rechten | nee |
| Wat verloopt er binnenkort | nee |
| Wat kost het | half |

**Acht redelijke beheerdersvragen, nul die in één scherm beantwoord worden.** Dat is het gat
waar deze opdracht over gaat.

---

## 5. De schaal

Dit getal draagt het argument, dus de methode staat erbij en de aannames staan apart van de
metingen.

### Wat gemeten is

| Grootheid | Waarde | Bron |
|---|---|---|
| Projecten | **47** | `docs/derde-generale-augustus-2026.md:243-245`, nageteld in `rig-cluster-projects-sandbox`; de sandboxrepository is de omgezette kopie van de productierepository (`docs/productiebestanden-naar-een-sandbox.md:63`, "Done: 47/47 projects migrated") |
| Deployments | **137** | idem, tabel op `:249-254` |
| Componentdefinities | 90 | idem |
| Component-instanties in deployments | **261** | idem |
| Project-deployments op productie | **127**, in **44** namespaces | `opi/core/startup.py:582-583`, een meting uit de opstartlus zelf ("127 `get namespace` plus 127 `label namespace` for 44 distinct namespaces") |
| Platformbeheerders op productie | **2** | `opi/core/startup.py:559-561` (één vast) plus `ADMIN_EMAILS` (`bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/configmap.yaml:46`, één adres) |
| Toegelaten gebruikers uit de ConfigMap | 6 | `ALLOWED_EMAILS`, idem `:45` |
| Taaksoorten | **23** | `TaskType`, `opi/core/async_task_service.py:54-77` |

De twee tellingen van de deploymentaantallen (137 uit de bestanden, 127 uit de opstartlus)
liggen 8 procent uit elkaar en zijn van verschillende momenten. Voor een orde van grootte maakt
dat niets uit; ik reken hieronder met **137**.

De verdeling is scheef: de grootste vier projecten hebben samen 65 van de 137 deployments
(`wies` 18, `regel-k4c` 17, `asses-k2n` 15, `pm-5sj` 15, zie `docs/derde-generale-augustus-2026.md:256`),
dus 47 procent. Het gemiddelde is 2,9 deployments per project; de mediaan ligt lager.

### De structurele uitspraak, zonder aannames

Bijna alles wat een melding oplevert hangt aan een **deployment**, niet aan een project: een
uitrol, een gezondheidsgebeurtenis, een backup, een slaapstand, een stembeurt. Onder de
RC-148-standaard "platformbeheerder: alles" krijgt hij per definitie de som over alle 137.

Daaruit volgt zonder één aanname:

- een platformbeheerder krijgt **exact de som van alle 47 projecten**;
- vergeleken met een beheerder van een gemiddeld project (2,9 deployments) is dat **47 keer
  zoveel**;
- vergeleken met de beheerder van het grootste project (`wies`, 18 deployments) is het
  **7,6 keer zoveel**.

**Dat is de kern van het bezwaar van de opdrachtgever, uitgedrukt in een verhouding in plaats
van in een gevoel.** Een postvak dat voor een projectbeheerder werkt, is voor een
platformbeheerder tussen de acht en vijftig keer te vol. Er is geen filter dat dat repareert,
want een filter dat je elke keer opnieuw zet is zelf het werk.

### Een absoluut getal, met de aannames erbij

Om er een orde van grootte bij te zetten, reken ik alleen de bronnen die vuren **zonder dat
iemand iets doet**. Dat is de ondergrens, en het is de eerlijkste helft: over hoe vaak mensen
uitrollen kan ik niets meten.

| Bron | Rekenwijze | Per dag |
|---|---|---|
| Automatische stemmer | nachtelijke sweep over de component-instanties; hij schrijft alleen als de afwijking de drempel haalt (`increase_threshold: 10`, `decrease_threshold: 30`, `opi/services/catalog/resource_tuning/config.py:25-26`). **Aanname: 10 procent van 137 beweegt op een nacht.** | ~14 |
| Geplande backups | per deployment een RRULE, opt-in. **Aanname: de helft van de 137 deployments heeft een dagelijkse backup.** Elke run is een taak (`TaskType.BACKUP`) en dus een gebeurtenis, geslaagd of niet. | ~68 |
| Reconciliatie | draait om 03:00, maar `RECONCILIATION_DRY_RUN` blijft op productie `True` (paragraaf 3), dus geen mutaties. Eén samenvattingsregel. | ~1 |
| Slaapstand | staat op productie uit (paragraaf 3). | 0 |
| Logbewaker | draait al en gaat naar ntfy, niet naar een postvak. | 0 |
| **Ondergrens, niemand doet iets** | 14 + 68 + 1 | **~83** |

Daarbovenop komt alles wat mensen wél doen: uitrollen, images bijwerken, componenten
toevoegen, diensten configureren, verwijderen. Dat zijn 23 taaksoorten over 137 deployments.
Met de aanname dat een deployment eens per twee weken wordt aangeraakt, is dat nog eens
ongeveer tien per dag, plus wat er misgaat.

**Orde van grootte: enkele tientallen tot enkele honderden meldingen per dag voor één
platformbeheerder**, waarvan het grootste deel binnenkomt zonder dat iemand erom vroeg. Ter
vergelijking: op productie zijn er **twee** mensen die dat krijgen.

**Wat aan dit getal gemeten is**: 47, 137, 261, 23, de drempels, de nachtelijke uren, dat de
slaapstand uit staat en dat de reconciliatie in proefstand loopt. **Wat aangenomen is**: het
percentage componenten dat per nacht beweegt, het aandeel deployments met een dagelijkse
backup, en de uitrolfrequentie. Die drie aannames staan hierboven met hun waarde erbij, zodat
ze te vervangen zijn door een meting zodra iemand die heeft.

**Wat het getal niet verandert**: ook bij tien keer minder blijft de verhouding van 47 tegen 1
staan, en die verhouding is het argument.

---

## 6. Wat hier bewust niet in staat

- **De projectkant van het portaal.** De wizard, de detailpagina, de dienstenbladen. Dat is
  het gebruikersdeel en het werkt; deze opdracht gaat over het beheerdeel.
- **De infrastructuur.** ArgoCD, Prometheus, Grafana en Keycloak hebben hun eigen
  beheerinterfaces met hun eigen inlog. Wat dit document telt is wat een beheerder in ZAD zelf
  kan.
- **Een oordeel over de vijf pagina's als vormgeving.** Of `/admin/usage` er goed uitziet doet
  hier niet ter zake; wat hij kan wel.
- **De inhoud van de meldingsteksten.** Dat is RC-148 en het is bouwwerk.
