# Rechteninventarisatie ZAD: wat er vandaag is

Status: **inventarisatie, geen ontwerp**. Dit document beschrijft uitsluitend de gemeten werkelijkheid. De analyse en de oplossingsrichtingen staan in [rechten-modellen-en-tokens.md](rechten-modellen-en-tokens.md), de aanbeveling in [rechten-plan-van-aanpak.md](rechten-plan-van-aanpak.md). De scheiding is met opzet: wie het oneens is met de aanbeveling kan deze inventaris nog steeds gebruiken.

Meetmoment: 22 augustus 2026, commit `51fd763e` (gelijk aan `origin/main`). Elk feit hieronder verwijst naar een bestand, meestal met regelnummer. Wat niet in de code is teruggevonden staat er niet in, of staat er met de vermelding dat het niet geverifieerd is.

## 0. Hoe dit gemeten is

De routetellingen komen uit een AST-analyse van `operations-manager/python/opi/`: elke functie met een `@<router>.get/post/put/patch/delete/websocket`-decorator, met het `prefix=` van de bijbehorende `APIRouter` ervoor geplakt, en met de authenticatiedecorators die op dezelfde functie staan. Geteld is **per decorator en niet per functie**, want één functie kan meer dan één route dragen: `liveness_check` (`opi/server.py:491-492`) heeft twee gestapelde `@app.get`-decorators en registreert daarmee zowel `/health` als `/healthz`. Dat levert **157 routes**: 151 op een `APIRouter` en 6 rechtstreeks op de app. Grep is hier ontoereikend, omdat de paden zonder prefix in de bron staan en omdat sommige routers via `router.include_router(...)` in `opi/web/router.py:51-61` worden ingehangen.

Er is precies één route die zo niet gevonden wordt, omdat hij niet via een decorator maar via `web_router.add_api_route("/subdomains/check", ...)` wordt geregistreerd (`opi/web/router.py:120`, handler in `opi/web/router_self_service.py:49`). Handmatig toegevoegd komt het totaal op **158 routes**. Dat dit maar met de hand te vinden was, is zelf een bevinding: elke inventaris die op decorators steunt, mist per definitie wat buiten die conventie wordt geregistreerd, en een inventaris die per functie telt in plaats van per decorator mist bovendien de tweede en volgende route van een gestapelde decorator, zoals `/health` hierboven.

Drie routes zitten niet in die 158 omdat FastAPI ze zelf toevoegt: `/docs`, `/redoc` en `/openapi.json` (`opi/server.py:320-322`).

Waar dit document een gedragsclaim doet die niet uit lezen alleen volgt, is die claim uitgevoerd. Dat staat er dan bij.

## 1. Afwijkingen van de opdrachtbeschrijving

De taakbeschrijving bevatte een startinventaris. Die is op meerdere punten niet wat er in deze tak staat. Onderstaande tabel is bedoeld voor de lezer die met de oude cijfers in het hoofd binnenkomt.

| Bewering in de opdracht | Gemeten op 22 augustus 2026 |
|---|---|
| SSO-poort dekt 83 routes | **75** routes |
| Projectsleutel dekt 55 routes | **46** routes via de decorator, **plus 3** met een handgeschreven variant van dezelfde controle (zie 5) |
| `ADMIN_API_KEY` dekt 7 routes | **6** routes |
| `MASTER_API_KEY` dekt 4 routes | **3** routes |
| SSO-bearer-token `validate_user_token` in `opi/api/user_token_auth.py`, 2 routes | **Bestaat niet.** Geen bestand, geen functie, geen route. Er is geen bearer-token-pad in deze tak |
| `ApproverScope` in `opi/services/catalog/approval.py:54` | **Bestaat niet.** Er is geen `opi/services/catalog/`, en de strings `platform-admin`/`project-admin`/`project-member` komen nergens als waarde voor |
| Twaalf handgeschreven rolcontroles, waarvan drie in de dienstencatalogus | **Zes** handgeschreven rolcontroles, alle zes in `opi/web/router.py`. De genoemde catalogusbestanden bestaan niet |
| `PROJECT_EDIT_ROLES` in `opi/services/project_authorization.py:27` | Bestaat niet als constante. Het paar `("admin", "owner")` staat letterlijk uitgeschreven op zeven plekken in Python en dertien in templates |
| `features/cli-project-aanmaken.md`, `features/cli-projecten-opvragen.md` | Bestaan niet |
| `features/futures/keycloak-rechten-overdragen.md` | Bestaat niet |
| ZAD genereert Kubernetes-RBAC voor gebruikersnamespaces | Niet gevonden. In `operations-manager/python/manifests/` staat geen `Role`, `RoleBinding` of `ServiceAccount`. Wel een `NetworkPolicy` en een ArgoCD `AppProject` (zie 7) |
| `TODO_FUTURE.md` | Bestaat niet in de repository-root |

Dat de opdracht een `ApproverScope` en een bearer-token-pad beschrijft die hier niet staan, is op zichzelf een bevinding: **er is geen enkele plek waar het platform vastlegt welke gezagsbegrippen het kent**, dus een beschrijving ervan kan ongemerkt verouderen of vooruitlopen. Dit document is die plek nog niet, het is een momentopname. Zie 10 voor wat er nodig is om het niet meteen weer te laten verouderen.

## 2. De poorten

Zes manieren om een handeling uitgevoerd te krijgen. Vier ervan zijn een decorator; de twee andere zijn met de hand geschreven, verspreid over vier handlers.

| Poort | Waar | Routes | Wat hij bewijst | Wie hij identificeert |
|---|---|---|---|---|
| SSO-sessie, `@requires_sso` | `opi/core/auth_decorators.py:18` + `opi/middleware/authorization.py:73` | 75 | er is een geldige sessiecookie met een `user`-object, en dat e-mailadres staat op de allowlist | een mens, op e-mailadres |
| Projectsleutel, `@validate_api_token` | `opi/api/endpoint_util.py:14` | 46 | de aanroeper kent het geheim van precies dit project | niemand, alleen het project |
| `ADMIN_API_KEY`, `@validate_admin_api_key` | `opi/api/endpoint_util.py:82` | 6 | de aanroeper kent een gedeeld platformgeheim uit de omgeving | niemand |
| `MASTER_API_KEY`, `@validate_master_api_key` | `opi/api/endpoint_util.py:123` | 3 | idem, een tweede gedeeld geheim | niemand |
| Handgeschreven projectsleutelcontrole | `opi/api/task_router.py:56` en `:159-165` | 3 | hetzelfde als de decorator, apart uitgeschreven | niemand |
| Handgeschreven sessiecontrole in een websocket | `opi/api/logs_websocket_router.py:299-360` | 1 | Origin-header, sessiecookie, allowlist en projectlidmaatschap, allemaal met de hand | een mens |

De middleware doet er nog iets bovenop dat makkelijk over het hoofd wordt gezien. `AuthorizationMiddleware.dispatch` (`opi/middleware/authorization.py:82-116`) slaat elke padcontrole over voor `/metrics` en voor alles onder `/health`, `/ready`, `/version` en `/static/`, en laat elk pad dat met `/api/` begint zonder sessiecontrole door omdat die routes hun eigen sleutelcontrole doen. Voor alle overige paden bepaalt `_route_requires_sso` (`:126-156`) of de gevonden endpointfunctie het attribuut `_requires_sso` draagt. **Staat dat attribuut er niet op, dan is de route publiek.** Alleen wanneer er helemaal geen route matcht valt de middleware terug op "wel SSO vereist", en dat geval bereikt de handler per definitie nooit.

De decoratorvolgorde is daarbij bepalend: `@requires_sso` moet ónder de routedecorator staan, anders registreert de router de kale functie en gaat het attribuut verloren. Op alle routes met een decorator is de routedecorator de bovenste; dat is gecontroleerd met dezelfde AST-analyse. Bij de route uit `add_api_route` speelt de volgorde niet, omdat de gedecoreerde functie daar rechtstreeks wordt doorgegeven.

### Wat er niet meer aan poorten is

Geen bearer-token-pad. Geen mTLS. Geen tokenintrospectie. Geen enkele poort die zowel een mens identificeert als een beperking op de handeling meedraagt.

## 3. De notities van gezag

### 3.1 Globale allowlist van e-mailadressen

`UserService._allowed_emails` (`opi/services/user_service.py:24`), een `set` in het procesgeheugen. Gevuld bij startup uit drie bronnen (`opi/core/startup.py:469-492`): een hardgecodeerde lijst, de env-variabele `ALLOWED_EMAILS` (komma-gescheiden, `opi/core/config.py:178`) en alle rijen uit de `users`-tabel. Beantwoordt precies één vraag: mag dit e-mailadres überhaupt naar binnen. De middleware stuurt wie er niet op staat naar `/permission-denied` (`opi/middleware/authorization.py:116-119`).

Het laden uit de database staat in een `try/except Exception` die alleen een `logger.warning` schrijft (`opi/core/startup.py:493-494`). Valt de database bij startup weg, dan start OPI met een allowlist die alleen de hardgecodeerde en de env-adressen bevat, zonder dat iets die toestand zichtbaar maakt.

### 3.2 Platformbeheerders

`UserService._platform_admin_emails` (`opi/services/user_service.py:28`), eveneens in het geheugen, gevuld uit een hardgecodeerde lijst en de env-variabele `ADMIN_EMAILS` (`opi/core/startup.py:496-504`, `opi/core/config.py:179`). Niet uit de database: een rij in de `users`-tabel maakt iemand wél toegelaten, nooit beheerder.

Een platformbeheerder is lid van elk project met rol `admin`, want `is_user_authorized_for_project` en `get_user_role_for_project` geven hem voorrang vóór ze het projectbestand raadplegen (`opi/services/project_authorization.py:36-38` en `:56-57`).

### 3.3 Projectrollen in het projectbestand

`users: [{email, role}]`, verplicht beide velden, `additionalProperties: false`, `role` uit de enum `admin | owner | member | developer` (`opi/schemas/project_v2.json:146-154`). De Pydantic-kant heeft geen enum en geeft `developer` als standaard (`opi/forms/models/project_file.py:87-95`).

### 3.4 `organization.role` uit Keycloak: dood

`UserService._enrich_user_info` (`opi/services/user_service.py:88-94`) leest de tokenclaim `organization.role` en zet daaruit `is_admin`, `is_developer` en `is_manager` op het gebruikersobject in de sessie.

**Geen enkele beslissing hangt aan die velden.** Een zoektocht door de hele repository (Python, Jinja2-templates, JavaScript) levert naast de drie schrijvende regels zelf twee soorten treffers op, en geen van beide gebruikt de claim als gezag. De eerste is `opi/web/menu.py:49-56`, dat een eigen lokale variabele `is_admin` gebruikt die uit `is_platform_admin()` komt en niets met deze claim te maken heeft. De tweede zijn de tests: `operations-manager/python/tests/test_user_service.py` leest de drie velden op de regels 56-58, 68, 78-79, 90, 100-102, 166 en 170-171, verspreid over vijf testfuncties (`test_role_flag_admin`, `test_role_flag_administrator_variant_not_recognized`, `test_role_flag_dev`, `test_role_flag_manager_variants`, `test_no_role_flags_when_no_role`). Die tests toetsen alleen dat het schrijven gebeurt zoals bedoeld; ze zijn geen gebruiker van de velden, maar ze vallen wel om zodra de velden verdwijnen. Wie ze opruimt, ruimt die vijf tests mee op, zie fase 0 van het plan van aanpak. De claim wordt wel actief geconfigureerd: `operations-manager/python/scripts/setup_keycloak_client_scope.py:43` en `:66` mappen hem naar het token, en `operations-manager/python/docs/KEYCLOAK_SETUP.md:66-72` beschrijft hoe je hem inricht.

Dit is dus een volledig ingerichte, in het token aanwezige, in de sessie opgeslagen en door geen enkel codepad gebruikte autorisatiebron. Zolang hij bestaat ziet een lezer van de code een rollenmechanisme dat er niet is.

### 3.5 Wat er géén notitie van gezag is, maar er wel op lijkt

Uitnodigingen (`opi/api/invite_routes.py`, `features/invite-system.md`) kennen `roles` en `groups` toe. Die landen in de **Keycloak-realm van het project**, dus in de applicatie die het project draait, en geven geen enkel recht in ZAD zelf. Hetzelfde geldt voor `features/keycloak-realm-roles.md` en voor de toegangsmuur uit `features/authorization-wall.md`. Dat is een andere as: eindgebruikers van een gedeployde applicatie, niet beheerders van een project op het platform. De naamsgelijkenis (`developer` als realmrol én als projectrol) maakt verwarring waarschijnlijk.

### 3.6 Uitspraak over de vier notities

- De **allowlist** blijft nodig zolang Keycloak iedereen met een rijksoverheidsaccount binnenlaat; hij beantwoordt een andere vraag dan de rest.
- **Platformbeheerders** blijven nodig, maar de opslag (procesgeheugen, gevuld uit env plus broncode) is de zwakke plek, niet het begrip.
- **Projectrollen** blijven het hart, maar de enum belooft meer dan hij waarmaakt (zie 4).
- **`organization.role`** is dood en hoort weg. Dat is geen ontwerpkeuze maar opruimwerk; het staat hier omdat het anders bij het ontwerpen als bestaand mechanisme wordt meegeteld.
- **`ApproverScope`** bestaat in deze tak niet en kan dus niet blijven, verdwijnen of samensmelten. Als het begrip elders in ontwikkeling is, moet het bij invoering op de rollen uit 3.3 worden afgebeeld en geen vierde eigen namenlijst introduceren.

## 4. Drie rollenlijsten die niet overeenkomen

| Bron | Waarden |
|---|---|
| JSON-schema, `opi/schemas/project_v2.json:152` | `admin`, `owner`, `member`, `developer` |
| Keuzelijst in het formulier, `opi/forms/visualizers/providers.py:180-213` | `admin`, `developer`, `operator` |
| Wat de handhaving onderscheidt | `admin` of `owner` tegenover al het andere |

De keuzelijst biedt dus een rol aan (`operator`) die het schema weigert, en biedt twee rollen die het schema kent (`owner`, `member`) niet aan. Empirisch nagemeten met `validate_project_schema` uit `opi/core/project_schema.py:52`:

```
users[0].role = "operator"  -> ProjectSchemaError: Veld 'users/0/role': 'operator' is not one of ['admin', 'owner', 'member', 'developer']
users[0].role = "developer" -> geaccepteerd
```

Sinds het opslagpad via `ProjectManager.save_and_commit_project` (`opi/manager/project_manager.py:1350`) en `ProjectStore.save` loopt, wordt het schema vóór elke schrijfactie gecontroleerd (`opi/services/project_store.py:648-660`). "Operator" kiezen levert daarom geen kapot projectbestand op maar een mislukte opslag. Het is een gebroken keuze in de gebruikersinterface, geen gat. Het is wel het bewijs dat er geen enkele bron van waarheid voor rollen is.

De handhaving kent maar twee niveaus. `admin` en `owner` zijn overal inwisselbaar; `member` en `developer` zijn overal inwisselbaar. De enum belooft vier niveaus. Dat is precies het soort belofte waar later een beveiligingsaanname op wordt gebouwd: iemand die `member` leest, gaat ervan uit dat dat minder is dan `developer`, en dat is het niet.

## 5. Waar de gate staat

Er zijn twee gecentraliseerde helpers, drie identieke kopieën van een derde, en een reeks handgeschreven controles.

| Vorm | Waar gedefinieerd | Aantal aanroepen | Wat hij eist |
|---|---|---|---|
| `require_project_edit_access` | `opi/web/project_edit_security.py:28` | 11 | lidmaatschap **en** rol `admin`/`owner` |
| `_require_project_member_access` | `opi/web/router_detail_edit.py:163` | 15 | alleen lidmaatschap, elke rol |
| `_require_admin` (drie losse, identieke definities) | `opi/web/router_user_admin.py:41`, `opi/web/router_subdomain_admin.py:48`, `opi/web/router_usage.py:74` | 10 | platformbeheerder |
| Handgeschreven rolcontrole `user_role not in ["admin", "owner"]` | - | 6 | lidmaatschap en rol, per hand | 
| Handgeschreven lidmaatschapscontrole `is_user_authorized_for_project(...)` | - | 15 | alleen lidmaatschap, per hand |
| Rolcontrole in een Jinja2-template `user_role in ["admin", "owner"]` | - | 13 | bepaalt alleen wat er gerenderd wordt |
| Handgeschreven projectsleutelcontrole | `opi/api/task_router.py:56`, `:159-165` | 3 | kennis van de projectsleutel |
| Handgeschreven namespace-eigendomscontrole | `opi/api/restore_router.py:46` | 5 | de namespace hoort bij het geauthenticeerde project |

De zes handgeschreven rolcontroles staan alle in `opi/web/router.py`, op regel 300, 377, 432, 496, 530 en 2183. Van de vijftien handgeschreven lidmaatschapscontroles staan er veertien in `opi/web/router.py` (294, 373, 428, 492, 526, 644, 988, 1517, 1610, 1678, 1738, 1866, 2177, 2540) en één in `opi/api/logs_websocket_router.py:355`. De aanroepen in `opi/web/project_edit_security.py:41` en `opi/web/router_detail_edit.py:173` tellen daar niet in mee: dat zijn de lichamen van de twee helpers uit de tabel hierboven, geen losse controles.

`features/futures/form-field-rbac.md` benoemt het gevolg al voor de opslagpaden: vergeet één pad en de gate is daar afwezig, zonder dat iets dat opmerkt. Dezelfde redenering geldt breder: er is geen plek waar staat welke routes een gate horen te hebben, dus een ontbrekende gate is alleen te vinden door alles te lezen.

## 6. De rechtencatalogus

Dit is het hart van dit document: welke handelingen kent het platform, op welk object, en wie mag ze vandaag. Gegroepeerd naar object, niet naar router. Alle 158 routes komen in precies één groep terug; de kolom "n" telt de routes van die groep. Een reviewer die een willekeurige route uit `opi/api/` pakt, hoort hem hier terug te vinden.

De kolom **Poort** noemt wat er bewezen moet worden. De kolom **Rol** noemt wat er daarnaast nog wordt geëist; "-" betekent dat er geen rolcontrole is.

Optelling ter controle: 12 + 4 + 8 + 6 + 6 + 6 + 3 + 8 + 21 + 38 + 46 = 158. De drie door FastAPI toegevoegde documentatieroutes in 6.1 tellen daar niet in mee.

### 6.1 Object: platform, publiek

| n | Handeling | Route(s) | Poort | Rol | Controle |
|---|---|---|---|---|---|
| 6 | Levensteken en versie opvragen | `GET /health`, `/healthz`, `/readyz`, `/version`, `/favicon.ico`, `/.well-known/security.txt` | geen | - | `opi/server.py:476, 483, 491, 492, 498, 506`; `/health` en `/healthz` zijn twee gestapelde decorators op één functie |
| 1 | Prometheus-metingen van OPI zelf ophalen | `GET /metrics` | geen | - | `opi/api/prometheus_router.py:23` |
| 4 | Landingspagina, uitleg, architectuur, weigeringspagina lezen | `GET /`, `/about`, `/architecture`, `/permission-denied` | geen | - | `opi/web/router.py:65, 2598, 2610, 76` |
| 1 | Doorverwijzing naar de wizard volgen | `GET /projects/new` | geen | - | `opi/web/router.py:114` |
| 3 | De volledige API-beschrijving lezen | `GET /docs`, `/redoc`, `/openapi.json` | geen | - | `opi/server.py:320-322` |

### 6.2 Object: sessie en identiteit

| n | Handeling | Route(s) | Poort | Rol | Controle |
|---|---|---|---|---|---|
| 3 | Inloggen, terugkeren van Keycloak, uitloggen | `GET /auth/login`, `/auth/callback`, `/auth/logout` | geen (dit ís de poort) | - | `opi/api/auth_routes.py:26, 79, 165` |
| 1 | Eigen sessiegegevens uitlezen | `GET /auth/user` | sessie, in de handler | - | `opi/api/auth_routes.py:224-242` |

### 6.3 Object: uitnodiging

| n | Handeling | Route(s) | Poort | Rol | Controle |
|---|---|---|---|---|---|
| 8 | Uitnodiging openen, via SSO of een identiteitsprovider aanmelden, een lokaal account aanmaken, resultaatpagina zien | `GET/POST /invite/{key}...` | de sleutel in de URL | - | `opi/api/invite_routes.py:302-1000` |

De uitnodigingssleutel is hier het bewijsmiddel. Hij staat in het projectbestand, kan een `expires_at` en een `restrict_domain` hebben (`features/invite-system.md`), en levert rollen in de Keycloak-realm van het project, niet in ZAD.

### 6.4 Object: platformgebruikers

| n | Handeling | Route(s) | Poort | Rol | Controle |
|---|---|---|---|---|---|
| 6 | Platformgebruikers tonen, aanmaken, bewerken, verwijderen | `GET/POST /admin/users...` | SSO | platformbeheerder | `opi/web/router_user_admin.py:41-48`, 6 aanroepen |

Een rij in deze tabel zet iemand op de allowlist bij de volgende startup (`opi/core/startup.py:485-491`), niet direct. Zie `features/user-admin-crud.md`.

### 6.5 Object: subdomeinen en gebruik, platformniveau

| n | Handeling | Route(s) | Poort | Rol | Controle |
|---|---|---|---|---|---|
| 3 | Domeinaanvragen bekijken en goed- of afkeuren | `GET/POST /admin/subdomains...` | SSO | platformbeheerder | `opi/web/router_subdomain_admin.py:48-55` |
| 1 | Gebruik en kosten over alle projecten bekijken | `GET /admin/usage` | SSO | platformbeheerder | `opi/web/router_usage.py:74-81` |
| 2 | Clusterbrede metingen verkennen, metriekennamen per dienst ophalen | `GET /metrics-explorer`, `GET /ui/metrics-explorer/metrics/{service_id}` | SSO | **-** | `opi/web/metrics_explorer_router.py:79, 99` |

De metrics-explorer is de enige platformbrede leeshandeling die aan elke toegelaten gebruiker openstaat. Hij toont de lijst bewaakte diensten en het externe Prometheus-adres.

### 6.6 Object: platformonderhoud

| n | Handeling | Route(s) | Poort | Rol | Controle |
|---|---|---|---|---|---|
| 6 | Voor verwijdering gemarkeerde resources tonen en een markering intrekken; opruiming en reconciliatie starten; wezenrapport opvragen en wezen bevestigen | `GET /api/v2/admin/marked-for-deletion`, `DELETE /api/v2/admin/marked-for-deletion/{mark_id}`, `POST /api/v2/admin/cleanup/trigger`, `POST /api/v2/admin/reconciliation/trigger`, `GET /api/v2/admin/orphans/report`, `POST /api/v2/admin/orphans/confirm` | `ADMIN_API_KEY` | - | `opi/api/admin_router.py:46-218` |

Deze zes handelingen raken elk project op het cluster. Zie `features/yaml-diff-driven-deletion.md` en `features/service-orphan-reconciliation.md`.

### 6.7 Object: federatie

| n | Handeling | Route(s) | Poort | Rol | Controle |
|---|---|---|---|---|---|
| 2 | Peers opvragen en hun gezondheid controleren | `GET /api/federation/peers`, `GET /api/federation/health` | `MASTER_API_KEY` | - | `opi/api/federation_router.py:29, 49` |
| 1 | Een taak aanmaken zonder projectcontext | `POST /api/tasks` | `MASTER_API_KEY` | - | `opi/api/task_router.py:227` |

### 6.8 Object: taak

| n | Handeling | Route(s) | Poort | Rol | Controle |
|---|---|---|---|---|---|
| 3 | Taakstatus opvragen, taken van een project lijsten, een wachtende taak annuleren | `GET /api/tasks/{task_id}`, `GET /api/tasks`, `POST /api/tasks/{task_id}/:cancel` | projectsleutel, handgeschreven | - | `opi/api/task_router.py:56, 119, 159-165, 208` |
| 5 | Voortgang en foutmeldingen van een taak in de interface volgen | `GET /projects/progress/{task_id}`, `GET /ui/tasks/{task_id}/status`, `GET /projects/{p}/task-progress/{task_id}`, `GET /projects/{p}/task-errors/{task_id}`, `GET /projects/{p}/modal-wizard/progress/{task_id}` | SSO | **-** | geen eigenaarscontrole; zie 8.2 |

### 6.9 Object: project: lezen via de interface

| n | Handeling | Route(s) | Poort | Rol | Controle |
|---|---|---|---|---|---|
| 1 | De eigen projectenlijst zien | `GET /projects` | SSO | filtert op lidmaatschap | `opi/web/router.py:2540` |
| 1 | Projectdetails openen | `GET /projects/details/{p}` | SSO | lid (elke rol) | `opi/web/router.py:988` |
| 4 | Resourcegebruik, back-ups, ArgoCD-status en metingen van een deployment lezen | `GET /projects/details/{p}/resource-usage`, `/backups`, `/argocd-status/{d}`, `/metrics/{d}` | SSO | lid | `opi/web/router.py:1517, 1610, 1678, 1738` |
| 1 | Takenoverzicht van een project lezen | `GET /projects/{p}/tasks` | SSO | lid | `opi/web/router_jobs.py:160` |
| 2 | Uitvoeringsvenster van een deployment openen en de status pollen | `GET /projects/{p}/jobs/{d}/modal`, `/status` | SSO | lid | `opi/web/router_jobs.py:191, 199` |
| 2 | Databaseconsole openen en de status pollen | `GET /projects/{p}/db-console/{d}/modal`, `/status` | SSO | lid | `opi/web/router_db_console.py:93, 101` |
| 1 | Logs live meelezen | `WEBSOCKET /api/logs/stream/{p}` | sessie, handgeschreven | lid | `opi/api/logs_websocket_router.py:355` |
| 1 | Dashboard openen | `GET /dashboard` | SSO | - | `opi/web/router.py:619` |
| 1 | Dienstenoverzicht lezen | `GET /services` | SSO | - | `opi/web/services_router.py:22` |
| 1 | Beschikbaarheid van een subdomein controleren | `GET /subdomains/check` | SSO | - | `opi/web/router_self_service.py:49`, geregistreerd via `add_api_route` in `opi/web/router.py:120` |
| 6 | Ontwikkel- en voorbeeldpagina's openen | `GET /projects/roos`, `/test-architecture`, `/test-hero`, `/forms/formulier`, `/test-template-variables`, `/example` | SSO | - | `opi/web/router.py:219, 557, 569, 581, 2647, 2662` |

### 6.10 Object: project: schrijven via de interface

| n | Handeling | Route(s) | Poort | Rol | Controle |
|---|---|---|---|---|---|
| 15 | Een nieuw project doorlopen en indienen, inclusief bijlagen stagen | `GET/POST /forms/wizard/...` | SSO | - bij aanmaken; `admin`/`owner` bij bewerken van een bestaand project | `opi/web/router_wizard.py:432, 752, 1989` |
| 8 | Een projectonderdeel via het bewerkvenster wijzigen, overslaan, bevestigen of herordenen | `GET/POST /projects/{p}/modal-wizard/...`, `POST /projects/{p}/edit/{section_id}/sequence` | SSO | `admin`/`owner`, behalve voor back-up- en herstelstromen: die eisen alleen lidmaatschap (`_is_backup_restore_flow`) | `opi/web/router_detail_edit.py:525, 579-582, 715, 741-743, 970-972, 988-990, 1008, 1036, 1167-1169` |
| 1 | Project verwijderen | `POST /projects/delete/{p}` | SSO | `admin`/`owner` | `opi/web/router.py:294-303` |
| 1 | Deployment verwijderen | `POST /projects/{p}/delete-deployment/{d}` | SSO | `admin`/`owner` | `opi/web/router.py:373-379` |
| 1 | Component verwijderen | `POST /projects/{p}/delete-component/{c}` | SSO | `admin`/`owner` | `opi/web/router.py:428-434` |
| 2 | Project of deployment opnieuw laten verwerken | `POST /projects/{p}/refresh`, `/refresh/{d}` | SSO | `admin`/`owner` | `opi/web/router.py:492-499, 526-533` |
| 2 | Webadresinstellingen van een deployment tonen en opslaan | `GET/POST /projects/{p}/deployments/{d}/domain-settings` | SSO | `admin`/`owner` bij opslaan; lid bij tonen | `opi/web/router.py:1866, 2177-2186` |
| 1 | Bijlage verwijderen | `DELETE /projects/{p}/attachments/{id}` | SSO | `admin`/`owner` | `opi/web/router_attachments.py:36` |
| 2 | Databaseconsole starten en stoppen | `POST /projects/{p}/db-console`, `/db-console/{sid}/stop` | SSO | **lid, elke rol** | `opi/web/router_db_console.py:109, 146` |
| 2 | Uitvoeringssessie starten en stoppen | `POST /projects/{p}/jobs`, `/jobs/{sid}/stop` | SSO | **lid, elke rol** | `opi/web/router_jobs.py:207, 260` |
| 2 | AGE-tekst versleutelen en ontsleutelen met een zelf aangeleverde sleutel | `POST /tools/encrypt`, `/tools/decrypt` | SSO | - | `opi/web/router.py:2734, 2767` |
| 1 | Gereedschapspagina openen | `GET /tools` | SSO | - | `opi/web/router.py:2699` |

De twee regels in vet zijn het opvallendst: een `developer` of `member` kan een databaseconsole en een uitvoeringssessie in de productienamespace van het project starten, terwijl diezelfde persoon een componentnaam niet mag wijzigen. Dat is niet per se fout, maar het is een keuze die nergens is opgeschreven.

### 6.11 Object: project: via de projectsleutel

Alle 46 routes hieronder vragen dezelfde ene sleutel, en geen enkele vraagt iets anders. De sleutel draagt geen rol, dus de kolom "Rol" is voor de hele paragraaf leeg.

| n | Handeling | Route(s) | Waar |
|---|---|---|---|
| 3 | Project verwijderen; deployment verwijderen (v1 en v2) | `DELETE /api/projects/{p}`, `DELETE /api/projects/{p}/{d}`, `DELETE /api/v2/projects/{p}/{d}` | `opi/api/router.py:2198, 2290`, `opi/api/v2/router.py:588` |
| 2 | Deployment aanmaken of bijwerken (v1 en v2) | `POST /api/projects/{p}/:upsert-deployment`, `POST /api/v2/projects/{p}/:upsert-deployment` | `opi/api/router.py:1007`, `opi/api/v2/router.py:475` |
| 2 | Component aan het project toevoegen | `POST /api/projects/{p}/components`, `POST /api/v2/projects/{p}/components` | `opi/api/router.py:1218`, `opi/api/v2/router.py:798` |
| 2 | Component wijzigen | `PATCH /api/projects/{p}/components/{c}`, `PATCH /api/v2/projects/{p}/components/{c}` | `opi/api/router.py:1395`, `opi/api/v2/router.py:864` |
| 2 | Component aan een deployment koppelen | `POST /api/projects/{p}/deployments/{d}/components`, v2 idem | `opi/api/router.py:1512`, `opi/api/v2/router.py:915` |
| 2 | Dienst aan het project toevoegen | `POST /api/projects/{p}/services`, v2 idem | `opi/api/router.py:1669`, `opi/api/v2/router.py:978` |
| 2 | Image van een deployment vervangen | `PUT /api/projects/{p}/deployments/{d}/image`, v2 idem | `opi/api/router.py:1814`, `opi/api/v2/router.py:624` |
| 4 | Project of deployment opnieuw laten verwerken | `GET /api/projects/{p}/:refresh`, `GET /api/projects/{p}/deployments/{d}/:refresh`, `POST /api/v2/projects/{p}/:refresh`, `POST /api/v2/projects/{p}/deployments/{d}/:refresh` | `opi/api/router.py:1992, 2084`, `opi/api/v2/router.py:546, 753` |
| 2 | Deployments van een project opvragen | `GET /api/v2/projects/{p}/deployments`, `GET /api/v2/projects/{p}/deployments/{d}` | `opi/api/v2/router.py:379, 422` |
| 5 | Database of bucket klonen vanuit een externe bron, en zo'n kloon vooraf valideren | `POST .../:clone-database-from-external`, `.../:clone-bucket-from-external`, `.../:validate-clone`, en de v2-varianten `:clone-database` en `:clone-bucket` | `opi/api/router.py:2386, 2549, 2711`, `opi/api/v2/router.py:677, 715` |
| 2 | Registratie van een containerregister toevoegen, via een bestaand secret of via inloggegevens | `POST /api/projects/{p}/registries/by-secret`, `/by-credentials` | `opi/api/router.py:3278, 3373` |
| 1 | Een image naar het platformregister duwen | `POST /api/v1/projects/{p}/images/push` | `opi/api/image_router.py:31` |
| 4 | Back-upstatus opvragen; deployment back-uppen; back-uploopjes lijsten; een momentopname verwijderen | `GET /api/v1/backup/status`, `POST /api/v1/backup/project/{p}/deployment/{d}`, `GET /api/v1/backup/runs/{p}/{d}`, `DELETE /api/v1/backup/snapshot/{p}/{d}/{sid}` | `opi/api/backup_router.py:301, 333, 642, 782` |
| 8 | Momentopnamen lijsten (per namespace en per PVC); een PVC, database of bucket terugzetten; een project of deployment terugzetten, ook uit een specifiek back-uploopje | `GET/POST /api/v1/restore/...` | `opi/api/restore_router.py:442, 485, 526, 634, 1174, 1321, 1435, 1551` |
| 1 | Logs van een project ophalen | `GET /api/logs/{p}` | `opi/api/logs_router.py:34` |
| 2 | Resources bijstellen; resources opschonen | `POST /api/resources/{p}/tune`, `/sanitize` | `opi/api/resource_router.py:66, 101` |
| 2 | Subdomeinbeschikbaarheid controleren; subdomeinregistraties lijsten | `GET /api/subdomains/check/{subdomain}`, `GET /api/subdomains` | `opi/api/router.py:3104, 3199` |
| - | Taak aanmaken zonder projectcontext staat in dezelfde router maar hoort bij federatie en telt daar mee | - | zie 6.7 |

Twee van deze 46 zijn onbereikbaar; zie 8.1.

### 6.12 Wat er in de catalogus ontbreekt

Er is geen enkele handeling met de betekenis "een recht toekennen", "een recht intrekken", "een sleutel roteren", "een sleutel intrekken" of "zien wie wat deed". Dat is geen gat in de dekking van dit document maar in het platform: die handelingen bestaan niet. Het dichtstbijzijnde is het bewerken van de `users`-lijst in het projectbestand, wat een gewone projectbewerking is en dus dezelfde poort en dezelfde afwezige registratie kent als het wijzigen van een poortnummer.

## 7. Sleutels, geheimen en andere bewijsmiddelen

| Bewijsmiddel | Waar het vandaan komt | Vorm | Vervaldatum | Rotatie | Intrekking | Attributie |
|---|---|---|---|---|---|---|
| Projectsleutel | `generate_api_key`, `opi/utils/api_keys.py:24` | 32 tekens uit `ascii_letters + digits`, via `secrets.choice`; AGE-versleuteld in `config.api-key` | geen | geen, anders dan het projectbestand bewerken | geen | geen |
| `ADMIN_API_KEY` | omgeving, `opi/core/config.py:226` | vrije string, standaard `None` | geen | geen | geen | geen |
| `MASTER_API_KEY` | omgeving, `opi/core/config.py:225` | vrije string, standaard `None` | geen | geen | geen | geen |
| Peer-sleutels voor federatie | `FEDERATION_PEERS`, `opi/core/config.py:386` | JSON-string in een omgevingsvariabele, met de `MASTER_API_KEY` van elke peer in platte tekst | geen | geen | geen | geen |
| SSO-sessiecookie | Starlette-sessie, ondertekend met `SECRET_KEY` | cookie | sessieduur | - | uitloggen | e-mailadres in het log van de handler |
| Uitnodigingssleutel | projectbestand, `invites.active[].key` | door de beheerder gekozen string in de URL | optioneel `expires_at` | handmatig | sleutel verwijderen uit het bestand | geen |
| Projectsleutelpaar (AGE) | `config.age-public-key` / `age-private-key` | AGE | geen | geen | geen | - |

Aanvullende feiten:

- `USE_UNSAFE_API_KEY` (`opi/core/config.py:224`) vervangt de gegenereerde sleutel door de vaste waarde `API_TOKEN`, die als hardgecodeerde standaard in de broncode staat (`opi/core/config.py:223`). In die stand schrijft `opi/utils/api_keys.py:39` de sleutel voluit in het debuglog.
- `ADMIN_API_KEY` en `MASTER_API_KEY` worden nergens in `bootstrap/` of `infrastructure/` gezet. Zonder waarde antwoorden de bijbehorende 9 routes met 501 (`opi/api/endpoint_util.py:106-111` en `:147-152`). Waar ze in productie vandaan komen is buiten deze repository geregeld en is hier niet vastgesteld.
- Een mislukte sleutelpoging levert één `logger.warning` met de routenaam en verder niets: geen aanroeper, geen adres, geen teller (`opi/api/endpoint_util.py:36, 49, 114, 155`). Een geslaagde poging levert een `logger.debug`.
- De projectsleutel is leesbaar op de projectdetailpagina, achter een rolcontrole in de template (`opi/templates/project-details/section-config.html.j2:2` en `:38`). De handler ontsleutelt hem echter voor elk lid, ongeacht rol (`opi/web/router.py:1010-1015`), samen met de private AGE-sleutel van het project en de Keycloak-beheerderswachtwoorden.
- Er is één bestaand, declaratief autorisatieobject dat ZAD zelf uitrolt: de ArgoCD `AppProject` per project (`operations-manager/python/manifests/argocd-appproject.yaml.jinja`). Die pint de bestemming op één namespace en één cluster, laat cluster-scoped resources helemaal niet toe, en laat namespaced resources onbeperkt toe binnen die namespace. `sourceRepos` staat op `'*'`.

## 8. De gaten

Elk gat met vindplaats. De volgorde is die van afnemende zekerheid, niet van afnemende ernst.

### 8.1 Twee routes achter de projectsleutel zijn onbereikbaar

`validate_api_token` haalt `project_name` uit de aanroepargumenten en geeft 401 zodra dat ontbreekt (`opi/api/endpoint_util.py:38-43`). Twee routes hebben geen enkele parameter met die naam, ook geen queryparameter:

- `GET /api/subdomains/check/{subdomain}` (`opi/api/router.py:3103-3104`)
- `GET /api/v1/backup/status` (`opi/api/backup_router.py:300-301`)

Beide geven daarom altijd `401 {"detail": "Missing project_name parameter"}`, ongeacht welke sleutel je meestuurt. Nagemeten door beide handtekeningen met dezelfde decorator in een losse FastAPI-app te draaien; beide antwoordden 401. De voorbeelden in de docstrings van beide routes tonen een aanroep zonder `project_name` en kunnen dus nooit gewerkt hebben zoals beschreven.

Voor `check_subdomain_availability` is er een werkend equivalent achter SSO: `check_subdomain_availability_web` (`opi/web/router_self_service.py:49`), de route die via `add_api_route` wordt geregistreerd. Voor `get_backup_status` is er geen equivalent. Merk op dat die route bij herstel wél iets zou lekken: `current_namespace` en `locked_by` zijn platformbrede waarden en horen niet bij één project.

### 8.2 Vijf voortgangsroutes zonder eigenaarscontrole

`GET /projects/progress/{task_id}` (`opi/web/router.py:125`), `GET /ui/tasks/{task_id}/status` (`:183`), `GET /projects/{p}/task-progress/{task_id}` (`:2903`), `GET /projects/{p}/task-errors/{task_id}` (`:2933`) en `GET /projects/{p}/modal-wizard/progress/{task_id}` (`opi/web/router_detail_edit.py:1561`) halen de taak op via `task_service.get_task(task_id)` en renderen hem, zonder ergens te toetsen of de aanroeper lid is van het project waar die taak bij hoort. Bij de twee routes met een `{project_name}` in het pad wordt die naam alleen als weergavewaarde doorgegeven, niet als controle.

De API-tegenhangers doen die controle wél (`opi/api/task_router.py:56-72`). Wat de interfaceroutes beschermt is uitsluitend dat een taak-id een willekeurige UUID is (`opi/core/async_task_schema.py:9`, `gen_random_uuid()`). Dat is geen autorisatie; het is onraadbaarheid. Zodra een id ergens in een log, een foutmelding of een gedeelde URL belandt, is de bijbehorende voortgang en foutdetail leesbaar voor elke toegelaten gebruiker.

### 8.3 Ontsleutelde omgevingsvariabelen zonder rolcontrole

`project_details` ontsleutelt `user-env-vars` van elk component (`opi/web/router.py:1027` en verder) en de template rendert ze via `c-secret-field` zonder rolcontrole (`opi/templates/project-details/section-deployments.html.j2:130-180`, het veld zelf op 167). De overige gevoelige blokken op die pagina staan wél achter een rolcontrole: `section-config`, `section-keycloak`, `section-danger-zone`, `section-actions`, `section-deployment-actions`, `section-team`, `section-services`, `section-components`, `section-header` en `section-attachments`. Samen met de bewerkknoppen in `section-deployments` zijn dat de dertien blokken uit 5.

Gevolg: een `developer` of `member` leest de door de gebruiker gezette geheimen van elke deployment van het project. Of dat de bedoeling is valt te verdedigen, het zijn de geheimen van de applicatie waar die persoon aan werkt, maar het is inconsistent met het feit dat dezelfde persoon de projectsleutel niet mag zien, en het staat nergens als keuze opgeschreven.

### 8.4 De gate zit in de template, niet in de handler

De handler van `GET /projects/details/{p}` eist alleen lidmaatschap (`opi/web/router.py:988`) en ontsleutelt vervolgens de projectsleutel, de private AGE-sleutel en de Keycloak-wachtwoorden in de sjabloonruimte (`:1005-1024`). De rolcontrole staat pas in de dertien Jinja2-blokken. Dat betekent dat elke toekomstige `include`, elke nieuwe sectie en elke sjabloonfout de rolcontrole omzeilt zonder dat er iets in Python verandert. `features/futures/form-field-rbac.md` beschrijft dit patroon al voor de opslagpaden; hier is de leeskant dezelfde constructie.

### 8.5 Dezelfde vraag, verschillend beantwoord

- "Mag deze aanroeper dit project bewerken" wordt beantwoord door `require_project_edit_access` (11 keer) en zes keer met de hand in `opi/web/router.py`.
- "Is deze aanroeper lid" wordt beantwoord door `_require_project_member_access` (15 keer) en vijftien keer met de hand.
- "Is dit een platformbeheerder" wordt beantwoord door drie identieke, los van elkaar gedefinieerde `_require_admin`-functies en één losse aanroep in `opi/web/menu.py:52`.
- "Kent de aanroeper de projectsleutel" wordt beantwoord door de decorator (46 keer), door `_validate_task_api_key` (2 keer) en door een inline blok in `list_tasks` (`opi/api/task_router.py:159-165`).

Vier vragen, negen implementaties. Elke implementatie is een plek waar de volgende wijziging vergeten kan worden.

### 8.6 Eén route in een familie kent de uitzondering niet

De modal-wizard behandelt back-up- en herstelstromen bewust anders: die eisen alleen lidmaatschap, niet `admin`/`owner`. Die uitzondering staat in `modal_wizard_init` (`opi/web/router_detail_edit.py:579-582`), `modal_wizard_submit_step` (`:741-743`), `modal_wizard_skip` (`:970-972`), `modal_wizard_confirm` (`:988-990`) en `_modal_do_submit` (`:1167-1169`).

Hij staat **niet** in `modal_wizard_load_step` (`:710-715`), de route voor terugnavigeren binnen een stroom. Die eist onvoorwaardelijk `admin`/`owner`. Een lid dat een herstelstroom start, doorloopt en bevestigt, krijgt dus 403 zodra het een stap teruggaat.

Dit is precies het gevolg dat 8.5 voorspelt: dezelfde regel, zesmaal met de hand herhaald, vijf keer goed.

### 8.7 De sleutel mag meer dan de mens die hem kreeg

De projectsleutel opent alle 46 API-routes van paragraaf 6.11, waaronder het verwijderen van het project, het terugzetten van back-ups en het vervangen van images. Alleen een `admin` of `owner` kan hem lezen. Maar zodra hij is uitgegeven:

- kan iedereen die hem heeft alles doen wat de rol `admin` mag, zonder ooit als persoon herkend te zijn;
- kan hij niet worden ingetrokken zonder het projectbestand te bewerken, wat een commit en een AGE-hercodering is;
- verloopt hij niet;
- staat in het logboek alleen dat de handeling plaatsvond, niet door wie.

Omgekeerd is er geen enkele API-route die *minder* mag dan de sleutel. Er bestaat dus geen manier om een geautomatiseerde aanroeper alleen leesrecht te geven.

### 8.8 Een persoon in de broncode

`opi/core/startup.py:470` en `:497` zetten hetzelfde persoonlijke e-mailadres neer als standaard-allowlist én als standaard-platformbeheerder. Dat is een met naam genoemde superuser in de broncode van een productieplatform, actief in elke omgeving, ongeacht configuratie, en niet uit te schakelen zonder de code te wijzigen. Hij is bovendien onzichtbaar voor iedereen die alleen naar de env-variabelen of de `users`-tabel kijkt.

### 8.9 `/metrics` staat op de publieke ingress

De middleware slaat autorisatie voor `/metrics` expliciet over (`opi/middleware/authorization.py:27`), de route zelf heeft geen poort (`opi/api/prometheus_router.py:23`) en de productie-ingress stuurt het hele pad `/` naar de dienst (`bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/ingress.yaml:16-17`). Er is in deze repository geen NetworkPolicy, ingress-annotatie of snippet gevonden die `/metrics` alsnog afschermt. `features/metrics-endpoint-security.md` beschrijft precies waarom dat een probleem is, maar behandelt alleen Keycloak en toekomstige gebruikersapplicaties, niet OPI zelf.

**Niet geverifieerd:** dit is afgeleid uit configuratie, niet gemeten tegen een draaiend productiecluster. Er kan iets vóór de ingress staan dat het pad blokkeert.

### 8.10 De allowlist kan stil kleiner worden

Zie 3.1: het laden van de `users`-tabel staat in een brede `except Exception` met alleen een waarschuwing. De toestand "OPI draait, maar de helft van de mensen kan er niet in" is niet te onderscheiden van "OPI draait".

### 8.11 Een ingerichte, gelezen, nooit gebruikte rolclaim

Zie 3.4. `organization.role` staat in het token, wordt in de sessie opgeslagen als `is_admin`/`is_developer`/`is_manager` en beïnvloedt geen enkele beslissing; alleen `tests/test_user_service.py` leest de velden, om het schrijven zelf te toetsen.

## 9. Wat BIO2 hiervan vindt

**Voorbehoud:** de skill `bio` was in deze omgeving niet geïnstalleerd. De onderstaande koppeling is gemaakt tegen `plans/bio2-compliance-analysis.md` in deze repository, dat BIO2 beschrijft als volgend op de ISO 27002:2022-structuur, en tegen die structuur zelf. **De nummering en formulering zijn niet tegen de officiële BIO2-tekst gecontroleerd** en moeten dat alsnog worden voordat er conclusies over naleving op worden gebaseerd.

| Beheersmaatregel | Wat hij vraagt | Stand hier |
|---|---|---|
| A5.15 Toegangsbeveiliging | een vastgesteld beleid dat bepaalt wie waartoe toegang heeft | er is geen beleid en geen rechtencatalogus; toegang is een emergent gevolg van welk codepad je bereikt |
| A5.16 Identiteitsbeheer | elke identiteit is uniek en herleidbaar tot een persoon of dienst | de sessie voldoet; de sleutels uit 7 niet, die identificeren niemand |
| A5.17 Authenticatie-informatie | uitgifte, wijziging en intrekking van geheimen is geregeld | uitgifte is geregeld, wijziging is een commit, intrekking bestaat niet |
| A5.18 Toegangsrechten | rechten worden toegekend, periodiek beoordeeld en ingetrokken | toekennen kan (de `users`-lijst), beoordelen kan niet (geen overzicht), intrekken werkt niet voor sleutels |
| A8.02 Speciale toegangsrechten | beperkt, apart geregistreerd, apart bewaakt | platformbeheerder komt uit env plus broncode, staat niet in de database, wordt niet geregistreerd; zie 8.8 |
| A8.03 Beperking toegang tot informatie | toegang tot informatie is beperkt tot wat nodig is | grotendeels op orde per project (namespaces, `_require_namespace_owned_by_project`), maar binnen een project ontbreekt de beperking; zie 8.3 |
| A8.05 Veilige authenticatie | sterke authenticatie voor toegang | SSO voldoet; een gedeeld statisch geheim in een header voldoet niet |
| A8.15 Logbestanden | vastleggen wie welke handeling wanneer deed | bestaat niet voor autorisatiebeslissingen; zie 6.12 en de parallelle taak RC-149 |

Twee eisen die hier naar mijn oordeel **niet** van toepassing zijn, met de reden:

- Eisen rond fysieke toegang en werkplekbeveiliging raken dit onderwerp niet; ZAD kent geen fysieke component.
- Eisen rond betrouwbaarheidsniveaus voor burgerauthenticatie (eIDAS, DigiD) gelden hier niet: ZAD authenticeert medewerkers van de rijksoverheid tegen een interne Keycloak, geen burgers. Die eisen worden wél relevant zodra een gedeployde applicatie ze nodig heeft, en dat is de as uit 3.5, niet deze.

## 10. Wat dit document niet is

Het is een momentopname, geen mechanisme. Zolang de rechten alleen bestaan als "welk codepad bereik je", veroudert elke opsomming ervan bij de eerstvolgende route. De AST-analyse waarmee de tellingen zijn gemaakt is reproduceerbaar maar staat niet in de repository; dat is met opzet, want een telling is geen norm. Wat wel houdbaar zou zijn, een expliciete lijst handelingen waar de code aan getoetst kan worden, is precies het onderwerp van [rechten-modellen-en-tokens.md](rechten-modellen-en-tokens.md).

## Verwante documenten

- [rechten-modellen-en-tokens.md](rechten-modellen-en-tokens.md), invalshoeken, referentiemodellen, tokens, agents, AuthZEN
- [rechten-plan-van-aanpak.md](rechten-plan-van-aanpak.md), aanbeveling, fasering, migratie, te nemen besluiten
- [form-field-rbac.md](form-field-rbac.md), rolcontrole op veldniveau in het formulierensysteem
- [tenant-isolation-followups.md](tenant-isolation-followups.md), openstaande punten rond scheiding tussen projecten
- `features/user-admin-crud.md`, beheer van de `users`-tabel
- `features/invite-system.md`, `features/keycloak-realm-roles.md`, `features/authorization-wall.md`, `features/zad-external-user-support.md`, de andere as: eindgebruikers van gedeployde applicaties
- `features/metrics-endpoint-security.md`, afscherming van metingsendpoints
- `features/yaml-diff-driven-deletion.md`, `features/service-orphan-reconciliation.md`, `features/federation-routing.md`, `features/async-task-system.md`, de handelingen achter de gedeelde platformgeheimen
