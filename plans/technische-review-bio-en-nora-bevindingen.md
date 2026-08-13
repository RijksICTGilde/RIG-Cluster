# Technische review BIO en NORA: bevindingen

Status: bevindingen, 13 augustus 2026. Reviewopdracht uit `plans/technische-review-bio-en-nora.md`. Dit is een oordeel met bewijs, geen wijziging aan de code. Toetsingskader: BIO2 v1.3 (definitief 9 januari 2026), controlstructuur NEN-EN-ISO/IEC 27002:2022. NORA is op het niveau van de kwaliteitseigenschappen vertrouwelijkheid, integriteit, onweerlegbaarheid en controleerbaarheid meegewogen.

Werkwijze: vier onderzoekssporen (multi-tenancy, autorisatie, netwerk, audit) plus een eigen sweep op geheimen. Elke bevinding die hieronder als constatering staat, is tegen de daadwerkelijke code geverifieerd, niet alleen gelezen. Waar ik iets niet kon vaststellen staat dat er expliciet bij.

## Kernoordeel

De fundamenten staan er goed op. De API-sleutelbinding, de server-side namespace-afleiding in de restore- en backup-paden, de centrale fail-closed CSRF-laag, de AGE-versleutelde opslag van sleutels met timing-safe vergelijking en de verplichte tenant-baseline-NetworkPolicy per deployment zijn stuk voor stuk correct en consistent doorgevoerd. Dat is de helft van het werk die niet opnieuw bedacht hoeft te worden.

De scheiding tussen tenants, het hart van dit systeem, leunt op drie plekken echter op een naam of op een gedeelde ruimte in plaats van op een afgedwongen grens. Het onderscheid zit in de bereikbaarheid. Alleen de registry-bevinding (A) is door een gewone geregistreerde gebruiker te bereiken, want die gebruikt de door de aanroeper opgegeven `image_name` en `tag`, allebei vrij per request. De twee naamgebaseerde bevindingen (B, namespace-suffix; C, identifier-collisie) vereisen dat de aanvaller de projectnaam kan kiezen, en dat kan een self-service gebruiker niet: de wizard genereert de technische `name` uit de display-naam als `{initialen}-{3 random tekens}` via `generate_project_name` (`opi/utils/project_names.py`), `name` is geen bewerkbaar veld (`opi/configs/project-template.yaml:3` somt de editable-velden op, `name` staat er niet tussen), en de create-project-API is uitgeschakeld (`opi/api/router.py:2045`, uitgecommentarieerd). Een projectnaam letterlijk zetten kan alleen via een directe commit naar de `zad-projects`-repo, en die schrijft alleen OPI zelf (één servicecredential), niet de gebruiker. B en C zijn daarmee latente ontwerpzwakheden voor een beheerder of een toekomstige feature die een naam wél verbatim doorlaat, niet iets dat een tenant vandaag kan uitbuiten.

De `@requires_sso`-guard op `opi/web/router_wizard.py:2109` laat elke ingelogde gebruiker een project aanmaken, maar de naam wordt gegenereerd, niet getypt; de gebruiker kiest alleen de display-naam.

Rangschikking op zwaarte:

| # | Weging | Kern | Vindplaats |
|---|---|---|---|
| A | Kritiek | Image-push overschrijft de container-image van een ander project in de gedeelde registry, met code-executie in diens namespace als gevolg | `opi/connectors/skopeo.py:117`, `opi/api/image_router.py:30` |
| B | Laag (latent) | Projectnaam `{x}-infrastructure` valt samen met de dedicated-Postgres-infranamespace van project `x`; alleen bereikbaar via een verbatim naam in git (admin/OPI), niet via de wizard | `opi/utils/naming.py:951`, `opi/core/cluster_config.py:652`, `opi/services/project_store.py:381` |
| C | Laag (latent) | DB-, MinIO- en Redis-identifiers van twee projecten kunnen samenvallen; idem, vereist een verbatim gekozen naam die de wizard niet toelaat | `opi/utils/naming.py:457,503`, `opi/manager/database_manager.py:322` |
| D | Middel | Dev-secrets in versiebeheer: een gecommit dev-`.env` met plaintext token en `admin`-wachtwoord, plus twee losse restanten (een verouderde SOPS-oefenmap met een testsleutel, een ingetrokken dev-sleutel in de historie) | `operations-manager/python/.env`, `sops-sandbox/` |
| E | Middel | Wie een wijziging deed is achteraf niet uit de git-commit te herleiden; het indirecte spoor vervalt na 1 uur; Keycloak-auditevents staan niet aan op bestaande realms | `opi/connectors/git.py:53`, `opi/core/config.py:357`, `opi/manager/keycloak_manager.py:895` |
| F | Middel | Allow-all-NetworkPolicy staat sinds 11 juni in de operations-namespace en maskeert de al aanwezige restrictieve policies | `bootstrap/rig-system/kustomize/overlays/odcn-production/network-policies/emergency-restore-allow-all.yaml` |
| G | Laag | Volledige API-sleutel in de DEBUG-log als `USE_UNSAFE_API_KEY` aanstaat (uit in productie) | `opi/utils/api_keys.py:39` |
| H | Laag | Wizard-attachment-endpoints checken tokenbezit, niet projectlidmaatschap | `opi/web/router_wizard_attachments.py:144` |
| I | Informatief | CSP met `unsafe-inline`, geen COOP/COEP/CORP; productie-ingress niet VPN-beperkt (bewuste keuze) | `opi/middleware/security_headers.py:56`, `opi/core/cluster_config.py:152` |

Over de bewust genomen restore-risicoafweging (externe bestemming zonder eigendomscheck): ik acht die verdedigbaar. Zie de aparte sectie onderaan.

## Bevindingen

### A. Registry-poisoning: cross-tenant code-executie via image-push (Kritiek)

**Wat.** De endpoint `POST /api/v1/projects/{project_name}/images/push` staat correct achter `@validate_api_token`, dus de sleutel moet bij `project_name` horen. Maar de registry-bestemming wordt uitsluitend uit de door de aanroeper opgegeven `image_name` en `tag` gebouwd, zonder enige relatie tot het project. In `opi/connectors/skopeo.py:117` is `_build_destination` gelijk aan `docker://{REGISTRY_URL}/{REGISTRY_ORG}:{image_name}-{tag}`, waarbij `REGISTRY_ORG` een globale, platform-brede robot-account-repo is (`opi/core/config.py:291`). Het commentaar in de code legt uit waarom de tagruimte plat is: Quay ondersteunt geen geneste repos onder één robot-account-scope. De projectnaam komt in die bestemming niet voor.

**Waar.** `opi/connectors/skopeo.py:117`, endpoint `opi/api/image_router.py:30`. Default `imagePullPolicy: Always` in `opi/manager/project_manager.py:5345` en `manifests/deployment.yaml.jinja:76`. De image-referentie van een component is vrij tekst (`opi/manager/project_manager.py:394` e.v.), er is geen per-project registry-ACL.

**Wat er mis kan gaan.** Project B draait een component met tag `backend-latest`. Project A, met alleen zijn eigen geldige API-sleutel, roept `push?image_name=backend&tag=latest` aan met een eigen kwaadaardige tarball. Skopeo schrijft naar exact dezelfde gedeelde tag. Bij de eerstvolgende herstart of rollout van project B trekt Kubernetes, dankzij `imagePullPolicy: Always` en het ontbreken van digest-pinning, de image van A binnen en voert die uit in de namespace van B. Dat is code-executie in de tenant van een ander, bereikbaar met niets meer dan een eigen geldige sleutel. Ook zonder overschrijven bestaat een leesrisico: elk project kan elk bekend `image:tag` als eigen deployment-image opgeven en zo de image van een ander pullen.

**BIO/NORA.** BIO2 8.03 (Beperking toegang tot informatie), overheidsmaatregel 8.03.01: "Er zijn maatregelen genomen die het fysiek en/of logisch isoleren van informatie met specifiek belang waarborgen." De gedeelde tagruimte doorbreekt die logische isolatie. Daarnaast 8.19 (Installeren van software op operationele systemen), doel "de integriteit van operationele systemen garanderen": een tenant kan de software in de draaiende omgeving van een andere tenant vervangen. NORA: schending van integriteit en vertrouwelijkheid tussen afnemers.

**Weging.** Kritiek. Dit is het enige gevonden pad met een werkende, end-to-end code-executie-uitkomst tussen tenants, bereikbaar door elke geregistreerde gebruiker. De platte tagruimte is een bewuste ontwerpkeuze om een registry-beperking, maar de eigendomscontrole die er dan bij hoort (tag pinnen op `{project_name}-...`, of weigeren als de tag al van een ander is, zoals `_require_namespace_owned_by_project` dat in het restore-pad wél doet) ontbreekt.

### B. Namespace-collisie via de `-infrastructure`-suffix (Hoog)

**Wat.** De applicatienamespace van een project volgt uit de projectnaam: `get_prefixed_namespace(cluster, project_name)`. De dedicated-Postgres-infranamespace van een project volgt uit dezelfde functie met een afgeleide basis: `get_prefixed_namespace(cluster, "{project}-infrastructure")` (`opi/utils/naming.py:951`, `opi/core/cluster_config.py:652`). Die infranamespace bestaat alleen als het project een eigen CNPG-cluster gebruikt (`opi/services/postgres_scope.py:47`). Projectnaam-uniekheid wordt uitsluitend als exacte string vergeleken (`opi/services/project_store.py:381`), er is geen reserved-suffix-check en het Namespace-object krijgt geen project-identiteitslabel (`manifests/namespace.yaml.jinja` zet alleen `created-by: operations-manager`).

**Waar.** Zie de vindplaatsen hierboven, plus `opi/manager/project_manager.py:1904` waar "namespace bestaat al" alleen gelogd wordt en de flow doorgaat zonder eigendomscheck.

**Wat er mis kan gaan.** Project `foo` gebruikt een dedicated Postgres-cluster en heeft dus infranamespace `rig-foo-infrastructure` (met daarin het superuser-secret en de CNPG-pods). Een andere gebruiker registreert een volkomen geldig nieuw project met de naam `foo-infrastructure`. De applicatienamespace daarvan is óók `rig-foo-infrastructure`. De manifesten van het tweede project worden via ArgoCD in dezelfde fysieke namespace gesynchroniseerd als de private database-infrastructuur van het slachtoffer. De tenant-baseline-policy gebruikt voor een infranamespace `podSelector: {}` (`manifests/tenant-baseline-network-policy.yaml.jinja:20`), dus elke pod die daar landt mag met de bestaande Postgres-pods praten. Het naamgat wordt zo direct een netwerktoegangsgat naar de database van een ander.

**BIO/NORA.** BIO2 8.03.01 (logische isolatie) en 8.22 (Netwerksegmentatie), overheidsmaatregel 8.22.01: "Alle gescheiden groepen hebben een gedefinieerd beveiligingsniveau." Twee tenants delen hier onbedoeld één segment. NORA: vertrouwelijkheid en integriteit van de tenantscheiding.

**Bereikbaarheid (belangrijke inperking).** Dit vereist dat de aanvaller een project met de naam `{slachtoffer}-infrastructure` kan laten bestaan, en dat kan een self-service gebruiker niet: de wizard genereert de `name` als `{initialen}-{3 random tekens}` (`generate_project_name`), `name` is geen bewerkbaar veld, en de create-API is uit. De enige weg naar een verbatim naam is een directe commit naar `zad-projects`, die alleen OPI zelf schrijft. Een gegenereerde naam kan de vereiste vorm (`foo-infrastructure`, meerdere koppeltekens, langer dan de 20-tekenlimiet van de generator) sowieso niet aannemen.

**Weging.** Laag (latent). De collisie is echt en bewezen (zie meetnotitie onderaan), maar niet door een tenant uit te buiten. Het blijft de moeite waard om af te dekken als defense-in-depth, want het leunt nu impliciet op de naamgenerator in plaats van op een expliciete grens: een reserved-suffix-check bij projectaanmaak plus een project-eigendomslabel op het Namespace-object zouden het structureel dichtzetten, ongeacht via welk pad een naam ooit binnenkomt. Voorwaarden voor de theoretische collisie: het slachtoffer gebruikt dedicated Postgres, en de slachtoffernaam is kort genoeg dat `{naam}-infrastructure` binnen 30 tekens past.

### C. Identifier-collisie op gedeelde database-, MinIO- en Redis-servers (Hoog)

**Wat.** `_sanitize_for_identifier` vervangt in elke input apart koppeltekens door underscores (`opi/utils/naming.py:457`), en de identifier wordt gebouwd als `{project}_{deployment}` zonder scheidingsteken dat de twee delen ondubbelzinnig houdt (`opi/utils/naming.py:503` voor de databasegebruiker, vergelijkbaar voor schema, databasenaam, MinIO-gebruiker, bucket en Redis-prefix). Zowel projectnaam als deploymentnaam mogen koppeltekens bevatten.

**Waar.** `opi/utils/naming.py:457,503,522,589,679,742,698`. De credential-rotatie bij collisie: `opi/manager/database_manager.py:322` roept bij een reeds bestaande gebruiker onvoorwaardelijk `update_user_password` aan; `opi/manager/minio_manager.py:356` verwijdert en herschept de bestaande MinIO-gebruiker met een nieuwe secret key. Er is geen eigendomsregistratie die vastlegt welk project welke gebruiker bezit.

**Wat er mis kan gaan.** Slachtoffer: project `a`, deployment `b-c`, databasegebruiker `a_b_c`. Aanvaller: project `a-b`, deployment `c`, databasegebruiker eveneens `a_b_c`. Bij het provisionen van het tweede project wordt het wachtwoord van de bestaande gebruiker herschreven en als "eigen" credential aan de aanvaller teruggegeven, terwijl het in Kubernetes opgeslagen secret van het slachtoffer ongeldig wordt. De aanvaller krijgt geldige toegang tot de bestaande data van het slachtoffer, en het slachtoffer verliest toegang (denial-of-service). Dit speelt op de standaardconfiguratie, want `postgresql-database` (gedeelde server) is de default; alleen `namespace-postgresql-database` (dedicated) ontsnapt eraan (`opi/core/config.py:342`).

**BIO/NORA.** BIO2 8.03.01 (logische isolatie), 8.03.02 (uitsluitend de eigen informatie inzien), 8.05 (Beveiligde authenticatie: de credential van de één wordt hier aan de ander uitgereikt). NORA: vertrouwelijkheid, integriteit en beschikbaarheid tegelijk geraakt.

**Bereikbaarheid (belangrijke inperking).** Zelfde als bij bevinding B: de aanvaller zou een project met een specifiek gekozen naam moeten kunnen laten bestaan (bijvoorbeeld `a-b` naast `a`), en de wizard genereert die naam met een random postfix, `name` is niet bewerkbaar, en de create-API is uit. Alleen een verbatim naam via een directe `zad-projects`-commit (OPI/admin) zou de identifiers laten samenvallen.

**Weging.** Laag (latent). De ambiguïteit in de identifier-opbouw is echt en de credential-overschrijving bij collisie ook, maar de precondition (een gekozen colliderende projectnaam) is niet door een tenant te zetten. Waard om af te dekken bij een toekomstige verbatim-naam-weg: een ondubbelzinnig scheidingsteken of een lengte-veilige hash in de identifier-opbouw. Naast de hyphen-ambiguïteit kan ook de afkapping op 63 tekens (`_truncate_if_needed`) bij lange namen tot collisies leiden, ook zonder opzet.

### D. Sleutelmateriaal in versiebeheer (Middel)

**Wat en waar.** Drie gevallen naast het sessiekoekje dat al is opgeruimd:

- `sops-sandbox/` is een verouderde SOPS-oefenmap uit de begindagen van de repo (twee commits ooit, sinds oktober 2025 niet meer aangeraakt, nergens door tooling gebruikt). Er staat een AGE-testsleutel plain in (`sops-key.txt` en `sops-secret-for-in-namespace.yaml`, publieke helft `age1fdup3p8...`) die alleen de twee demobestanden in diezelfde map ontsleutelt. Het is niet de productiesleutel en niet de sandboxsleutel, en het botst met het bedoelde model waarin sleutels bij setup in het gitignored `security/` worden gegenereerd (`Taskfile.yaml:226`). Laag belang; opruimen is de map schrappen wanneer het uitkomt.
- `operations-manager/python/.env` staat op `origin/main` met `ARGOCD_PASSWORD=admin` en een plaintext 32-hex `API_TOKEN`. De lange git-serverwachtwoorden zijn wél `base64+age:`-versleuteld (goed), en productie draait op een aparte SOPS-versleutelde overlay die dit token niet hergebruikt (geverifieerd). Het is een dev-artefact, maar dev-geheimen horen niet in git.
- De ingetrokken dev-AGE-sleutel (`04ca3bc2`) is uit de werkboom, maar staat nog in de historie en heeft op GitHub gestaan. De aantekening in de repo klopt: ongebruikt en ingetrokken.

**BIO/NORA.** BIO2 8.24 (Gebruik van cryptografie), doel "correct en doeltreffend gebruik ... van cryptografie", met 8.24.02 dat vraagt om te weten waar welke sleutels staan en wie verantwoordelijk is. Sleutelmateriaal in publiek versiebeheer is daar rechtstreeks mee in strijd. Ook 5.17 (Authenticatie-informatie) en 8.12 (Voorkomen van gegevenslekken). NORA: vertrouwelijkheid.

**Weging.** Middel, en dat leunt op de dev-`.env`; de twee restanten (oefenmap-testsleutel, ingetrokken dev-sleutel) zijn laag. Geen van de gevonden waarden ontsluit productie of de echte sandbox. De ene maatregel die het geheel afdekt is een pre-commit secret-scan, zodat een volgende dev- of demowaarde niet ongemerkt meelift; de bestaande gevallen zijn opruimwerk, geen incident.

### E. Herleidbaarheid van wijzigingen en bewaartermijn (Middel)

**Wat.** Voor de meeste projectwijzigingen is achteraf niet uit de git-commit te halen wie ze deed. Elke commit die OPI naar `zad-projects`, `zad-argo-user-applications` en `zad-deployments` schrijft, draagt een vast systeemaccount als auteur (`GIT_COMMIT_AUTHOR_NAME = "Operations Manager"`, `opi/connectors/git.py:53`), en de commit-message beschrijft alleen wat er gebeurde, niet wie het aanvroeg. De handelende gebruiker staat wél in de `async_tasks`-tabel (`created_by`, `opi/core/task_helpers.py:53`), maar die rij wordt standaard na 1 uur opgeruimd (`TASK_WORKER_CLEANUP_RETENTION_HOURS = 1`, `opi/core/config.py:357`). Daarna is de koppeling commit-tot-gebruiker alleen nog via tijdcorrelatie in Loki te maken, en de logregels dragen lokale Amsterdamse tijd zonder offset (`TZ=Europe/Amsterdam` in de Dockerfile, geen tz-aanduiding in het logformaat, `opi/utils/logging_config.py:48`), wat die correlatie extra foutgevoelig maakt rond de DST-overgang.

**Uitzonderingen die het wél goed doen.** De `runs`-tabel houdt `started_by`/`ended_by` blijvend bij voor db-console- en job-runs (`opi/services/runs_service.py`). Approval-verdicts krijgen een `by`-veld dat mee de proj-YAML in gecommit wordt en dus permanent in de git-inhoud staat (`opi/services/approvals.py:120`). Login, logout en autorisatieweigeringen worden mét e-mail gelogd zonder secrets.

**Keycloak-auditevents.** De blueprints zetten `eventsEnabled`/`adminEventsEnabled` aan met 90 dagen retentie (alle 6 realm-configs in `opi/configs/keycloak/`). Maar de reconcile-flow voor een reeds bestaand projectrealm roept `create_realm()` niet aan (`opi/manager/keycloak_manager.py:895` e.v.) en geen van de `_ensure_*`-functies raakt de events-instelling. Er is, anders dan voor het vergrendelen van identiteitsvelden, geen `ensure_realm_events()`-tegenhanger. Elk projectrealm dat vóór 20 juli 2026 is aangemaakt heeft de auditevents dus nooit met terugwerkende kracht aangekregen. Dit bevestigt de aanname dat het op productierealms nog niet aan staat, met de precieze code-oorzaak erbij.

**BIO/NORA.** BIO2 8.15 (Logging), overheidsmaatregel 8.15.01, die een logregel voorschrijft met minimaal actie, object, resultaat, oorsprong, **actor** en tijdstempel. Precies de actor ontbreekt in het git-spoor, en het aanvullende spoor dat de actor wél heeft vervalt in de default na 1 uur, wat op gespannen voet staat met 8.15.04 (bewaartermijn risicogericht bepalen, rekening houdend met aanvallers die langdurig binnen zijn). Ook 8.17 (Kloksynchronisatie) raakt de tijdzone-ambiguïteit, al is dat een leesbaarheids- en correlatiepunt, niet per se een synchronisatiefout. NORA: onweerlegbaarheid en controleerbaarheid.

**Weging.** Middel. Voor de dagelijkse werking is er genoeg spoor, maar voor forensisch onderzoek na een incident is "wie deed deze wijziging" op de meeste projectmutaties niet betrouwbaar te beantwoorden zodra het uur voorbij is. De goedkoopste verbetering is de handelende gebruiker in de commit-trailer of commit-message opnemen (dat spoor is dan zo permanent als de git-historie), en de retentie van `async_tasks` te verhogen.

### F. Allow-all-NetworkPolicy in de operations-namespace (Middel)

**Wat en waar.** `bootstrap/rig-system/kustomize/overlays/odcn-production/network-policies/emergency-restore-allow-all.yaml` is een echte allow-all (`podSelector: {}`, lege ingress- en egressregel) en staat er sinds 11 juni 2026 (commit `39be4995`), ruim twee maanden. Belangrijke nuance: de kustomize-transformer scoped hem op `rig-prd-operations`, de platform/operations-namespace (ArgoCD, Keycloak, OPI, external-dns, gedeelde datastores), niet op de projectnamespaces. NetworkPolicies zijn additief, dus deze policy maskeert de al aanwezige restrictieve policies in diezelfde namespace (`argocd-network-policy.yaml`, `network-policy.yaml` voor OPI), die daardoor nu decoratief zijn.

**BIO/NORA.** BIO2 8.22 (Netwerksegmentatie), 8.22.01 (gedefinieerd beveiligingsniveau per gescheiden groep). Binnen de operations-namespace is dat niveau nu "alles mag". NORA: verdedigbaarheid, defense-in-depth.

**Weging.** Middel. De tenantnamespaces zijn niet geraakt (die vallen onder de verplichte tenant-baseline-policy, die wél handhaaft op productie via Calico), dus de blootstelling zit op de gedeelde infra, niet op de projectscheiding. Het verwijderplan staat in het bestand zelf; de per-component policies voor keycloak, external-dns, minio, redis en rig-db die het plan vereist, kon ik niet in de repo terugvinden, dus stap 1 tot 3 van dat plan lijken nog niet af.

### G tot I. Kleinere en informatieve punten

- **G (Laag).** `opi/utils/api_keys.py:39` logt de volledige API-sleutel op DEBUG als `USE_UNSAFE_API_KEY` aanstaat. In productie staat de vlag uit (`configmap.yaml:43`, `USE_UNSAFE_API_KEY=false`), dus het pad is niet actief, maar de `opi`-logger staat onvoorwaardelijk op DEBUG, dus één omgevingsvlag scheidt dit van een sleutel in de log. Raakt BIO2 8.15.02 ("een logregel bevat nooit gegevens die tot het doorbreken van de beveiliging kunnen leiden").
- **H (Laag).** `opi/web/router_wizard_attachments.py:144` e.v.: de wizard-attachment-endpoints steunen alleen op bezit van het wizard-token (uuid4), zonder de `is_user_authorized_for_project`-check die de analoge routes in `opi/web/router_detail_edit.py` wél doen. Exploitatie vereist dat een 128-bit token lekt. Inconsistent toegepast controlepatroon, geen werkend gat.
- **I (Informatief).** CSP met `unsafe-inline` op `script-src` en `style-src` (`opi/middleware/security_headers.py:56`), bewuste keuze voor inline htmx-handlers, maar het ondermijnt de XSS-mitigatie van CSP. Geen COOP/COEP/CORP-headers. De productie-ingress `ip_whitelist` staat op `0.0.0.0/0` met de bedoelde "VPN only: 147.181.0.0/16" ernaast als commentaar (`opi/core/cluster_config.py:152`); dit is gedocumenteerd als bewuste risicoafweging in `features/bio-network-access-no-vpn-compliance.md` (identity-first, SSO en MFA compenseren, beheerplane apart afgeschermd). De ArgoCD-Route is wél VPN-beperkt.

## De bewust genomen restore-risicoafweging: mijn oordeel

De opdracht vraagt te wegen of het verdedigbaar is dat bij een restore naar een externe bestemming (host, gebruiker, wachtwoord) niet wordt gecontroleerd of die bestemming bij de aanroeper hoort. Ik heb `opi/api/restore_router.py` doorgelezen en vind het verdedigbaar, om drie redenen die in de code kloppen:

- De aanroeper moet de externe credentials al kennen; het platform reikt ze niet uit. De all-or-nothing-validator (`opi/api/restore_router.py:243`) voorkomt dat een half opgegeven doel stilzwijgend in de eigen database belandt.
- Het pad naar de eigen resource is stevig begrensd: `_require_namespace_owned_by_project` (`opi/api/restore_router.py:61`) pint elke namespace-gebonden restore op `get_prefixed_namespace(cluster, project_name)`, en de eigen credentials worden server-side uit het deployment-secret in de eigen namespace gelezen, nooit gelogd en nooit in een foutmelding herhaald (`opi/api/restore_router.py:1436` e.v.).
- De restore-pod draait in de eigen namespace onder de eigen NetworkPolicy.

BIO-kader: 8.03 gaat over toegang tot informatie die de entiteit beheert. Een externe bestemming die de aanroeper zelf aandraagt en zelf al kan bereiken, valt buiten de gegevens die ZAD beschermt; ZAD faciliteert hooguit een uitgaande verbinding die de gebruiker ook zonder ZAD kon leggen. Het restpunt dat ik zou benoemen is niet vertrouwelijkheid maar uitgaand verkeer: de restore-pod mag op poort 80/443 naar `0.0.0.0/0` (de tenant-baseline laat dat bewust open), dus een externe restore-bestemming is technisch overal te bereiken. Dat is dezelfde open-egress-keuze die al gedocumenteerd is, geen nieuw gat. Kortom: verdedigbaar, mits de open egress een bewuste, opgeschreven keuze blijft.

## Wat goed staat (niet opnieuw bedenken)

- API-sleutelbinding: `validate_api_token` (`opi/api/endpoint_util.py:14`) vereist de sleutel bij `project_name`, vergelijkt timing-safe met `secrets.compare_digest` en overschrijft `project_name` met de canonieke naam uit de store, wat path-traversal via de projectnaam afvangt.
- Server-side namespace-afleiding in restore, backup, logs en tasks: nooit uit een client-gecontroleerd padsegment, altijd uit `project.data` of via de eigendomscheck. `_require_namespace_owned_by_project` is het patroon dat elders (registry) node ontbreekt.
- Task-polling en `list_tasks` (`opi/api/task_router.py`) toetsen sleutel-tot-project vóór het filteren; een sleutel van A haalt geen tasks van B op.
- CSRF: centrale, fail-closed middleware (`opi/utils/csrf.py`) met double-submit plus Origin/Referer-check op elke unsafe method, smalle expliciete uitzonderingen. Het "elke htmx-POST heeft het token nodig"-risico is omgezet in server-side afdwinging; een vergeten header breekt de actie in plaats van een bypass te geven.
- RBAC op UI-mutaties: consistente `is_user_authorized_for_project` / `require_project_edit_access` / `require_platform_admin`, met developer stilzwijgend read-only (`PROJECT_EDIT_ROLES`, `opi/services/project_authorization.py:27`).
- Cryptografie in de applicatie: AGE-decryptie via tempfile en `create_subprocess_exec` zonder shell (`opi/utils/age.py`), SOPS met skip-if-unchanged, AGE-versleutelde API-sleutels en wake-tokens met timing-safe vergelijking. Het wake-token is bewust smaller dan de projectsleutel (least privilege).
- De verplichte tenant-baseline-NetworkPolicy per deployment (`manifests/tenant-baseline-network-policy.yaml.jinja`), onvoorwaardelijk gegenereerd, met label-gebaseerde isolatie die op productie (Calico) aantoonbaar handhaaft.
- De cross-domain-access-service draagt naast de namespace-selector een verplicht `project`-podlabel, expliciet om toevallige namespacenaam-gelijkheid af te vangen. Goed doordacht, al dekt het bevinding B niet af (daar is de namespace echt gedeeld, geen naamgelijkenis).
- Security headers: HSTS (alleen bij https), nosniff, `X-Frame-Options: DENY`, `frame-ancestors 'none'`, `base-uri 'self'`.
- Wachtwoorden lekken niet naar taakresultaten of foutmeldingen: clone- en restore-resultaten dragen host/port/database, geen wachtwoord.

## Wat ik niet heb kunnen vaststellen

- De Kubernetes-RBAC per projectnamespace (ServiceAccount, Role, RoleBinding, `automountServiceAccountToken`) wordt niet in `operations-manager/python` gegenereerd; dat leunt op Capsule of het ODCN-projectmechanisme in de infra-overlays, die buiten deze codeanalyse vielen. Of tenant-podtokens effectief geen rechten op de Kubernetes-API hebben, kon ik niet vaststellen.
- Het daadwerkelijke `tlsSecurityProfile` (minimale TLS-versie, ciphers) van de OpenShift IngressController komt in deze repo niet voor; niet te verifiëren.
- De retentie van de externe Loki-installatie en de node-log-rotatie (~3u) staan buiten deze repo; niet te verifiëren.
- De live `eventsEnabled`-status van specifieke productierealms in Keycloak zelf (alleen het codepad is geanalyseerd, dat verklaart waarom het waarschijnlijk uit staat, geen live-verificatie).
- Of `restrict-access` end-to-end tegen een levende Keycloak is gedraaid (alleen de config-extractie is per test afgedekt, plus een guard-test tegen regressie van de oude foute leespatronen).
- Of er een expliciet API-sleutel-rotatiemechanisme bestaat; ik vond alleen sleutelgeneratie bij projectaanmaak.
- Of admission control of image-signing buiten deze Python-codebase de registry-poisoning uit bevinding A op een andere laag alsnog beperkt.
