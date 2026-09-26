# Beveiligingsscan ZAD / RIG-Cluster: pentest op code tegen BIO2 en NORA

Datum: 2026-09-26. Getoetste versie: branch `main`, commit e6c08b22c. Aard: statische whitebox-toets (code, configuratie, architectuur), geen live test tegen sandbox of productie. Toetskader: BIO2 v1.3 (definitief 9 januari 2026) en de NORA BIO-thema-uitwerkingen Applicatieontwikkeling (APO), Toegangsbeveiliging (TBV) en Clouddiensten (CLD). De BIO2-teksten zijn uit de brontekst geciteerd; de NORA-koppeling is op algemene kennis van de thema's gelegd en zo gemarkeerd. Het logboek van de scan staat in `logboek.md`; de acht deelrapporten met alle details in `deel-A.md` t/m `deel-H.md`.

De indeling volgt het pentestvoorstel voor Wies: introductie, te verwerken gegevens, architectuur in scope, reikwijdte, scenario's, randvoorwaarden. Daarna komen de uitkomsten: sterke plekken, bevindingen per scenario, en een aanpak om eraan te werken.

## 1. Introductie: wat is ZAD?

ZAD (Zelfservice Applicatie Deployment) is het deploymentplatform van het Digi Gilde (BZK/ODI) voor RIG-projecten in ODC-Noord. Een projectteam beschrijft in één YAML-projectbestand wat het nodig heeft (componenten, images, databases, opslag, SSO, domeinen, backups), en de Operations Manager (OPI) provisioneert dat: hij maakt namespaces, databases, Keycloak-realms en buckets aan, genereert Kubernetes-manifests en secrets, en schrijft alles naar drie git-repositories waaruit ArgoCD het cluster bijwerkt. Teams bedienen het via een webportal met SSO of via een REST-API met een projectsleutel.

ZAD hoort onder de BIO2. Het platform is via internet bereikbaar, host applicaties met persoonsgegevens (Wies is er een van) en beheert de secrets en de toegang van al die applicaties. Wat ZAD verkeerd doet, doen alle afnemers verkeerd; dat maakt de vertrouwensgrens van het platform zelf het belangrijkste onderwerp van deze scan.

Doel van de scan: vaststellen waar een aanvaller ongeautoriseerd toegang kan krijgen tot gegevens of functionaliteit van ZAD of van de afnemers, met drie aanvallers voor ogen: een ingelogde projectgebruiker met beperkte rechten, een aanvaller met een gecompromitteerde tenant-workload of ontwikkelaarslaptop, en een aanvaller die de authenticatie, de GitOps-keten of de toeleveringsketen probeert te omzeilen. Daarnaast: benoemen wat sterk is, en aangeven hoe projecten met een hoog beveiligingsniveau de gevonden gaten dichten.

## 2. Te verwerken gegevens (gevoeligheid)

- **Projectbestanden** (repo `zad-projects`): per project de gebruikerslijst met e-mailadressen en rollen, de configuratie van alle diensten, en AGE-versleutelde secrets: de projectsleutel (API-key), databasewachtwoorden, Keycloak-clientsecrets, registry-tokens, repo-wachtwoorden, attachments. Wie een projectbestand kan lezen én de sleutel heeft, heeft het project.
- **Gegenereerde manifests en secrets** (repo `zad-deployments`): SOPS-versleutelde Kubernetes-Secrets per namespace, ontsleuteld door de ArgoCD-sidecar met de platformsleutel.
- **Platformsecrets**: de AGE-platformsleutel, git-PAT's op de drie repos, Keycloak-admin, ArgoCD-token, TransIP-API, MinIO-root, mail. Deze staan in de OPI-pod en (deels) op ontwikkelaarslaptops.
- **Identiteits- en toegangsgegevens**: Keycloak-realms per project met gebruikers, rollen, sessies en (waar aan) events; de OPI-gebruikerstabel (e-mail, naam); invites.
- **Gegevens van afnemers**: databases, buckets en PVC's van de gehoste applicaties, en de backups daarvan in de backup-destination. ZAD verwerkt die niet inhoudelijk, maar wie het platform breekt komt erbij.
- **Operationele gegevens**: logs van OPI en van tenant-pods (via de logviewer), metrics in Prometheus/Mimir, taak- en run-historie in de OPI-database. Daarin staan e-mailadressen, projectnamen, hostnamen en foutmeldingen.

Classificatie in BIO-termen: het platform verwerkt authenticatie-informatie (5.17) en speciale toegangsrechten (8.02) voor tientallen applicaties; BBN2 is het passende basisbeveiligingsniveau voor het platform als geheel, ongeacht het niveau van de individuele afnemers.

## 3. Architectuur en techniek (in scope)

### 3.1 Operations Manager (OPI)
- FastAPI op Python 3.14, 468 modules, ~164k regels; Jinja2 met lord-of-the-components voor de UI; PostgreSQL voor de ProjectStore en taken; uitrol als één pod per cluster (`rig-system` in de sandbox, `rig-prd-operations` op ODCN).
- Drie manieren om een aanroeper te herkennen: sessie via Keycloak-OIDC (webroutes), `X-API-Key` per project (API), Bearer SSO-token (alleen project aanmaken). 187 routes, 43 unieke API-paden.
- Autorisatie: platform-admin (`is_platform_admin`), projectlid/-eigenaar, en goedkeuringen op velden via `ApprovalSpec` (een niet-admin kan sommige velden alleen aanvragen).
- Externe aanroepen via connectors: kubectl, git, sops, age, kopia, mc, skopeo, chisel, psql, en HTTP naar Keycloak, ArgoCD, Prometheus/Grafana, TransIP, mail, andere OPI's (federatie).
- Manifestgeneratie: 39 Jinja2-templates (namespace, deployment, ingress, drie NetworkPolicy-varianten, ServiceAccount, pod-security-context, ArgoCD Application en AppProject, backup- en restore-pods, db-console, oauth2-proxy-sidecar).

### 3.2 GitOps-keten
Drie repos in een Forgejo (in-cluster op de sandbox, apart op ODCN): `zad-projects` (bron), `zad-argo-user-applications` (ArgoCD Applications), `zad-deployments` (manifests plus SOPS-secrets). ArgoCD (eigen build `argocd-rig` op upstream v3.5.1) rolt uit met een CMP-sidecar die SOPS ontsleutelt met de platformsleutel. OPI reageert ook op commits die rechtstreeks in `zad-projects` landen (git-monitor).

### 3.3 Platformdiensten
Keycloak 25 (SSO, koppeling met SSO-Rijk als identity provider, realm per project), PostgreSQL via CNPG (per namespace of gedeeld), MinIO (eigen build), Redis, cert-manager, ingress-nginx (sandbox) of de OpenShift-router (ODCN), external-dns en TransIP, chisel (reverse tunnels), pgadmin, vault, mail (Stalwart), een registry-proxy (Quay, `rcr.rijksapps.nl`), Prometheus en Mimir/Grafana, kopia voor backups.

### 3.4 Multi-tenancy op het cluster
Per project een namespace `rig-prd-<project>` (Capsule-tenant op ODCN), een baseline-NetworkPolicy, een ServiceAccount, en ArgoCD-AppProject-scoping. OPI draait op ODCN met de Capsule-admin-RoleBinding per tenant-namespace; op de sandbox met een ClusterRole die cluster-breed secrets mag beheren.

### 3.5 Bouw en toelevering
Publieke GitHub-repo als CI-bron (pre-commit, tests, pip-audit, Trivy, secret-scan, SBOM, licentiecheck); images naar `ghcr.io` met CalVer-tags; op ODCN gepind in de overlay en via de registry-proxy binnengehaald.

## 4. Reikwijdte

In scope: alle code en configuratie in de repository op `main`, met de nadruk op de OPI-code, de gegenereerde manifests, de GitOps-keten, de platformconfiguratie (Kustomize-overlays, met `odcn-production` als leidend), de CI/CD en het ontwikkelproces.

Out of scope: de ODCN-infrastructuur onder het cluster (OpenShift, netwerk, MetalLB, router), de centrale SSO-Rijk-voorziening, de afnemersapplicaties zelf, en live tests tegen draaiende omgevingen. Runtime-instellingen die alleen in het cluster staan (bijvoorbeeld handmatig gezette Keycloak-instellingen) zijn alleen beoordeeld voor zover git ze beschrijft.

Beperkingen: een statische toets bewijst een zwakte in de code, niet de uitbuitbaarheid op productie; de zekerheid staat daarom per bevinding als "bevestigd" (volledig codepad gelezen, geen mitigatie gevonden) of "aannemelijk" (mitigatie mogelijk buiten het gelezen pad). Een aantal bevindingen vraagt om een korte live verificatie; die staan in deel 8 als eerste stap.

## 5. Testscenario's

De scan is uitgevoerd in acht delen die samen de scenario's uit het Wies-voorstel dekken en er de platformspecifieke aan toevoegen:

| Deel | Scenario | BIO2-controls |
|---|---|---|
| A | Authenticatie (OIDC, sessie, bearer, API-key, invites) en web-laag (CSRF, CSP, XSS, websockets, foutpagina's) | 5.16, 5.17, 8.05, 8.26, 8.28 |
| B | Autorisatie: elke route, horizontale en verticale escalatie, goedkeuringen | 5.15, 5.18, 8.02, 8.03 |
| C | Tenant- en omgevingsisolatie, platform-RBAC en blast radius | 8.22, 8.20, 8.27, 8.02, 8.18 |
| D | Secretbeheer: AGE/SOPS van generatie tot cluster, rotatie, lekken | 8.24, 5.17, 8.12 |
| E | Injectie en invoervalidatie: subprocess, YAML/template, SQL, paden, SSRF | 8.28, 8.26 |
| F | GitOps-keten en infrastructuurconfiguratie (ArgoCD, Keycloak, Forgejo, TLS, ingress) | 8.32, 8.09, 8.04, 8.20, 8.21, 8.24 |
| G | Logging, audit, privacy, beschikbaarheid en herstel | 8.15, 8.16, 5.28, 5.34, 8.13, 8.06 |
| H | Supply chain, build en ontwikkelproces | 8.08, 8.19, 5.21, 8.25, 8.29, 8.31, 8.32 |

## 6. Randvoorwaarden en werkwijze

- Alleen lezen: geen wijziging aan code, cluster of repositories; geen kubectl; sleutelbestanden niet geopend.
- Elke bevinding heeft bewijs op `bestand:regel`, een ernst (Kritiek, Hoog, Midden, Laag, Info), een zekerheid, de BIO2-control(s), een aanvalsscenario en een aanbeveling met referentiepraktijk.
- Ernst volgt de gangbare pentest-schaal: Kritiek = directe overname van platform of alle afnemers zonder voorkennis; Hoog = toegang tot andermans gegevens of platformrechten voor een geauthenticeerde gebruiker, of een structureel ontbrekende beheersmaatregel met groot bereik; Midden = beperkte escalatie, of een ontbrekende verdedigingslaag die een andere fout tot Hoog maakt; Laag = hygiëne met beperkte impact; Info = observatie zonder direct risico.
- Verificatiepas: bevindingen uit de deelrapporten zijn door de hoofdonderzoeker opnieuw langs het codepad gelezen voordat ze in dit document kwamen (zie `logboek.md`, stap 4).

## 7. Sterke plekken

Dit is geen beleefdheidslijst: elk punt is met bewijs in de deelrapporten onderbouwd en is een fundament waarop de aanbevelingen in deel 9 en 10 voortbouwen. Het patroon dat opvalt: waar het team een keuze bewust heeft gemaakt en heeft vastgelegd (in een feature-document, een test of een commentaarregel met "waarom"), is de uitvoering goed. De gaten zitten vrijwel steeds waar iets nooit als beveiligingsbeslissing is behandeld.

**Authenticatie en web-laag (deel A)**
- De fix uit de post-mortem van juli 2026 staat op alle drie de paden: `email_verified` wordt afgedwongen bij de sessie-login, bij het bearer-token en in de authorization-wall-sidecar, met een regressietest.
- De bearer-tokenverificatie is strak: vaste lijst asymmetrische algoritmen, issuer en audience verplicht, JWKS-cache met gethrottlede refetch, en een scheiding tussen "wie ben je" en "wat mag je".
- CSRF is centraal en niet opt-in: double-submit token plus exacte Origin-host-controle op elke onveilige methode buiten de API. Geen enkele POST/PUT/DELETE-route ontsnapt eraan.
- Sessiesecret zonder default en fail-closed onder 32 tekens; project-API-keys timing-safe vergeleken, aan het project uit de route gebonden en AGE-versleuteld opgeslagen; admin- en master-endpoints staan in productie uit omdat de sleutels niet geconfigureerd zijn.
- Foutafhandeling lekt niets, security headers zijn compleet, autoescape staat aan en elke `|safe` is gemotiveerd, de WebSocket-handshake doet Origin, sessie, allowlist en projectautorisatie vóór `accept()`.

**Isolatie (deel C)**
- Het projectschema is gesloten: geen `hostPath`, `privileged`, `hostNetwork`, eigen ServiceAccount of `secretKeyRef` naar andermans secrets; container-securityContext is hard en consequent.
- Elke tenant krijgt een eigen ServiceAccount zonder Role; de AppProject is gepind op één namespace zonder cluster-resources; de CMP leest de sleutel van de doelnamespace en dwingt `.namespace` af.
- MinIO-policy per bucket, Kopia-sleutel per namespace, Keycloak-realm-admin gescopeerd, PostgreSQL-superuser alleen op een dedicated cluster, subdomein-uniekheid met een databaseconstraint, federatie uit in productie.
- De tijdelijke databaseconsole is goed opgezet: oauth2-proxy met ledenallowlist, sessie-lock, TTL en reaper, en een blijvende administratie in de `runs`-tabel.

**Secretbeheer (deel D)**
- Randomness klopt overal (`secrets`-module), wachtwoorden 20 tekens met klassen, API-keys 32 tekens; private keys gaan nooit via argv naar een subproces maar via tempfile 0600 of omgeving; plaintext via stdin.
- Het SOPS-pad faalt gesloten in drie lagen: de enige schrijfingang weigert bij een overgebleven `.to-sops.yaml`, de git-connector weigert een commit ermee, en `.gitignore` is het laatste vangnet; een test bewaakt dat managers de kale variant niet aanroepen.
- De API maskeert beide opslagvormen en `plain:`; de TOTP-seed bereikt de pagina nooit; het tenant-namespace-Secret draagt de projectsleutel en niet de platformsleutel.
- De rotatietooling is goed gebouwd: dry-run, fingerprint van de plaintext voor en na, selectie op recipient, een verplichte eindtoets dat de oude sleutel niets meer opent, en acht testbestanden.
- Eerdere lekken zijn eerlijk in commitberichten benoemd en hebben regressietests gekregen; sinds september maakt elke test zijn eigen sleutelpaar.

**GitOps en infrastructuur (deel F)**
- SOPS/AGE is bijna overal de norm; push-serialisatie met compare-and-swap; `selfHeal` en `prune` aan met `allowEmpty: false`; `Prune=false` op CNPG zodat een fout in git geen database wist.
- Deny-by-default NetworkPolicies per platformcomponent, consistente pod-hardening, ECDSA-384-certificaten met CAA en HSTS, SAML naar SSO-Rijk gepind.
- De eigen ArgoCD-build is verifieerbaar: upstream v3.5.1 plus genummerde patches, met `task src:verify` als bewijs.

**Logging en herstel (deel G)**
- Log-uitgifte aan projectleden is strikt per project en per pod gescoped, met sessie-authenticatie op de websocket, Origin-controle, verbindings- en snelheidslimieten, en opgeschoonde logregels.
- Restore is tenant-gebonden; Kopia-repository en -wachtwoord per project; de identity-bug uit de retentie is gefixt.
- Goedkeuringen zijn herleidbaar (datum, status, wie, bericht in het projectbestand); Keycloak-audit-events staan aan in de realm-blueprints met 90 dagen bewaartermijn.
- Graceful drain en herstel van halfslachtige taken; compare-and-swap met driewegs-merge op het projectbestand.
- De eigen documentatie is eerlijk over de gaten (backup-beleid, gebeurtenissen-inventarisatie), wat een discussie over de feiten scheelt.

**Supply chain en proces (deel H)**
- Secret-scan op drie niveaus (pre-commit, CI, een historie-modus), met semantische validatie in plaats van een allowlist, plus GitHub secret scanning met push protection.
- pip-audit strikt en zonder uitzonderingen, wekelijks; licentiecontrole op PARANOID-niveau; de Keycloak-provider-jar wordt in CI reproduceerbaar herbouwd en byte voor byte vergeleken.
- Images draaien non-root met tini als PID 1; multi-stage builds; `.dockerignore` sluit `security/` en `.env`-bestanden uit met uitleg waarom.
- Een reeks beveiligingsguards als tests: plaintext-secret-guard op alle schrijfpaden, template-injectie-sweep, command-injectie op pg_dump, CSRF-afdwinging, security headers, fail-closed secret key, tenant-isolatietests, e-mail-verified.
- Zes verplichte status checks op main; een releaseproces met gedocumenteerde generale doorlopen; de CalVer-pin in de productie-overlay maakt de deploybeslissing een git-commit.

**Autorisatie (deel B)**
- De middleware faalt gesloten: een niet-gematcht pad vereist SSO, en de skip-lijsten zijn exact of prefix-met-slash.
- De projectbinding van de API-key is structureel: de decorator weigert zonder projectnaam en vervangt de naam uit de URL door de canonieke naam uit de store; alle 131 API-key-routes dragen hem.
- Namespace-pin: een namespace die niet gelijk is aan de projectnaam wordt geweigerd; projectnaam en cluster worden server-side bepaald.
- Elke muterende webroute hercontroleert de rol op het moment van schrijven; onveranderlijke velden geven 400; platform-managed velden zijn op het model gedeclareerd en op de API-schrijfweg afgedwongen.
- Admin-pagina's sluiten zelf de deur, niet alleen het menu, en dat is negatief getest.

**Injectie en invoervalidatie (deel E)**
- Eén validatie-chokepoint in de ProjectStore, validatie na migratie, en het git-pad wordt expliciet als vijandige bron behandeld.
- Geen `shell=True` behalve op één plek; geen `eval`, `pickle` of onveilige YAML-loaders; SQL overal via identifier-validatie, quoting en parameters; `pg_dump` zonder shell.
- Een correcte `yaml_scalar`-quotingfilter bestaat en de deployment-template en de authorization-wall-template gebruiken `tojson` consequent, met regressietests.
- Skopeo pusht altijd naar de platformregistry, weigert niet-globale adressen en maskeert userinfo; upload-staging met strikte tokens, 0700-mappen en TTL-sweep; image-upload streamt met een limiet tijdens het lezen.

## 8. Bevindingen

### 8.1 Hoe dit deel te lezen

De acht deelrapporten bevatten samen 148 genummerde bevindingen (4 kritiek, 24 hoog, 63 midden, 41 laag, 16 info). Veel daarvan zijn hetzelfde probleem vanuit een andere hoek gezien: de gedeelde PAT staat in D-4 en F-3, de CMP-sidecar in F-1, C-2 en D-7, de heredoc-shell in D-6, E-4 en C-14. Hieronder zijn ze samengevoegd tot 20 hoofdbevindingen (4 kritiek, 16 hoog) en 24 middenbevindingen, met verwijzing naar de deel-ID's waar het bewijs staat. De lage en informatieve bevindingen staan alleen in de deelrapporten en in de bijlage.

Per bevinding: ernst, zekerheid, BIO2-control(s), het NORA-thema-object waar het onder valt (op algemene kennis, gemarkeerd met een asterisk), wat er aan de hand is, het aanvalsscenario, en een aanbeveling met de praktijk van projecten met een hoog beveiligingsniveau als referentie.

Twee dingen vooraf. Ten eerste: de vier kritieke bevindingen zijn geen abstracte zwaktes maar vandaag bruikbare paden, en twee ervan (K-1 en K-2) zijn feitelijk lopende incidenten die om handelen binnen dagen vragen, niet om een planning. Ten tweede: de rode draad door bijna alle hoge bevindingen is één architectuurkeuze: het platform vertrouwt zijn eigen GitOps-repositories, zijn eigen sidecar en zijn eigen proces onvoorwaardelijk. Wie één van die drie raakt, heeft alles. De aanbevelingen in deel 10 gaan daarom vooral over het inbouwen van een tweede grens.

### 8.2 Kritiek

#### K-1. De productie-platformsleutel stond ruim een jaar in de publieke repo, en de secrets eronder zijn niet vervangen (D-1)
- **Ernst**: Kritiek. **Zekerheid**: bevestigd door de hoofdonderzoeker (afgeleide publieke sleutel vergeleken met de SOPS-recipients; sleutelinhoud niet getoond). **BIO2**: 8.24, 5.17, 8.04, 8.12, 5.26. **NORA***: APO uitvoering (geheimenbeheer), CLD cryptografie.
- **Wat**: `tests/test_age_password_decryption.py` bevatte van juli 2025 tot 22 september 2026 een geldige AGE-privésleutel. Die sleutel was tot de rotatie van 24 september de recipient van 19 SOPS-bestanden op `main`, waarvan 16 in de odcn-productie-overlays: OPI-env-secrets (met `KEYCLOAK_ADMIN_PASSWORD`, `DATABASE_ADMIN_PASSWORD`, `MINIO_ADMIN_SECRET_KEY`, `OIDC_CLIENT_SECRET`, `GRAFANA_TOKEN`, `SECRET_KEY`), ArgoCD-admin, Keycloak-admin, Postgres-admin, MinIO-admin, Redis, pgadmin, TransIP, vault-init, mail, backup-destination, Prometheus-auth. De GitHub-repo is publiek sinds 8 oktober 2025 en het testbestand zit in de eerste commit. De rotatie van 24 september heeft de bestanden op een nieuwe sleutel gezet, maar geen enkel SOPS-bestand is daarna inhoudelijk gewijzigd: de wachtwoorden die een jaar lang voor iedereen te ontsleutelen waren, zijn dus nog steeds de wachtwoorden van productie. De oude sleutel staat bovendien nog als `old_key.txt` en `original_zad_key.txt` op de laptop.
- **Scenario**: clone op een commit van voor 22 september, sleutel uit het testbestand, ontsleutel de 16 productiebestanden op een commit van voor 24 september. Resultaat vandaag: Keycloak-admin (alle realms, alle gebruikers van alle applicaties), Postgres-superuser op de gedeelde instantie, MinIO-root, ArgoCD-admin, TransIP (DNS van `rijksapp.nl`), en de `SECRET_KEY` waarmee elke OPI-sessie te vervalsen is. Forks, caches en secret-scanners van derden hebben dit met zekerheid gezien.
- **Aanbeveling**: behandel dit als een lopend incident volgens het eigen incidentproces (BIO 5.24 t/m 5.26) en meld het aan de CISO en ODC-Noord. (1) Vervang binnen dagen alle waarden die onder de oude recipient stonden, in deze volgorde: Keycloak-admin en master-clientsecret, Postgres-admin, MinIO-admin, ArgoCD-admin, TransIP, `SECRET_KEY`, vault-init, Grafana-token, Redis, pgadmin, mail, Prometheus-auth. (2) Roteer daarna de projectsleutels (issue #183) en wat daaronder hangt, want die stonden onder dezelfde recipient. (3) Onderzoek audit-logs van Keycloak, ArgoCD, TransIP en de Postgres-superuser vanaf oktober 2025 op onbekende toegang. (4) Vernietig de oude sleutelbestanden. (5) Post-mortem in `docs/post-mortems/`. Referentiepraktijk: bij Logius, DICTU en de SSC-ICT-platforms geldt "sleutel gezien is sleutel weg, en alles wat eronder lag ook"; een rotatie die alleen hersleutelt telt niet als herstel.

#### K-2. Het Keycloak-databasewachtwoord staat in klare tekst in de productie-overlay en de database is vanuit elke tenant bereikbaar (F-5, F-10, C-8)
- **Ernst**: Kritiek. **Zekerheid**: bevestigd voor git en de include-keten; de live waarde is niet gelezen, maar ArgoCD draait met `selfHeal` op deze map, dus git is leidend. **BIO2**: 5.17, 8.05, 8.24, 8.09, 8.22. **NORA***: TBV uitvoering (authenticatiemiddelen), APO (geheimen in configuratie).
- **Wat**: `infrastructure/bootstrap/infrastructure/secrets/config/overlays/odcn/keycloak-db-credentials.yaml` bevat `username: keycloak`, `password: keycloak`. Het bestand is een gewone resource in de overlay die `production-infrastructure` uitrolt; CNPG koppelt de rol `keycloak` aan precies dit Secret; Keycloak logt ermee in. De NetworkPolicy op `rig-db` laat poort 5432 toe vanuit elke namespace met het label `created-by: operations-manager`, dus vanuit elke tenant-pod. Het bestand is ongewijzigd sinds de eerste publieke commit. Een `@secret-gen`-annotatie laat zien dat de generator hem had moeten vervangen; dat is nooit gebeurd en niets controleert dat.
- **Scenario**: een willekeurige tenant-pod (of een projectlid via een job, zie H-11) verbindt met `rig-db-rw.rig-prd-operations:5432` als `keycloak/keycloak` en heeft de Keycloak-database: alle realms, client-secrets, wachtwoord-hashes, OTP-secrets en sessies van alle aangesloten applicaties. Daarmee is elke applicatie op het platform, Wies inbegrepen, over te nemen.
- **Aanbeveling**: vandaag roteren (CNPG past een gewijzigd Secret op de rol toe) en het bestand vervangen door een SOPS-versie via de bestaande generator. Structureel: een CI-guard die een `stringData:` zonder `sops:`-metadata onder `overlays/odcn` weigert (de secret-scanner vangt dit niet, want het is geen sleutelvormige string), `sslmode=require` en `hostssl` in `pg_hba`, en de Keycloak-database op een eigen CNPG-cluster dat alleen Keycloak-pods mag bereiken. Referentiepraktijk: de identity provider is bij elk platform met een hoog niveau een aparte beveiligingszone met eigen database, eigen netwerksegment en eigen sleutels (BIO 8.22.01: elke gescheiden groep heeft een gedefinieerd beveiligingsniveau).

#### K-3. De ArgoCD-sidecar voert door tenants bepaalde inhoud uit met een ServiceAccount die alle SOPS-sleutels kan lezen (F-1, C-2, D-7, D-2)
- **Ernst**: Kritiek. **Zekerheid**: bevestigd voor de codepaden; de omvang van de SA-rechten is aannemelijk (operator-gegenereerd, niet in git). **BIO2**: 8.04, 8.24, 8.22, 8.32, 8.02. **NORA***: CLD (scheiding tussen tenants), APO (veilige bouwketen).
- **Wat**: de CMP-plugin in de ArgoCD-repo-server doet per render drie dingen die samen niet mogen: hij sourcet `.cmp-env` als bash-script, hij draait `kustomize build --enable-alpha-plugins --enable-exec --enable-helm` en `helmfile template` op inhoud die uit git komt (inclusief chart-repo's van derden die OPI voor een tenant kloont), en hij leest met `kubectl get secret sops-age-key -n <doelnamespace>` de sleutel van de app die hij rendert. Eén sidecar onder één ServiceAccount (`argocd-argocd-server`) rendert alle tenant-apps én de platform-apps in `rig-prd-operations`, dus die SA kan de platformsleutel lezen. OPI schrijft `.cmp-env` ongequoted uit `env-vars` van een helmfile-deployment, en schrijft daarin bovendien ontsleutelde waarden in klare tekst naar `zad-deployments` (D-2). Render-stderr wordt aan de gebruiker getoond.
- **Scenario**: een projectbestand met een helmfile-deployment en `env-vars: {X: "x; kubectl get secret sops-age-key -n rig-prd-operations -o yaml >&2"}`, of een eigen chart-repo met een gotmpl `exec` of een `kustomization.yaml` met een exec-plugin. Bij de volgende render lekt de platformsleutel via de foutmelding in de portal. Vandaag is helmfile alleen via een directe commit in `zad-projects` te configureren, niet via wizard of API; met H-1 (de gedeelde PAT) heeft elke project-admin dat schrijfrecht.
- **Aanbeveling**: (a) `.cmp-env` niet sourcen maar per regel parsen met een strikte sleutel-regex, of de waarden via ArgoCD `plugin.env` meegeven; versleuteld committen. (b) `--enable-exec` en `--enable-alpha-plugins` uit voor tenant-apps. (c) Een aparte sidecar en SA voor platform-apps, en voor tenant-apps een SA met een Role per namespace die alleen `get` op het ene Secret mag. (d) Helmfile-bronrepo's alleen uit een allowlist. (e) Render-stderr filteren. Referentiepraktijk: bij multi-tenant GitOps (bijvoorbeeld de referentie-inrichtingen van de CNCF TAG Security en de Red Hat GitOps-hardening) rendert een tenant nooit in hetzelfde proces als het platform, en heeft een render-proces nooit meer dan de sleutel van de tenant die hij rendert.

#### K-4. Het app-of-apps draait in AppProject `default` en de ArgoCD-repo is via YAML-injectie te vervuilen (F-2, E-1, C-13, F-4)
- **Ernst**: Kritiek. **Zekerheid**: elke schakel bevestigd; de keten als geheel niet uitgevoerd. **BIO2**: 8.32, 8.04, 8.02, 8.28. **NORA***: APO (wijzigingsbeheer en output-encoding), CLD.
- **Wat**: de Application `user-applications` (en `production-infrastructure` en `ron-infrastructure`) staat op `project: default`, en nergens in de repo is dat default-project ingeperkt: alle bronrepo's, alle bestemmingen, alle cluster-resources. Alles wat in `zad-argo-user-applications` landt, rolt dus uit met de rechten van de ArgoCD-controller. Tegelijk rendert OPI `repositories[].username`, `.password`, `.branch` en `targetRevision` kaal in `argo-repository-https.yaml.jinja` en `argocd-application.yaml.jinja`, en legt het schema op die velden geen pattern. Een `branch` met een newline en `---` levert een extra document in die repo op. Los daarvan geeft schrijfrecht op `zad-deployments` controle over andermans namespace zonder herkomstcontrole (F-4): vaste bot-committer, geen signing, `--no-verify`, `prune` en `selfHeal`.
- **Scenario**: wie `zad-projects` mag schrijven (zie H-1 voor wie dat zijn) zet in een projectbestand `branch: "main\n---\napiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRoleBinding\n..."`; OPI valideert (schema laat het door), rendert, commit naar de argo-repo; ArgoCD synct het als onderdeel van `default` en de aanvaller is cluster-admin binnen het ArgoCD-bereik.
- **Aanbeveling**: het `default`-project dichtzetten (lege `sourceRepos` en `destinations`) en een eigen AppProject voor het app-of-apps met `clusterResourceWhitelist: []` en alleen de argo-namespace als bestemming; beide templates op `yaml_scalar`; patterns op `repository.name`, `username`, `branch`; OPI-commits signeren en `signatureKeys` op de AppProjects; geen menselijke schrijvers op de drie repo's. Referentiepraktijk: ArgoCD's eigen hardening-gids en de NCSC-richtlijn voor CI/CD noemen een ingeperkt default-project en commit-signing als basis; bij platforms met een hoog niveau is "wat in git staat is waar" alleen houdbaar als git zelf niet door één account te wijzigen is.

### 8.3 Hoog

#### H-1. Eén gedeelde schrijf-PAT in elk projectbestand en elk ArgoCD-repository-secret (D-4, F-3, D-9, F-15)
- **Ernst**: Hoog. **Zekerheid**: bevestigd. **BIO2**: 5.17, 8.24, 8.04, 8.02. **NORA***: TBV (authenticatiemiddelen voor systemen).
- **Wat**: bij projectaanmaak kopieert OPI `settings.PROJECT_REPO_PASSWORD` (een ciphertext-default in `config.py:238`) in `repositories[].password` van elk projectbestand, en schrijft het ontsleuteld in een per-project ArgoCD-repository-Secret. Een project-admin ziet zijn projectsleutel in de UI en kan het wachtwoord dus ontsleutelen: dat is de PAT waarmee OPI naar alle drie de repo's pusht. Bovendien kloont OPI met die PAT naar elke `https://`-host die in `repositories[].url` staat (F-15).
- **Scenario**: project-admin ontsleutelt de PAT en heeft push op `zad-deployments` (andermans namespace, F-4), op `zad-projects` (K-3, K-4) en op de argo-repo (K-4). Dit is de schakel die de kritieke bevindingen voor elke project-admin bereikbaar maakt.
- **Aanbeveling**: ArgoCD een eigen read-only credential via een repo-credential-template op URL-prefix; `repositories[].password` uit het projectbestand; per repo een deploy key of fine-grained token met alleen `contents: write` op die ene repo; de platform-PAT nooit naar een niet-platform-host sturen. Referentiepraktijk: één identiteit per schrijver en per repo, met de scope in de token zelf, is de standaard bij elk GitOps-platform met een hoog niveau; een gedeeld schrijftoken in tenant-data is nergens acceptabel.

#### H-2. Ontsleutelde helmfile-env-vars worden in klare tekst in `zad-deployments` gecommit (D-2)
- **Ernst**: Hoog. **Zekerheid**: bevestigd in code; niet nagemeten in een clone. **BIO2**: 8.24, 8.12, 8.04.
- **Wat**: `project_manager.py:4947-4954` schrijft `KEY=plaintext` naar `.cmp-env`, `git add -A` neemt het mee, en de plaintext-guard kijkt alleen naar `*.to-sops.yaml`. De featuredoc belooft `<encrypted>`; de test verwacht de ontsleutelde waarde.
- **Aanbeveling**: `.cmp-env` versleuteld committen en in de CMP ontsleutelen; de guard uitbreiden; de historie van `zad-deployments` beoordelen en de betreffende waarden vervangen.

#### H-3. Eén platformsleutel ontsluit alles, staat in de procesomgeving en op laptops, zonder KMS, break-glass of gebruikslogging (D-3, C-9, H-7, D-8, D-17)
- **Ernst**: Hoog. **Zekerheid**: bevestigd. **BIO2**: 8.24.01/02, 8.02.01, 8.18.02, 5.17. **NORA***: CLD cryptografie en sleutelbeheer.
- **Wat**: de platformsleutel versleutelt elke projectsleutel en daarmee transitief elk geheim van elk project, plus de git-credentials; hij staat als omgevingsvariabele in de OPI-pod (erfelijk naar elk kindproces), in twee namespaces, in een handmatige kopie, en op laptops in `security/` naast oude sleutels en PAT's met mode 0644. OPI is één proces met alle platformsleutels én Capsule-admin in elke tenant-namespace: een fout in de weblaag is root over het platform. Er is geen register van wie de sleutel heeft, geen kwartaalbeoordeling, geen logging van gebruik buiten het cluster.
- **Aanbeveling**: projectsleutels onder een aparte KEK die alleen OPI heeft; sleutel als gemount bestand met `SOPS_AGE_KEY_FILE` in plaats van env; `emptyDir` in geheugen voor `/tmp`; per beheerder een eigen recipient (AGE ondersteunt `age-plugin-yubikey`) zodat intrekken per persoon kan; oude sleutels vernietigen; `chmod 600` afdwingen; een sleutelregister met kwartaalcontrole. Op termijn OPI splitsen in een frontend zonder sleutels en een worker met sleutels. Referentiepraktijk: BIO 8.24.01 vraagt een cryptografiebeleid met eigenaarschap en registratie; teams met een hoog niveau houden de clustersleutel alleen in KMS of HSM en werken op laptops uitsluitend met een persoonlijke, intrekbare sleutel.

#### H-4. De rotatie hersleutelt maar vervangt niet, de historie blijft leesbaar, en er is geen incident-runbook (D-5, D-22, D-19)
- **Ernst**: Hoog. **Zekerheid**: bevestigd. **BIO2**: 8.24.02, 8.10, 5.17, 5.24.
- **Wat**: `key_rotation.py` houdt de plaintext gelijk, roteert geen projectsleutels en herschrijft geen historie. Wie een oude sleutel en een oude clone heeft, leest alles wat sindsdien niet is vervangen. Naast D-1 staan nog drie andere geldige AGE-sleutels en een SSH-privésleutel in de publieke historie, en 537 van 548 lokale refs dragen er nog een. Er is geen runbook "sleutel gelekt" en geen rollbacksectie.
- **Aanbeveling**: een beslisdocument over het historie-restrisico; bij een bevestigd lek altijd óók de onderliggende secrets vervangen (prioriteitsvolgorde vastleggen); de historiescan wekelijks in CI; oude branches en PR-refs opruimen; een incident-runbook.

#### H-5. Netwerkisolatie op het cluster staat grotendeels open (F-6, F-9, C-7, C-4, C-5, F-14, C-6)
- **Ernst**: Hoog. **Zekerheid**: bevestigd voor git; Capsule- en SCC-instellingen op ODCN zijn niet in git en blijven aannemelijk. **BIO2**: 8.22.01, 8.20, 8.27. **NORA***: CLD (zonering), CVZ (segmentatie).
- **Wat**: (1) `emergency-restore-allow-all` uit de storing van 10 juni staat nog in de productie-kustomization en maakt elke deny-by-default-policy in `rig-prd-operations` decoratief; de OPI-netpol zegt dat zelf. (2) De tenant-baseline laat egress toe naar de hele ops-namespace op alle poorten: OPI:8000, ArgoCD:8080 (plain HTTP), Prometheus:9090 zonder auth, Keycloak intern. (3) De ACME-policy opent poort 80 en 8089 van alle pods in een namespace voor elke bron in het cluster. (4) Er is geen namespace-brede default-deny: helm- en helmfile-pods en elke unlabelde pod staan volledig open. (5) Tenant-egress naar internet staat open. (6) Geen Pod Security Admission-labels op gegenereerde namespaces, dus buiten wat OPI zelf rendert kan `privileged` of `hostPath` gevraagd worden (op ODCN mogelijk door SCC gevangen; niet toetsbaar). Het geheugen wist al van de allow-all; het is nu 3,5 maand later.
- **Aanbeveling**: de vier stappen uit het allow-all-bestand afronden en het verwijderen; de ops-egress per dienst en poort uitschrijven; de ACME-regel selecteren op solver-pods en de router-namespace; een default-deny per namespace op wave 0; PSA `restricted` op elke gegenereerde namespace; pod- en service-CIDR's uitsluiten van de internet-regel; op termijn een egress-allowlist per project. Referentiepraktijk: NSA/CISA Kubernetes Hardening Guide en de CIS Benchmark: default-deny per namespace, expliciete egress, PSA restricted; de Kind-sandbox handhaaft geen policies, dus dit is alleen toetsbaar met Calico in de e2e-suite.

#### H-6. ArgoCD-toegang: ontwikkelaarsgroep is admin, OPI gebruikt het lokale adminaccount over plain HTTP (F-7, F-8, A-9)
- **Ernst**: Hoog (als `rig-prd-developer-group` tenant-ontwikkelaars bevat). **Zekerheid**: configuratie bevestigd; groepsinhoud onbekend. **BIO2**: 8.02, 5.15, 8.32, 8.20.
- **Wat**: `g, rig-prd-developer-group, role:admin` in de productie-ArgoCD; een ArgoCD-admin kan Applications in `default` aanmaken (K-4), repo's toevoegen en elke tenant-app syncen of verwijderen. OPI logt in als `admin` op `http://argocd-server:80`; het 24-uurs JWT wordt gecachet; de API is in-cluster op 8080 zonder `from`-beperking bereikbaar.
- **Aanbeveling**: developers `role:readonly` of een per-project rol; een lokaal `opi`-account met project-gescoped rol; `admin.enabled: "false"`; TLS naar argocd-server; RBAC-logging aan. Referentiepraktijk: bij GitOps-platforms met een hoog niveau is de ArgoCD-UI voor tenants read-only en loopt elke wijziging via git.

#### H-7. Keycloak: adminconsole en master-realm op internet, master zonder brute-force-bescherming, OPI op het adminwachtwoord (F-20, F-21, F-28)
- **Ernst**: Hoog. **Zekerheid**: bereikbaarheid bevestigd; master-instellingen aannemelijk. **BIO2**: 8.05, 5.17.01 (MFA op beheeraccounts), 8.02, 8.08. **NORA***: TBV (beheertoegang).
- **Wat**: de Keycloak-Ingress publiceert `/` zonder IP-beperking, geen `KC_HOSTNAME_ADMIN`; niets zet `bruteForceProtected` of een wachtwoordbeleid op `master`; het productie-adminsecret mist de OTP-sleutels die het OTP-adminpad voeden; OPI draait aannemelijk nog op `KEYCLOAK_ADMIN_PASSWORD` (client-credentials alleen als `KEYCLOAK_ADMIN_CLIENT_SECRET` gezet is, en die ontbreekt in het odcn-secret). Keycloak 25.0.6 is sinds oktober 2024 zonder security-fixes. Realm-adminnamen zijn voorspelbaar.
- **Aanbeveling**: aparte admin-hostname met allowlist (of router-regel op `/admin`); `bruteForceProtected` en wachtwoordbeleid op master via de boot-setup; het prod-secret regenereren met client-credentials en OTP-adminpad, daarna `KEYCLOAK_ADMIN_PASSWORD` uit OPI's env; de Keycloak-26-upgrade uit het bestaande plan uitvoeren. Referentiepraktijk: BIO 5.17.01 eist MFA op accounts met beheerrechten; SSO-Rijk-aangesloten IdP's van Logius en DICTU hebben de adminconsole nooit op een publiek adres.

#### H-8. Invite: de gebruiker kiest zijn eigen e-mailadres, en account-linking op e-mail maakt dat een impersonatiepad (F-22, A-10, A-1, A-2, B-8)
- **Ernst**: Hoog. **Zekerheid**: aannemelijk (code gelezen, niet uitgevoerd; hangt af van de first-broker-login-flow). **BIO2**: 8.05, 5.16, 8.26.
- **Wat**: op een `sso-support`-realm registreert een genodigde met een vrij gekozen adres, alleen beperkt door een optionele domeinrestrictie; kiest hij het adres van een collega die nog nooit via SSO inlogde, dan botst die bij eerste login op "account bestaat al" en wordt (bij `account-link: automatic` stil) gekoppeld aan het account van de aanvaller. De post-mortem-fix dekt de IdP-kant, niet een lokaal gekozen adres. Daarbovenop: de registratieroute is publiek zonder rate-limit, invites verlopen niet en hebben geen `max_uses`, keys mogen zelfgekozen zijn vanaf 3 tekens en worden op INFO gelogd.
- **Aanbeveling**: invite per adres uitgeven (adres in de invite, formulier read-only) of lokale accounts nooit als link-doel toestaan; `idp-auto-link` alleen op realms zonder lokale registratie; rate-limit, `max_uses` en `expires` op invites; minimumlengte 16 en nooit loggen. Referentiepraktijk: dit is precies de klasse fout uit de post-mortem van juli; de les daaruit ("een e-mailadres is pas een identiteit als de bron het autoritatief levert") geldt ook voor het invite-pad.

#### H-9. Ontsleutelde secrets in de HTML voor de rollen member en developer (B-1, D-15)
- **Ernst**: Hoog. **Zekerheid**: bevestigd tot in de templates. **BIO2**: 5.15, 8.03.02, 5.18. **NORA***: TBV (autorisatie op gegevensniveau).
- **Wat**: de detailpagina ontsleutelt voor elk projectlid de projectsleutel, de API-key, Keycloak-wachtwoorden, TOTP, `user-env-vars`, aliases en helm-values; alleen het Configuratie-blok is in het template op `admin`/`owner` gegate; het deployment-blok rendert de env-vars voor iedereen in een `c-secret-field`. De rolcontrole zit uitsluitend in het template; één nieuw fragment lekt alles.
- **Aanbeveling**: alleen ontsleutelen als de rol het toelaat, server-side, en het ongebruikte oude template verwijderen. Referentiepraktijk: autorisatie hoort in de dataselectie, niet in de weergave (OWASP ASVS V4).

#### H-10. Elke projectrol mag code in de namespace uitvoeren met het databasesecret, via jobs en de db-console (B-3, B-7)
- **Ernst**: Hoog. **Zekerheid**: bevestigd. **BIO2**: 8.02, 5.18, 8.03.
- **Wat**: `POST /projects/{p}/jobs` en `/db-console` eisen alleen lidmaatschap (elke rol). De job neemt image en command uit het formulier, draait via `/bin/sh -c` onder het project-serviceaccount met het databasesecret als `envFrom`. Ook een restore met `restore_mode == new` laat een member het projectbestand wijzigen.
- **Aanbeveling**: beperken tot de edit-rollen of een expliciete operationele rol met vervaltijd (het rechtenontwerp in `features/futures/rechten-*.md` voorziet daarin); starter, image en command als gebeurtenis loggen.

#### H-11. Een project-admin kan een domein zelf op `approved` zetten via het formulierpad (B-4)
- **Ernst**: Hoog. **Zekerheid**: bevestigd op functieniveau, niet over HTTP. **BIO2**: 8.02, 5.18.
- **Wat**: de API beschermt platform-managed velden (`PLATFORM_MANAGED`, 422); het formulierpad niet: de services-selectie-converter bewaart dict-entries "as-is", de merge vervangt lijsten, en de store kent geen regel die de approval-status met `previous` vergelijkt. Het manifestpad vertrouwt de opgeslagen status. De geheugennotitie klopt: de enforcer slikt de FieldWarning, dus handhaving zit uitsluitend in het manifestpad. Hetzelfde kanaal bereikt `send-email`-approvals.
- **Aanbeveling**: de platform-managed-bescherming ook op het formulierpad, en op store-niveau een integriteitsregel die platform-managed sleutels met `previous` vergelijkt zodat elke schrijfweg gedekt is; een test met een owner-payload met `status: approved` die op `requested` uitkomt.

#### H-12. Een project kan zichzelf toegang tot alle keys van de gedeelde Redis geven (C-1)
- **Ernst**: Hoog. **Zekerheid**: bevestigd. **BIO2**: 8.22, 8.03, 8.02.
- **Wat**: het formulierveld `acl-key-prefix: false` geeft de ACL-gebruiker van het project elke key van de gedeelde instantie, zonder goedkeuring; de code logt een WARNING, maar een log is geen guard.
- **Aanbeveling**: de opt-out een `ApprovalSpec` maken of schrappen; bestaande projectbestanden nalopen.

#### H-13. YAML-injectie in manifest-templates via velden zonder pattern (E-2, E-3, E-8, E-9, E-10, E-11, E-1)
- **Ernst**: Hoog. **Zekerheid**: bevestigd. **BIO2**: 8.28, 8.26. **NORA***: APO uitvoering (veilig programmeren, input/output-validatie).
- **Wat**: het validatiemodel is goed (één chokepoint, validatie na migratie), maar het schema beschermt alleen waar een `pattern` staat, de `FormatChecker` staat uit, en zeven templates renderen velden zonder pattern kaal of met naïeve quotes: de CNPG `Cluster`-CR (`storage`, `image`, `resources`, `postInitSQL`, expliciet "Not pattern-constrained"), de restore- en backup-pods (API-body en URL-padsegmenten, direct door OPI toegepast), `components[].resources`, `generic-secret`-sleutels, `users[].email`, de metrics-scraper-annotatie, en de ArgoCD-templates uit K-4. De authwall-fix (`tojson`) is per template gedaan, niet centraal.
- **Scenario**: een projectlid met API-key zet `storage: "1Gi\n  hostNetwork: true\n  #"` in de dedicated-Postgres-config en injecteert siblings of een tweede document in zijn eigen namespace, buiten de goedkeuringen om.
- **Aanbeveling**: alle user-gestuurde scalars in de templates door `yaml_scalar` of `tojson`; `format_checker` aan; quantity- en charset-patterns in de pydantic-modellen; de kale `jinja2.Template` in `kubectl.py:333` op dezelfde Environment als `manifests.py`; een test die elk template met een injectiepayload rendert en parseert. Referentiepraktijk: output-encoding op de sink, niet alleen validatie op de bron (ASVS V5.3).

#### H-14. Geen persistent audit-spoor met actor (G-1, G-13)
- **Ernst**: Hoog. **Zekerheid**: bevestigd. **BIO2**: 8.15.01 (actie, object, resultaat, oorsprong, actor, tijdstempel), 8.15.04, 5.28. **NORA***: APO en TBV (logging en verantwoording); NEN 7513-achtige eisen voor de gehoste applicaties.
- **Wat**: de enige plek waar "wie" persistent staat voor projectwijzigingen is `async_tasks.created_by`, en die tabel wordt na één uur geleegd; de git-author is altijd de OPI-bot; ledenbeheer, projectverwijdering, image-pushes, platformbeheer en API-gebruik hebben na een uur geen spoor met actor; bij master-key-taken bepaalt de aanroeper zelf `created_by`. Het ontwerp voor een gebeurtenissentabel ligt er, ongebouwd.
- **Aanbeveling**: de gebeurtenissentabel bouwen met minimaal tijd, actor, actor-soort, type, onderwerp, uitkomst en bron-IP, en alle dag-één-schrijfwegen erop aansluiten; de actor in de commit van `zad-projects`; taakretentie naar 90 dagen of alleen de werkgegevens opruimen. Referentiepraktijk: BIO 8.15.01 is letterlijk de lijst; Wies moet als afnemer kunnen aantonen wie zijn deployment wijzigde, en dat kan nu niet.

#### H-15. Back-ups: platformbrede S3-admin-credentials in elke tenant-pod, met verwijderrecht, zonder object lock, op dezelfde Ceph (G-2, C-3)
- **Ernst**: Hoog. **Zekerheid**: bevestigd. **BIO2**: 8.13, 8.14, 5.28.
- **Wat**: elke backup- en restore-pod krijgt `S3_ACCESS_KEY`, `S3_SECRET_KEY` en `KOPIA_PASSWORD` als platte `value:` in de pod-spec; het script bevat `mc rm --recursive --force`; één credential voor alle projecten; de bestemming is een MinIO op een RWO-PVC op dezelfde Ceph, bereikbaar op 9000 vanuit alle tenant-namespaces; geen versioning of object lock. Het backup-beleid belooft alle drie en markeert ze als "belegd, planning volgt".
- **Aanbeveling**: per project een S3-gebruiker zonder delete op alleen de eigen bucket; verwijdering via een apart opruimproces; versioning en object lock; credentials via `secretKeyRef`; bestemming in een ander faaldomein. Referentiepraktijk: BIO 8.13.01 noemt ransomware-bestendigheid expliciet; "3-2-1 met één onveranderlijke kopie" is de norm.

#### H-16. Geen back-up van de platformstate en drie single points of failure (G-3)
- **Ernst**: Hoog. **Zekerheid**: bevestigd voor git. **BIO2**: 8.13, 8.14, 5.30.
- **Wat**: `rig-db` (gebruikers, runs, subdomeinregister, verwijdermarkeringen) draait met één instance en het `barmanObjectStore`-blok staat uitgecommentarieerd; Keycloak en Forgejo één replica; OPI één pod met inline worker; de hersteltest is nog niet gedaan. Verlies van de Keycloak-database betekent verlies van alle lokale accounts uit invites.
- **Aanbeveling**: CNPG-back-ups voor `rig-db` en de Keycloak-database; Forgejo meenemen; de hersteltest uitvoeren en de RTO vastleggen; PDB's en `instances: 2`.

#### H-17. De productie-image wordt op een laptop gebouwd en gepubliceerd, buiten CI om (H-13, F-11)
- **Ernst**: Hoog. **Zekerheid**: bevestigd. **BIO2**: 8.32, 8.19, 5.21, 8.31.
- **Wat**: `task publish-operations-manager` bouwt lokaal en pusht naar `ghcr.io/minbzk/base-images`; CI bouwt en scant een andere image. Productie draait `2026.09.02.2241-d81cdab4-dirty`: gebouwd uit een werkboom met niet-gecommitte wijzigingen, dus niet herleidbaar tot een commit; de deploy ervoor ook. Alle controlepunten uit de keten toetsen daarmee een image die niet draait.
- **Aanbeveling**: alleen CI-images in productie; de Taskfile weigert bij `-dirty`; provenance, SBOM en cosign-signing; verificatie op admission. Referentiepraktijk: SLSA Build L2 en hoger eist een gehoste build die de ontwikkelaar niet kan beïnvloeden; bij Logius en DICTU is dit een harde eis voor productie.

#### H-18. Main is te mergen zonder review, met force-push en zonder handtekeningen (H-1)
- **Ernst**: Hoog. **Zekerheid**: bevestigd via de repo-instellingen. **BIO2**: 8.32.01, 8.04, 5.03.
- **Wat**: `required_approving_review_count: 0`, `allow_force_pushes: true`, `enforce_admins: false`, geen verplichte handtekeningen, een bypass-allowance voor één gebruiker; drie van de vijf laatste merges zonder review; geen `CODEOWNERS`.
- **Aanbeveling**: minimaal één review door een ander, `enforce_admins`, geen force-push, lineaire historie, `CODEOWNERS` op de vertrouwensgrens-paden, commit-signing. Referentiepraktijk: OpenSSF Scorecard "Branch-Protection" tier 3; de twee-persoonsregel is bij Rijksplatforms standaard.

#### H-19. Kwetsbaarheidsmeldingen worden gegenereerd maar niet afgehandeld (H-2, H-11)
- **Ernst**: Hoog. **Zekerheid**: bevestigd. **BIO2**: 8.08.01 (binnen een week bij hoog risico), 8.08.02/03.
- **Wat**: 227 open Dependabot-alerts (oudste oktober 2025), 545 open code-scanning-alerts (Trivy 7 critical en 281 high; CodeQL 1 critical en 28 high), Dependabot security updates uit, Renovate geconfigureerd maar zonder enige PR (dus aannemelijk niet geïnstalleerd). Starlette high-CVE's open sinds juni terwijl `starlette==0.50.0` hard gepind staat. Trivy faalt de build niet.
- **Aanbeveling**: wekelijkse triage met eigenaar en de 8.08-termijnen in `SECURITY.md`; Trivy en pip-audit blokkeren op critical en high met een `.trivyignore` met vervaldatum; Renovate of Dependabot werkend maken. Referentiepraktijk: teams met een hoog niveau houden het aantal open high-alerts structureel op nul en zien een niet-nul-stand als incident.

#### H-20. Rechtenmodel: de rol "member" is te breed en er is geen negatieve test per grens (B-3, B-1, B-17, G-9)
- **Ernst**: Hoog (samenvattend). **Zekerheid**: bevestigd. **BIO2**: 5.15, 5.18, 8.03, 8.29.
- **Wat**: de bevindingen H-9, H-10, H-11 en H-12 hebben één oorzaak: er zijn drie rollen en één vlag, en de grens tussen "lezen" en "handelen" is per route met de hand getrokken, zonder test die afdwingt dat elke route een herkenning en een rolgrens draagt. Het rechtenontwerp in `features/futures/rechten-*.md` benoemt dit al. De lost update op het projectbestand (G-9) maakt het erger: een verwijderd lid kan stil terugkomen.
- **Aanbeveling**: het rechtenontwerp uitvoeren (grants met vervaltijd voor operationele handelingen), en één test die alle routes enumereert en voor elke route een expliciete herkenning en rolgrens eist; per grens een negatieve test. Referentiepraktijk: Wies heeft een row-level permission engine met tests per veld; ZAD als platform verdient minimaal een route-level equivalent.

### 8.4 Midden

Samengevoegd per thema; de deel-ID's verwijzen naar het bewijs.

| Nr | Thema en kern | Deel-ID's | BIO2 | Aanbeveling in één zin |
|---|---|---|---|---|
| M-1 | Sessies zijn stateless getekende cookies met een schuivend 8-uursvenster, zonder absolute levensduur en zonder server-side intrekking; een gestolen cookie overleeft logout. | A-3, A-14 | 8.05, 5.15 | Absolute levensduur plus een revocatielijst op `sub`+`issued_at` in de al aanwezige Redis; `sid` bewaren voor backchannel-logout. |
| M-2 | `kubectl` met stdin loopt via `sh -c` met een heredoc: secret-manifesten (inclusief de projectsleutel) staan in argv van de shell, en een regel `EOF` in een waarde breekt uit. | D-6, E-4, C-14 | 8.28, 8.24 | Eén regel: `create_subprocess_exec` met `stdin=PIPE`, zoals `age.py` en `minio_mc.py` al doen. |
| M-3 | Git-credentials in de clone-URL: in `.git/config` van de warme clone, in argv, in debug-logs (`http://` niet geobfusceerd), `StrictHostKeyChecking=no` op SSH; OPI kloont met de platform-PAT naar elke host uit het projectbestand. | D-10, E-13, F-17, F-15 | 5.17, 8.12, 8.24 | Credential via `GIT_ASKPASS` of `http.extraHeader`, host-allowlist, `accept-new` met beheerd `known_hosts`. |
| M-4 | Externe clone- en restore-doelen: OPI verbindt naar elke host:poort met onderscheidende foutmeldingen (SSRF-orakel, poortscan op `rig-db`), en een restore naar een externe database is een exfiltratiekanaal met alleen de project-API-key; het doelwachtwoord landt in `async_tasks.payload`. | E-5, B-14, G-10 | 8.26, 8.12, 8.15 | Allowlist of expliciete goedkeuring voor externe doelen, uniforme foutmelding, gebeurtenis met actor en doelhost, wachtwoord uit de payload. |
| M-5 | IDOR en tokenbinding: taakvoortgang via de modal-wizard zonder lidmaatschapscontrole; wizard-attachmentroutes en modal-state niet aan het pad-project gebonden; `_wizard_token` is een ongebonden bearer. | B-2, B-10, E-20 | 5.15, 8.03 | `_require_task_of_project` overal; state aan sessie en pad-project binden. |
| M-6 | `GET /api/v2/projects` met een kortlevend SSO-token levert de langlevende API-key van elk beheerd project; voor een platform-admin alle keys. | B-5, A-14 | 5.17, 8.03 | Key per project achter een aparte, gelogde aanroep. |
| M-7 | Metrics zonder authenticatie: `/metrics` van OPI publiek via de Ingress met `command`-labels uit `/proc/<pid>/cmdline` (kopia krijgt secrets op argv); Prometheus op internet achter een IP-range zonder auth; Keycloak `rig-metrics` zonder auth; één metrics-token voor alle tenants. | G-4, B-6, F-13, F-25, C-11, E-14 | 8.15.02, 8.16, 8.20 | `/metrics` op de probe-poort, `command`-label weg, secrets naar kopia via env, oauth2-proxy voor Prometheus, token per project. |
| M-8 | Logging: `opi`-logger hard op DEBUG zonder `LOG_LEVEL`, e-mail per verzoek, invite-keys op INFO en in de access-log, API-key in een debugregel, `ProxyHeadersMiddleware(trusted_hosts=["*"])` met het linker XFF-adres als client-IP (spoofbaar, rate-limiter omzeilbaar). | G-5, D-14, A-4, G-11, E-15 | 8.15.02/03, 5.34, 8.20 | `LOG_LEVEL` instelbaar en INFO in productie, redactiefilter op de handler, `trusted_hosts` op de router-CIDR en het rechter XFF-adres. |
| M-9 | Geen enkele alert-regel voor beveiliging of beschikbaarheid; foutregels naar `ntfy.sh` (publieke dienst buiten de overheidsketen); backupfalen wordt nergens gemeld. | G-6 | 8.16.03, 5.24, 8.13 | `PrometheusRule` met minimaal OPI down, 401/403-ratio, OOMKilled, PVC-vulgraad, cert-expiry, backup-ouderdom; aansluiten op de Alertmanager van ODCN; ntfy zelf hosten. |
| M-10 | Geen `ResourceQuota` of `LimitRange` per tenant-namespace, geen maximum op replicas of storage, auto-tune-plafond per container in plaats van per namespace. | G-7, E-19 | 8.06, 8.22 | Quota en LimitRange per namespace uit een projectbudget; de tuner respecteert de quota. |
| M-11 | Privacy: geen verwerkingsregister of DPIA-verwijzing; ledenlijsten met e-mail in git en in de publieke productie-configmap (`ALLOWED_EMAILS`); ledenverwijdering laat de Keycloak-gebruiker en de git-historie staan. | G-8 | 5.34, 8.10, 5.33 | Registerbijlage in `docs/`, allowlist naar het SOPS-secret, realm-gebruiker uitschakelen bij ledenverwijdering, bewaartermijn git-historie vastleggen. |
| M-12 | Lost update op het projectbestand is volgens het eigen statusdocument nog open; omdat het bestand de ledenlijst draagt is het een autorisatieprobleem. | G-9 | 5.33, 8.09 | De reproductie een blokkerende test maken en de dubbele save oplossen; elke conflictafhandeling als gebeurtenis loggen. |
| M-13 | Sandbox en productie gekoppeld: sandbox kan de productie-Keycloak gebruiken via een gedeeld clientsecret; productieprojectbestanden worden met dezelfde projectsleutel naar de sandbox herversleuteld (externe API-sleutels van 47 projecten één op één bruikbaar); daarvoor staat de productie-clustersleutel op een werkstation; de sandbox-Forgejo heeft `admin1234`, publieke repos en staat op een publiek resolvende naam. | C-10, H-8, F-16 | 8.31, 8.33, 5.34 | Aparte IdP voor niet-productie (acceptatie-SSO-Rijk), nieuwe projectsleutel en placeholders bij import, herversleuteling op het productiecluster zelf, sandbox-Forgejo privé met gegenereerd wachtwoord. |
| M-14 | Geen zelfbedieningsrotatie voor database-, MinIO-, Keycloak-client- en API-key-secrets; één geldige API-key zonder overlap; gedeeld realm-adminaccount met gedeelde TOTP-seed en het wachtwoord in elke admin-paginaweergave. | D-11, D-12 | 5.17, 8.24.02, 5.16.02 | Roteer-actie per dienst met herstart; twee geldige API-keys met overlap; realm-admins persoonsgebonden via federatie, gedeeld account alleen als break-glass. |
| M-15 | Secret-scan dekt niet alles: alleen op GitHub terwijl het werk via Forgejo loopt, nooit op de historie in CI, mist PEM binnen base64, `ENCRYPTED PRIVATE KEY`, URL-credentials, Forgejo-tokens; 537 van 548 lokale refs dragen nog een sleutel. | D-20, D-21, D-22 | 8.04, 8.28 | Dezelfde scan als Forgejo-job of pre-receive hook, `--history` wekelijks, gitleaks of trufflehog als tweede laag, oude refs opruimen. |
| M-16 | Helm-chart-verwerking doet `rmtree`/`copytree` op ongevalideerde padcomponenten uit een externe repo; upload wordt volledig in geheugen gelezen voordat de groottelimiet geldt. | E-6, E-7 | 8.28, 8.06 | Padcomponenten valideren tegen DNS-1123, streaming met limiet tijdens het lezen (zoals de image-upload al doet). |
| M-17 | Keycloak-clientconfiguratie: extra clients zonder `redirect-uris` krijgen `*`; alle deployment-clients hebben ROPC en service-accounts aan; een projectlid kan externe of wildcard redirect-URI's zetten; de `algoritmeregister`-blueprint maakt demo-gebruikers met `demo123` en rol `admin`; het platformrealm heeft `http://localhost:*/*`-redirects op een confidential client; geen PKCE op de hoofd-OIDC-flow. | E-12, F-23, F-26, F-27, A-8 | 8.05, 8.26 | `redirect-uris` verplicht en nooit `*`, ROPC en service-accounts standaard uit, demo-gebruikers alleen lokaal, PKCE S256 afdwingen (NL GOV OIDC-profiel). |
| M-18 | Gedeelde platform-PostgreSQL met `enableSuperuserAccess`, zonder afgedwongen TLS, bereikbaar vanuit alle tenants; `CONNECT` op `PUBLIC` wordt na `CREATE DATABASE` niet ingetrokken, dus een deployment-rol van A kan verbinden met de database van B; `postInitSQL` draait als superuser zonder filter. | F-10, C-8, E-17, E-18 | 8.03, 8.22, 8.24 | `REVOKE CONNECT ... FROM PUBLIC` en `REVOKE ALL ON SCHEMA public`, `hostssl` in `pg_hba`, `sslmode=require`, en een test die cross-database-connect verwacht te falen. |
| M-19 | Supply chain: geen enkele image op digest, de SOPS-sidecar op productie op `latest`, `jpillora/chisel:latest`, downloads zonder checksum of handtekening in vier Dockerfiles, `kubectl` uit `stable.txt`, custom Keycloak-jars bij elke podstart ongeverifieerd van GitHub, geen provenance/SBOM/signing bij publicatie, actions op mutable tags, `contents: write` in de build-workflow, eigen ArgoCD-fork buiten de scanmatrix. | H-3, H-4, H-5, H-10, F-11, F-24 | 8.19, 5.21, 8.32 | Digest-pinning via Renovate, sha256 en cosign per download, provenance en SBOM aan, cosign keyless signing met admission-verificatie, actions op SHA, jars in een eigen image. |
| M-20 | Security-linting verzwakt waar het het meest telt: ruff `S105/S106/S603/S607/S608` uit voor heel `opi/`, `S701` globaal uit, coverage-ondergrens 21%, CodeQL-resultaten onverwerkt. | H-6 | 8.28, 8.29 | S-regels per plek terug aan met `noqa` en reden, semgrep met projecteigen sinks, dekkingsdrempel per beveiligingskritische map. |
| M-21 | CSP met `'unsafe-inline'` zodat autoescape de enige XSS-verdediging is; op ODCN levert het platform voor tenant-ingresses alleen HSTS omdat de nginx-annotaties door de OpenShift-router worden genegeerd. | A-5, F-12 | 8.26, 8.28 | Nonce-based CSP met `strict-dynamic`, Chart.js zelf hosten; headers voor tenants via een platform-sidecar of expliciet als afnemersplicht documenteren en toetsen. |
| M-22 | Fail-open of onnodig oppervlak: `DEBUG_MODE` default `reload` en de `debug`-stand opent debugpy op 0.0.0.0:5678 (poort ook als Service gepubliceerd; productie staat aantoonbaar niet in die stand en de netpol laat alleen 8000 toe); Swagger en OpenAPI publiek; de LOTC-proefopstelling met beheerdersschermen publiek in de productie-image; help-route rendert elk template. | A-6, A-7, A-11, B-12, B-13 | 8.09, 8.31, 8.03 | Default `production`, debugpy op localhost, `EXPOSE 5678` weg; docs achter SSO; test- en demorouters alleen bij `ENVIRONMENT=local`. |
| M-23 | Cross-domain-access laat een project zonder platformgoedkeuring een poort voor elke bron in het cluster openzetten (wildcard `from`). | C-12 | 8.22, 8.02 | `ApprovalSpec` op de wildcard-vorm, zoals het commentaar zelf voorziet. |
| M-24 | Configuratiebeheer: de backup-MinIO, `rig-prd-ron`, de bootstrap en de effectieve RBAC (Capsule) staan buiten ArgoCD en buiten git; Keycloak-events ontbreken in twee van de vier realm-definities; geen `SECURITY.md`, `CODEOWNERS`, threat model of post-mortem-sjabloon. | F-18, G-12, H-9, H-12 | 8.09, 8.15, 5.24, 8.27 | Een runbook "wat staat buiten ArgoCD", periodieke read-only vergelijking, events in alle blueprints, `SECURITY.md` met meldweg en reactietijden, threat model van twee pagina's, post-mortem-sjabloon met maatregelenregister. |

### 8.5 Laag en Info

De 57 lage en informatieve bevindingen staan volledig in de deelrapporten en in bijlage A. Ze zijn hygiëne (dode code met een niet-timing-safe vergelijking, logout via GET, hardcoded ontwikkelsleutels in de sandbox-overlay, sleep-mode-bestaansorakel, restanten van proxy-headerconfiguratie) en vragen geen aparte planning: ze horen bij de opruimronde van het betreffende thema.

## 9. Koppeling aan BIO2 en NORA

Oordeel per control volgens BIO2 v1.3, gesplitst in wat de code en configuratie laten zien en wat de organisatie zelf moet borgen. De BIO richt zich op de entiteit; ZAD kan compliance hoogstens ondersteunen. "Deels" betekent: de maatregel is aanwezig maar met een gat dat in deel 8 staat. De NORA-kolom noemt het thema-object op algemene kennis van de thema-uitwerkingen Applicatieontwikkeling (APO), Toegangsbeveiliging (TBV) en Clouddiensten (CLD); de brontekst van die uitwerkingen is niet geladen.

| BIO2-control | Maatregel (kern) | Oordeel technisch | Bevindingen | Organisatorisch te borgen | NORA-object* |
|---|---|---|---|---|---|
| 5.15 Toegangsbeveiliging | regels voor logische toegang | Deels: middleware fail-closed, maar member-rol te breed | H-9, H-10, H-20, M-5 | toegangsbeleid per rol vaststellen | TBV: toegangsbeleid, autorisatie |
| 5.16 Identiteitsbeheer | sluitende registratie en afmelding, geen groepsaccounts | Deels: allowlist en Keycloak, maar invite-pad en gedeeld realm-admin | H-8, M-14, M-11 | procedure voor in- en uitdienst per project | TBV: identiteitsbeheer |
| 5.17 Authenticatie-informatie | MFA op beheeraccounts, beheerproces voor authenticatie-informatie | Niet: platformsleutel en PAT's publiek geweest en niet vervangen; Keycloak-admin zonder OTP op master | K-1, K-2, H-1, H-3, H-4, H-7 | sleutelregister, uitgifte- en intrekprocedure | TBV: authenticatiemiddelen |
| 5.18 Toegangsrechten | monitoren van accounts met bijzondere rechten, jaarlijkse beoordeling | Deels: goedkeuringen herleidbaar, maar approval-status injecteerbaar en geen audit-spoor | H-11, H-14, H-6 | jaarlijkse rechtenreview, kwartaalreview speciale rechten | TBV: autorisatiebeheer |
| 5.21 Toeleveringsketen | beheer van de ICT-toeleveringsketen | Deels: pip-audit en Trivy aanwezig, maar geen signing, digests of verwerkte alerts | H-17, H-19, M-19 | leveranciersbeoordeling van images en actions | CLD: leveranciersketen |
| 5.24 t/m 5.26 Incidentbeheer | plannen, beoordelen, reageren | Niet aantoonbaar: geen `SECURITY.md`, geen runbook, K-1 niet als incident behandeld | K-1, H-4, M-24 | incidentproces met CISO en ODC-Noord; meldplicht beoordelen | APO: incidentafhandeling |
| 5.28 Bewijsmateriaal | verzamelen van bewijs | Niet: taken na een uur gewist, git-author is de bot | H-14 | bewaartermijnen vaststellen | APO/TBV: logging |
| 5.34 Privacy | AVG-eisen identificeren en naleven | Deels: dataminimalisatie in code, geen register of DPIA-verwijzing | M-11, M-8, M-13 | verwerkingsregister, DPIA-verwijzing, verwerkersovereenkomsten (GitHub, ntfy) | CLD: privacy |
| 8.02 Speciale toegangsrechten | beperkt en per kwartaal beoordeeld | Niet: OPI is één proces met alles; developer-groep ArgoCD-admin; laptops met productiesleutel | H-3, H-6, H-10 | kwartaalbeoordeling, break-glass-procedure | TBV: beheerrechten |
| 8.03 Beperking toegang tot informatie | isoleren van informatie met specifiek belang | Deels: per-bucket en per-realm goed, gedeelde Redis en Postgres niet | H-12, M-18, H-9 | classificatie per project | TBV: gegevenstoegang |
| 8.04 Toegang tot broncode | lees- en schrijftoegang beheerd | Niet: main zonder review, gedeelde PAT, repos zonder herkomstcontrole | H-18, H-1, K-4 | wie mag waar schrijven vastleggen | APO: broncodebeheer |
| 8.05 Beveiligde authenticatie | beveiligde authenticatietechnologie | Deels: OIDC, bearer en CSRF goed; Keycloak-admin en invite-pad zwak; sessies zonder intrekking | H-7, H-8, M-1, M-17 | risicoafweging vastleggen (bestaat voor VPN, ontbreekt voor invites) | TBV: authenticatie |
| 8.06 Capaciteitsbeheer | monitoren en aanpassen | Deels: auto-tune aanwezig, geen quota per tenant | M-10 | budget per project | CLD: capaciteit |
| 8.08 Technische kwetsbaarheden | binnen een week bij hoog risico; jaarlijkse pentest; pentest per release voor internetfacing | Niet: honderden onverwerkte alerts, Keycloak EOL, geen pentest-historie | H-19, H-7, M-19 | triageproces met termijnen; jaarlijkse pentest inplannen (8.08.05) | APO: kwetsbaarhedenbeheer |
| 8.09 Configuratiebeheer | vastgesteld, gedocumenteerd, gemonitord | Deels: GitOps sterk, maar onderdelen buiten ArgoCD en `-dirty`-image | H-17, M-24, M-22 | baseline-document per component | CLD: configuratiebeheer |
| 8.10 Wissen van informatie | wissen op afgesproken moment | Niet: oude sleutels bewaard, historie nooit opgeschoond | H-4, M-11 | wisbeleid voor sleutels en persoonsgegevens | CLD: gegevensverwijdering |
| 8.12 Data leakage | voorkomen van gegevenslekken | Deels: redactie goed, maar plaintext in deployments-repo en secrets in HTML | H-2, H-9, M-7 | classificatie van repo's | APO: gegevensbescherming |
| 8.13 Back-up | bescherming tegen ransomware, integriteit, hersteltest | Niet: gedeelde delete-credential, geen object lock, geen platform-back-up, geen hersteltest | H-15, H-16 | back-upbeleid uitvoeren (bestaat al) | CLD: back-up en herstel |
| 8.14 Redundantie | redundante faciliteiten | Niet: drie single points of failure | H-16 | RTO/RPO vaststellen | CLD: continuïteit |
| 8.15 Logging | actor, object, resultaat, oorsprong, tijd; nooit geheimen; bewaartermijn | Niet: geen actor, retentie 1 uur, invite-keys en e-mail in logs, `/metrics` publiek | H-14, M-7, M-8 | overzicht van logbestanden (8.15.03), bewaartermijn (8.15.04) | APO/TBV: logging en monitoring |
| 8.16 Monitoren | detectie- en responsoplossing | Niet: geen alerts, geen SIEM-koppeling zichtbaar | M-9 | aansluiten op SOC/SIEM van ODCN | CLD: monitoring |
| 8.18 Speciale systeemhulpmiddelen | beperkt, gelogd, half jaar bewaard | Niet: laptops met productiesleutel zonder logging; jobs en db-console voor elke rol | H-3, H-10 | registratie van beheerhulpmiddelen | TBV: beheertoegang |
| 8.19 Installeren van software | beheerst installeren | Niet: ongesigneerde, ongepinde images en downloads | M-19, H-17 | vrijgaveprocedure voor images | CLD: software-installatie |
| 8.20 / 8.21 Netwerkcomponenten en -diensten | beheerinterfaces gescheiden, diensten beveiligd | Deels: OPI-netpol goed ontworpen maar overruled; ArgoCD over HTTP; Prometheus en Keycloak-admin publiek | H-5, H-6, H-7, M-7 | beheerzone definiëren | CVZ: zonering |
| 8.22 Netwerksegmentatie | gescheiden groepen met gedefinieerd niveau | Niet: allow-all live, tenant-egress naar hele ops-namespace, geen default-deny, gedeelde datastores | H-5, H-12, M-18 | zoneringsmodel vastleggen | CVZ/CLD: segmentatie |
| 8.24 Cryptografie | beleid, registratie, sterkte, sleutelbeheer | Deels: AGE/SOPS correct toegepast, sleutelbeheer niet op orde | K-1, H-3, H-4 | cryptografiebeleid (8.24.01) | CLD: cryptografie |
| 8.25 / 8.27 / 8.28 Ontwikkelcyclus, architectuur, veilig coderen | security by design gedocumenteerd, veilig coderen | Deels: veel goede patronen en tests, maar geen threat model, linting verzwakt, injectiefamilie open | K-3, K-4, H-13, M-2, M-20, M-24 | ontwikkelrichtlijn met beveiligingsparagraaf | APO: ontwerp en bouw |
| 8.29 Testen van beveiliging | gestructureerd en geautomatiseerd, met verslag | Deels: guards als tests aanwezig, geen negatieve test per autorisatiegrens, coverage 21% | H-20, M-20 | testplan met beveiligingsparagraaf | APO: testen |
| 8.31 / 8.33 Scheiding OTAP, testgegevens | niet testen in productie, geen productiegegevens in test | Niet: sandbox aan productie-Keycloak, productiebestanden naar sandbox, laptop-builds naar productie | M-13, H-17 | OTAP-document | APO: omgevingen |
| 8.32 Wijzigingsbeheer | administratie, risicoafweging, rollback, goedkeuring | Deels: releasedoorlopen goed gedocumenteerd, maar geen verplichte review en geen herkomstcontrole in GitOps | H-18, K-4, H-17 | wijzigingsproces met goedkeuring | APO: wijzigingsbeheer |

Samenvattend: van de 30 getoetste controls (sommige rijen bundelen er twee of drie) staan er 15 op "niet" en 15 op "deels"; geen control staat als geheel op "voldoende", al zijn er deelaspecten die wel op orde zijn (CSRF en security headers onder 8.26, SQL-hygiëne en de plaintext-guards onder 8.28, de bearer-tokenverificatie onder 8.05). Dat is een strenger beeld dan de eigen compliance-notitie (`features/bio-network-access-no-vpn-compliance.md`) suggereert, en dat komt niet doordat die notitie fout is, maar doordat zij één keuze (geen VPN) verdedigt terwijl de compenserende maatregelen die zij noemt (segmentatie, sterke authenticatie, logging en monitoring) in deze scan juist de zwakste plekken blijken.

## 10. Hoe hieraan te werken

Het beeld is dat van een platform dat door een klein team met veel vakmanschap is gebouwd, met een reeks goede beveiligingskeuzes waar iemand er bewust over nadacht, en met structurele gaten precies waar niemand er als beveiligingsvraag naar heeft gekeken: sleutelbeheer, de vertrouwensgrens van de GitOps-keten, het rechtenmodel, en het audit-spoor. De aanpak hieronder volgt hoe projecten waar beveiliging hoog staat dit doen: eerst het bloeden stoppen, dan de tweede grens inbouwen, dan het proces zo inrichten dat het niet terugkomt.

### 10.1 Nu (dagen): incidentrespons

1. **K-1 en K-2 als incident behandelen.** Vervang alle waarden die onder de oude platformsleutel stonden en het Keycloak-databasewachtwoord; onderzoek de audit-logs vanaf oktober 2025; meld volgens het eigen incidentproces aan CISO en ODC-Noord; beoordeel de meldplicht (Cyberbeveiligingswet, AVG als persoonsgegevens geraakt kunnen zijn). Schrijf de post-mortem.
2. **Containment van K-3 en K-4.** Zet in de CMP `--enable-exec` en `--enable-alpha-plugins` uit voor tenant-apps en parse `.cmp-env` in plaats van het te sourcen; perk het `default`-AppProject in en geef het app-of-apps een eigen project. Dat zijn kleine wijzigingen die de grootste paden sluiten.
3. **De allow-all NetworkPolicy** in `rig-prd-operations` verwijderen of minimaal tot ingress uit de eigen namespace beperken (H-5).
4. **De gedeelde PAT** vervangen door een read-only credential-template voor ArgoCD en de PAT uit de projectbestanden halen (H-1). Tot dan geldt: elke project-admin is potentieel cluster-admin.
5. **`rig-prd-developer-group`** naar `readonly` in ArgoCD (H-6) en de Keycloak-adminconsole achter een allowlist (H-7).

### 10.2 Binnen 30 dagen: de tweede grens

6. **Sleutelbeheer** (H-3, H-4): projectsleutels onder een eigen KEK, sleutel als bestand in plaats van env, persoonlijke recipients voor beheerders, oude sleutels vernietigen, sleutelregister, incident-runbook. Overweeg de al aanwezige `vault`-component of een KMS van ODCN als bewaarplaats.
7. **Rechtenmodel** (H-9 t/m H-12, H-20): ontsleutelen server-side op rol, jobs en db-console achter een expliciete grant, platform-managed velden op alle schrijfwegen, Redis-opt-out achter een approval. Eén test die alle routes enumereert en een herkenning en rolgrens eist.
8. **Injectiefamilie** (H-13): alle user-gestuurde scalars in de manifest-templates door `yaml_scalar` of `tojson`, `format_checker` aan, patterns op de pydantic-modellen, de heredoc-shell in `kubectl.py` vervangen (M-2). Eén test die elk template met een injectiepayload rendert en parseert.
9. **Netwerk** (H-5): default-deny per namespace, ops-egress per dienst en poort, ACME-regel op de solver, PSA `restricted` op elke namespace. Calico in de e2e-Kind-cluster zodat dit toetsbaar is.
10. **Audit-spoor** (H-14): de gebeurtenissentabel uit het bestaande plan bouwen en de dag-één-schrijfwegen erop aansluiten; taakretentie 90 dagen; actor in de commit.
11. **Back-up** (H-15, H-16): per-project S3-gebruiker zonder delete, object lock, CNPG-back-ups voor `rig-db` en Keycloak, de hersteltest doen.
12. **Bouwketen** (H-17, H-18, H-19): alleen CI-images in productie, `-dirty` weigeren, verplichte review op main, triage van alle open alerts met eigenaar en termijn, Renovate werkend.

### 10.3 Binnen 90 dagen: structureel

13. **Threat model en architectuurbesluit over OPI.** Een dataflow-diagram met vertrouwensgrenzen (de blast-radius-analyse uit deel C is het startpunt) en een besluit over het splitsen van OPI in een frontend zonder sleutels en een worker met sleutels; de async-taaklaag bestaat al.
14. **Identity provider als aparte zone.** Keycloak op een eigen database en netwerksegment, acceptatie-IdP voor de sandbox, Keycloak-26-upgrade, PKCE overal, clientconfiguratie strak (M-17).
15. **Supply chain naar SLSA L2/L3.** Digest-pinning, checksums en cosign op downloads, provenance en SBOM aan, keyless signing met admission-verificatie op ODCN, actions op SHA, de ArgoCD-fork in de scanmatrix met een rebase-afspraak.
16. **Logging en monitoring** (M-7, M-8, M-9): logniveau instelbaar, redactiefilter, `/metrics` intern, alert-regels, aansluiting op de Alertmanager en het SOC van ODCN, ntfy zelf hosten.
17. **OTAP en privacy** (M-11, M-13): OTAP-document, import naar sandbox met nieuwe sleutels en placeholders, verwerkingsregister-bijlage, ledenverwijdering die de Keycloak-gebruiker uitschakelt.
18. **Documenten die de BIO vraagt** en die nu ontbreken: `SECURITY.md` met meldweg en 8.08-termijnen, cryptografiebeleid (8.24.01), overzicht van logbestanden (8.15.03), post-mortem-sjabloon met maatregelenregister (5.27), wisbeleid (8.10).

### 10.4 Werkwijze die het laat beklijven

Wat projecten met een hoog beveiligingsniveau anders doen zit minder in tooling dan in een paar gewoontes, en de meeste passen bij hoe dit team al werkt:

- **Elke feature krijgt een beveiligingsparagraaf** in het feature-document: wie mag het, wat kan er misgaan, welke test bewijst dat het dicht is. `features/bio-network-access-no-vpn-compliance.md` en `client-access-restriction.md` laten zien dat het team dit al kan; het is nu de uitzondering, het moet de regel worden. De `instructions/services.md`-haken (`ApprovalSpec`, `owned_value_is_secret`) zijn de plek om die paragraaf af te dwingen.
- **Negatieve tests horen bij de definition of done.** Niet "werkt het voor de admin", maar "faalt het voor de member". Eén route-enumeratietest en één template-injectietest vangen de klassen die in deze scan het vaakst terugkwamen.
- **Een tweede grens is geen luxe.** Bij elke plek waar nu één ding alles beschermt (de platformsleutel, de PAT, de sidecar, het projectbestand) hoort een tweede, onafhankelijke controle: admission-policy, AppProject-beperking, netwerkpolicy, signing. Dat is de kern van BIO 8.27 (security by design).
- **Open alerts zijn nul, of ze zijn een incident.** Een wekelijkse triage van een half uur met een eigenaar houdt dat vol; het huidige aantal is het gevolg van het ontbreken daarvan, niet van slordigheid.
- **Productie is niet van laptops.** Geen productiesleutel, geen productie-image, geen productie-PAT op een werkstation. Alles wat productie raakt loopt via git en CI met een tweede paar ogen. Dat is de enige manier om 8.31 en 8.32 aantoonbaar te maken.
- **Een jaarlijkse externe pentest, en een geautomatiseerde per release** (BIO 8.08.05 voor internetfacing systemen). Het Wies-voorstel is daarvoor een goed sjabloon; laat de eerste ronde precies de scenario's uit deel 8.2 en 8.3 van dit document dekken, op de sandbox met Calico.
- **Security champion in het team**, met een uur per week, die de triage doet, de beveiligingsparagrafen reviewt en het contact met de CISO en ODC-Noord onderhoudt. Bij DICTU en Logius is dat de rol die het verschil maakt tussen een lijst bevindingen en een dalende trend.

## 11. Bijlagen

### Bijlage A. Volledige lijst van bevindingen uit de deelrapporten

| ID | Ernst | Zekerheid | BIO2 | Titel |
|---|---|---|---|---|
| D-1 | Kritiek | BEVESTIGD | 8.24 (cryptografie en sleutelbeheer), 5. | De productie-platformsleutel heeft ruim een jaar in een testbestand op publiek GitHub gestaan; de rotatie verv |
| F-1 | Kritiek | BEVESTIGD voor | 8.04 (toegang tot broncode), 8.24 (crypt | Tenant-gestuurde code-executie in de CMP-sidecar, met de SA die alle `sops-age-key`-secrets kan lezen |
| F-2 | Kritiek | BEVESTIGD voor | 8.32, 8.04, 8.02 (speciale toegangsrecht | App-of-apps `user-applications` op `project: default`: schrijfrecht op de argo-repo is cluster-admin binnen he |
| F-5 | Kritiek | BEVESTIGD voor | 5.17, 8.05, 8.24, 8.09 | Keycloak-databasewachtwoord staat plain in de productie-overlay en wordt zo door ArgoCD uitgerold |
| B-1 | Hoog | BEVESTIGD | 5.15, 8.03, 5.18 | Ontsleutelde secrets in de HTML voor rollen member en developer |
| B-3 | Hoog | BEVESTIGD | 8.02, 5.18, 8.03 | Jobs en db-console: elke projectrol mag code in de namespace uitvoeren met het databasesecret |
| B-4 | Hoog | BEVESTIGD op f | 8.02, 5.18 | Project-admin kan een domein zelf op `approved` zetten via de services-selectie |
| C-1 | Hoog | BEVESTIGD | 8.22 (segmentatie van diensten), 8.03 (b | Redis: een project kan zichzelf toegang tot alle keys van de gedeelde instantie geven |
| D-2 | Hoog | BEVESTIGD in c | 8.24 (cryptografie), 8.12 (gegevenslekke | Ontsleutelde helmfile env-vars worden plat in zad-deployments gecommit |
| D-3 | Hoog | BEVESTIGD | 8.24.01/02 (registratie en actueel houde | Eén platformsleutel ontsluit alles, en hij ligt op meer plekken dan de kluis |
| D-4 | Hoog | BEVESTIGD | 5.17 (authenticatie-informatie), 8.24, 8 | Eén gedeelde GitHub-PAT met schrijfrechten staat in elk projectbestand en elke ArgoCD-repository-secret |
| D-5 | Hoog | BEVESTIGD | 8.24.02, 8.10 (wissen), 5.17 | Rotatie hersleutelt maar vervangt niet; de historie blijft leesbaar; er is geen incident-runbook |
| E-1 | Hoog | BEVESTIGD | 8.28 (veilig coderen: output encoding),  | YAML-injectie in de ArgoCD app-of-apps repo via `repositories[].username`, `.password` en `.branch` |
| E-2 | Hoog | BEVESTIGD | 8.28, 8.26. | YAML-injectie in de CNPG `Cluster`-CR via de dedicated-Postgres serviceconfig |
| E-3 | Hoog | BEVESTIGD | 8.28, 8.26. | Manifest-injectie in direct toegepaste restore/backup-pods via API-body en URL-padsegmenten |
| F-3 | Hoog | BEVESTIGD voor | 8.05 (beveiligde authenticatie), 5.17 (a | Eén schrijf-PAT voor alle repos, in elk projectbestand en elk ArgoCD-repository-Secret |
| F-4 | Hoog | BEVESTIGD voor | 8.32, 8.04, 8.09 | Schrijfrecht op de deployments-repo geeft controle over andermans namespace, zonder controle op herkomst |
| F-6 | Hoog | BEVESTIGD | 8.20, 8.22 (netwerkscheiding), 8.09 | `emergency-restore-allow-all` staat nog in de productie-kustomization |
| F-7 | Hoog | BEVESTIGD voor | 8.02, 5.15, 8.32 | `rig-prd-developer-group` is `role:admin` in ArgoCD |
| F-20 | Hoog | publieke berei | 8.05, 8.02, 8.20, 5.17 | Keycloak-adminconsole en master-realm staan op internet; master zonder brute-force-bescherming, `admin` zonder |
| F-21 | Hoog | AANNEMELIJK | 8.05, 8.02, 5.17 | OPI in productie draait aannemelijk nog op het gedeelde Keycloak-adminwachtwoord |
| F-22 | Hoog | AANNEMELIJK | 8.05, 5.16 (identiteitsbeheer), 8.26 | Lokale invite laat de gebruiker zijn eigen e-mailadres kiezen; met account-linking op e-mail is dat een impers |
| G-1 | Hoog | BEVESTIGD | 8.15 (logging, overheidsmaatregel 8.15.0 | Geen persistent audit-spoor met actor voor beveiligingsrelevante handelingen |
| G-2 | Hoog | BEVESTIGD voor | 8.13 (back-up, overheidsmaatregel 8.13.0 | Back-up: platformbrede S3-admin-credentials in elke tenant-back-up-pod, verwijderrecht, geen object lock, zelf |
| G-3 | Hoog | BEVESTIGD voor | 8.13 (back-up), 8.14 (redundantie van in | Geen back-up van de platformstate en drie single points of failure zonder redundantie |
| H-1 | Hoog | BEVESTIGD | 8.32 wijzigingsbeheer (8.32.01 goedkeuri | Main is te mergen zonder review, met force-push en zonder handtekeningen |
| H-2 | Hoog | BEVESTIGD | 8.08 technische kwetsbaarheden (8.08.01: | Kwetsbaarheidsmeldingen worden gegenereerd maar niet afgehandeld |
| H-13 | Hoog | BEVESTIGD | 8.32 wijzigingsbeheer, 8.19 installeren  | De productie-image wordt op een laptop gebouwd en gepubliceerd, buiten CI om |
| A-1 | Midden | BEVESTIGD | 5.16 (identiteitsbeheer), 8.26 (beveilig | Ongeauthenticeerde, onbegrensde accountaanmaak en mailverzending via invite-registratie |
| A-2 | Midden | BEVESTIGD | 5.17 (authenticatie-informatie), 8.15 (l | Invite-keys zijn zelf te kiezen vanaf 3 tekens en worden op INFO gelogd |
| A-3 | Midden | BEVESTIGD | 8.5 (beveiligde authenticatie), 5.15 (to | Sessies zijn stateless getekende cookies zonder intrekking en zonder absolute levensduur |
| B-2 | Midden | BEVESTIGD | 5.15, 8.03 | IDOR op taakvoortgang via de modal-wizard |
| B-5 | Midden | BEVESTIGD | 8.02, 5.15 | `GET /api/v2/projects` geeft de API-key van elk project terug waar de aanroeper admin/owner is; voor een platf |
| B-6 | Midden | BEVESTIGD | 8.03, 5.15 | `/metrics` publiceert command-lines van subprocessen zonder authenticatie |
| B-7 | Midden | AANNEMELIJK | 5.18, 8.02 | Restore met `restore_mode == new` laat een member het projectbestand wijzigen |
| B-8 | Midden | BEVESTIGD | 5.18, 5.15 | Invite-links verlopen niet, zijn meervoudig bruikbaar en ongelimiteerd |
| B-17 | Midden | BEVESTIGD | 5.15, 8.02 | Testdekking: de meeste autorisatiegrenzen hebben geen negatieve test |
| C-2 | Midden | AANNEMELIJK | 8.27 (veilige systeemarchitectuur), 8.18 | De ArgoCD CMP voert `kustomize build --enable-exec` uit op inhoud die uit willekeurige git-repo's van een proj |
| C-3 | Midden | BEVESTIGD | 8.02, 8.13 (back-up), 8.22, 8.24 | Backuppods dragen de platformbrede S3-credentials van de backup-MinIO als platte env-vars in elke tenant-names |
| C-4 | Midden | BEVESTIGD | 8.22, 8.20 | De ACME-NetworkPolicy opent poort 80 en 8089 van alle pods in de namespace voor elke bron |
| C-5 | Midden | BEVESTIGD | 8.22, 8.27 | Geen namespace-brede default-deny: helm/helmfile-pods en unlabelde pods staan volledig open |
| C-6 | Midden | BEVESTIGD voor | 8.27, 8.20; CIS Kubernetes 5.2 / Pod Sec | Geen Pod Security Admission op de gegenereerde namespaces |
| C-7 | Midden | BEVESTIGD | 8.22, 8.20, 8.03 | Tenant-pods mogen op alle poorten naar de hele operations-namespace |
| C-9 | Midden | BEVESTIGD | 8.02 (speciale toegangsrechten beperken  | OPI is één proces met alle platformsleutels en tenant-admin in elke namespace |
| C-10 | Midden | BEVESTIGD | 8.31 (scheiding van ontwikkel-, test- en | Productiegeheimen reizen mee naar de sandbox, en de productie-clustersleutel staat daarvoor op een werkstation |
| D-6 | Midden | BEVESTIGD | 8.24, 8.12 | Secret-manifesten, inclusief de projectsleutel, gaan via een shell-heredoc door argv |
| D-7 | Midden | AANNEMELIJK | 8.24, 8.04, 5.15 | De CMP-sidecar draait onder de argocd-server-ServiceAccount en voert repo-inhoud uit |
| D-8 | Midden | BEVESTIGD | 5.17, 8.10, 8.24 | Werkbestanden in `security/` zijn world-readable, waaronder PAT's en oude platformsleutels |
| D-9 | Midden | BEVESTIGD | 8.04, 8.24, 5.17 | Productie-PAT als ciphertext hardcoded in broncode en migratiescript; zad-deployments-credential ontbreekt in  |
| D-10 | Midden | BEVESTIGD | 5.17, 8.12 | Git-PAT in de clone-URL: op schijf in `.git/config`, in argv, en de obfuscatie dekt geen `http://` |
| D-11 | Midden | BEVESTIGD | 5.17, 8.24.02 | Geen zelfbedieningsrotatie voor database-, MinIO-, Keycloak-client- en API-key-secrets |
| D-12 | Midden | BEVESTIGD | 5.17 (authenticatie-informatie is persoo | Gedeeld realm-adminaccount met gedeelde TOTP-seed |
| D-19 | Midden | BEVESTIGD | 8.24, 5.17, 8.04 | Een OpenSSH-privésleutel stond op zes paden in de historie van de publieke repo |
| D-20 | Midden | BEVESTIGD | 8.04, 8.28 (veilig ontwikkelen), 8.24 | De bindende secret-scan draait alleen op GitHub, het werk loopt via Forgejo, en de historie wordt nergens gesc |
| D-21 | Midden | BEVESTIGD | 8.04, 8.28 | De scanner mist PEM-sleutels binnen base64 en een reeks tokenvormen |
| D-22 | Midden | BEVESTIGD | 8.24, 8.10 | Nog drie andere geldige AGE-sleutels in de publieke historie, en 537 van 548 lokale refs dragen er nog een |
| E-4 | Midden | BEVESTIGD | 8.28, 8.24 (cryptografie: sleutelbeheer) | `kubectl` met stdin loopt door `sh -c` met een heredoc; secrets staan in de argv van de shell |
| E-5 | Midden | BEVESTIGD | 8.28, 8.26, 8.20 (netwerkbeveiliging), 8 | Externe clone-endpoints: OPI verbindt naar elke host:poort en geeft een onderscheidend foutantwoord (SSRF-orak |
| E-6 | Midden | BEVESTIGD | 8.28, 8.26, 8.30 (uitbestede ontwikkelin | Helm-chart-verwerking: `rmtree`/`copytree` op ongevalideerde padcomponenten, ook uit een externe repo |
| E-7 | Midden | BEVESTIGD | 8.26, 8.6 (capaciteitsbeheer), 8.28. | Upload wordt volledig in geheugen gelezen voordat de groottelimiet geldt |
| E-8 | Midden | BEVESTIGD voor | 8.28, 8.26. | YAML-injectie via `components[].resources` in `deployment.yaml.jinja` (naive quotes, geen schema-pattern) |
| E-9 | Midden | BEVESTIGD voor | 8.28. | Sleutels in `generic-secret.yaml.to-sops.jinja` staan kaal; de objectvorm van `user-env-vars` valideert de sle |
| E-10 | Midden | BEVESTIGD | 8.28, 8.26, 5.15 (toegangsbeveiliging: d | `users[].email` staat kaal in een block scalar en `format: email` wordt niet afgedwongen |
| E-11 | Midden | BEVESTIGD | 8.28. | `metrics-scraper` `path` naive gequote in een pod-annotatie |
| E-12 | Midden | BEVESTIGD | 8.26, 8.5 (beveiligde authenticatie), 5. | Keycloak: externe of wildcard redirect URIs en webOrigins zijn door een projectlid te zetten |
| F-8 | Midden | BEVESTIGD | 8.05, 8.24, 8.02 | OPI gebruikt het lokale ArgoCD-adminaccount over plain HTTP |
| F-9 | Midden | BEVESTIGD | 8.20, 8.22 | Tenant-baseline laat egress naar de hele ops-namespace toe, en OPI's eigen ingress-regel heeft geen bron |
| F-10 | Midden | BEVESTIGD | 8.20, 8.24, 8.03 | Gedeelde platform-PostgreSQL met superuser-toegang aan, bereikbaar vanuit alle tenant-namespaces, zonder afged |
| F-11 | Midden | BEVESTIGD | 8.09, 8.19 (installatie van software), 8 | Ongepinde of dubbelzinnige images in de productieketen |
| F-12 | Midden | BEVESTIGD | 8.26 (applicatiebeveiligingseisen), 8.24 | Security-headers uit de tenant-ingress-template werken alleen op nginx; op ODCN levert het platform alleen HST |
| F-13 | Midden | BEVESTIGD | 8.05, 8.20, 8.15 (logging) | Prometheus op internet achter alleen een IP-range, zonder auth |
| F-15 | Midden | AANNEMELIJK | 8.05, 5.17 | Repository-URL uit het projectbestand stuurt waar OPI met de platform-PAT naartoe kloont |
| F-16 | Midden | BEVESTIGD | 8.31 (scheiding omgevingen), 8.05, 8.04 | Sandbox is wezenlijk zwakker en staat op een publiek resolvende naam |
| F-23 | Midden | BEVESTIGD | 8.05, 8.26 | Extra clients zonder `redirect-uris` krijgen `redirectUris: ["*"]` en `webOrigins: ["*"]`; alle deployment-cli |
| F-24 | Midden | geen checksum  | 8.19, 8.09, 8.32 | Custom Keycloak-jars komen bij elke podstart ongeverifieerd van GitHub |
| F-25 | Midden | BEVESTIGD in b | 8.05, 8.15 | Custom `rig-metrics`-endpoint zonder authenticatie op de publieke poort |
| F-26 | Midden | BEVESTIGD | 8.05, 5.17 | `algoritmeregister`-blueprint maakt demo-gebruikers met wachtwoord `demo123` en rol `admin`, en een publieke c |
| F-27 | Midden | BEVESTIGD | 8.05, 8.26 | Platformrealm `rig-platform` heeft een confidential client met `http://localhost:*/*`-redirects en mist de zel |
| F-28 | Midden | BEVESTIGD | 8.08 (technische kwetsbaarheden), 8.19 | Keycloak 25.0.6 is sinds oktober 2024 zonder security-fixes; image op tag |
| G-4 | Midden | BEVESTIGD voor | 8.15.02 ("een logregel bevat nooit gegev | `/metrics` is zonder authenticatie publiek bereikbaar en bevat de command-lines van kindprocessen |
| G-5 | Midden | BEVESTIGD | 8.15.02, 8.15.03 (bescherming van loginf | `opi`-logger staat onvoorwaardelijk op DEBUG; e-mailadressen bij elk verzoek, invite-keys op INFO, API-sleutel |
| G-6 | Midden | BEVESTIGD voor | 8.16 (monitoren van activiteiten, overhe | Geen enkele alerting-regel voor beveiliging of beschikbaarheid; foutregels gaan naar ntfy.sh |
| G-7 | Midden | BEVESTIGD voor | 8.06 (capaciteitsbeheer), 8.14, 8.22 (sc | Geen ResourceQuota of LimitRange per tenant-namespace; het auto-tune-plafond geldt per container |
| G-8 | Midden | BEVESTIGD voor | 5.34 (privacy en bescherming van persoon | Privacy: geen verwerkingsregister of DPIA-verwijzing, persoonsgegevens in git en in de configmap, verwijdering |
| G-9 | Midden | BEVESTIGD | 8.15 nee; 5.33 (bescherming van registra | Lost update op het projectbestand is nog open |
| H-3 | Midden | BEVESTIGD | 8.19 installeren van software, 8.09 conf | Geen enkele image is op digest gepind; productie draait onderdelen op `latest` |
| H-4 | Midden | BEVESTIGD | 8.19, 5.21, 8.07. | Binaries in de images worden gedownload zonder checksum of handtekening |
| H-5 | Midden | BEVESTIGD | 5.21, 8.32, 8.04. | Images worden ongesigneerd en zonder provenance gepubliceerd; het workflow-token kan naar main schrijven |
| H-6 | Midden | BEVESTIGD | 8.28 veilig coderen, 8.29 testen van bev | Security-linting is verzwakt waar het het meest telt |
| H-7 | Midden | BEVESTIGD | 8.02 speciale toegangsrechten (8.02.01 k | De productiesleutel en PAT's staan op ontwikkelaarslaptops, zonder break-glass en zonder logging |
| H-8 | Midden | BEVESTIGD | 8.31 scheiden van ontwikkel-, test- en p | Sandbox en productie zijn gekoppeld via Keycloak en via productiebestanden |
| A-4 | Laag | BEVESTIGD | 8.20 (netwerkbeveiliging), 8.26 | Proxy-headers van iedereen vertrouwd; rate-limiter leest het linker XFF-adres |
| A-5 | Laag | BEVESTIGD | 8.26, 8.28 (veilig coderen) | CSP staat `'unsafe-inline'` toe voor scripts en stijlen |
| A-6 | Laag | BEVESTIGD voor | 8.9 (configuratiebeheer), 8.31 (scheidin | `DEBUG_MODE` valt standaard op `reload`; `debug` opent een ongeauthenticeerde debugger op 0.0.0.0 |
| A-7 | Laag | BEVESTIGD | 8.3 (beperking toegang tot informatie),  | OpenAPI en Swagger-UI zijn zonder authenticatie bereikbaar |
| A-8 | Laag | BEVESTIGD voor | 8.5, 8.24; NL GOV OpenID Connect-profiel | Geen PKCE op de hoofd-OIDC-flow |
| A-9 | Laag | BEVESTIGD voor | 8.20, 8.24, 8.9 | ArgoCD-verbinding zonder TLS, met standaard admin-wachtwoord in de defaults |
| A-10 | Laag | AANNEMELIJK | 5.16, 8.5 | Invite-SSO-pad zonder `email_verified` en met binding op bestaand realm-account via e-mail |
| A-11 | Laag | BEVESTIGD | 8.31 (scheiding ontwikkel/test/productie | Publieke LOTC-proefopstelling draait mee in de productie-image |
| B-9 | Laag | BEVESTIGD | 8.03 | Service-config leesroute redigeert niet |
| B-10 | Laag | AANNEMELIJK | 5.15 | Wizard-attachmentroutes en modal-state zijn niet aan het pad-project gebonden |
| B-11 | Laag | BEVESTIGD | 8.03 | Sleep-mode geeft een bestaansorakel zonder authenticatie |
| B-12 | Laag | AANNEMELIJK | 8.03 | Help-route rendert elk template dat de loader kent |
| B-14 | Laag | AANNEMELIJK | 8.03 | Externe clone- en restore-doelen: SSRF-vorm by design |
| B-16 | Laag | AANNEMELIJK | 5.18 | Lid verwijderen synct de allowlist niet terug |
| C-8 | Laag | AANNEMELIJK | 8.03, 8.22 | Op de gedeelde PostgreSQL kan de gebruiker van project A verbinden met de database van project B |
| C-11 | Laag | BEVESTIGD | 8.22, 8.24 | Eén metrics-bearer-token voor alle projecten, als Secret in elke tenant-namespace |
| C-12 | Laag | BEVESTIGD | 8.22, 8.02 | Cross-domain-access laat een project zonder platformgoedkeuring een poort voor iedereen openzetten |
| C-13 | Laag | BEVESTIGD | 8.32, 8.02 | AppProject `sourceRepos: '*'` en app-of-apps in AppProject `default` |
| C-14 | Laag | BEVESTIGD voor | 8.28 (veilig coderen), 8.02 | kubectl-invoer via een shell-heredoc: een regel `EOF` in een manifest breekt uit |
| D-13 | Laag | BEVESTIGD | 5.17 | API-key-lookup met `==` en een dood verificatiepad met de verkeerde sleutel |
| D-14 | Laag | BEVESTIGD | 8.12, 8.15 | DEBUG-logging en een API-key in een debugregel |
| D-15 | Laag | BEVESTIGD | 8.12, 5.15 | De projectpagina ontsleutelt de projectsleutel, de API-key en adminwachtwoorden voor elke rol; alleen het temp |
| D-16 | Laag | BEVESTIGD | 8.24, 8.04 | Plaintext is toegestaan in het projectbestand (`plain:`, aliases zonder sleutel, sandbox-credentials in git) |
| D-17 | Laag | BEVESTIGD | 8.24 | Sleutelmateriaal in de procesomgeving en op de emptyDir van de node |
| E-13 | Laag | BEVESTIGD | 8.24, 8.15. | git-credentials in de clone-URL: in argv en in `.git/config`, geen `GIT_TERMINAL_PROMPT=0` |
| E-14 | Laag | BEVESTIGD | 8.24, 8.15. | kopia, chisel en mc krijgen wachtwoorden en access keys op argv |
| E-15 | Laag | BEVESTIGD | 8.15 (logging), 8.28. | Log-injectie via ongeauthenticeerde invite-routes; geen newline-escaping in de formatter |
| E-16 | Laag | BEVESTIGD voor | 8.24, 8.15. | Rolwachtwoord staat letterlijk in de DDL-tekst; asyncpg is geïnstrumenteerd |
| E-17 | Laag | BEVESTIGD | 8.26, 8.2. | `postInitSQL` draait als CNPG-superuser zonder filter of approval |
| E-18 | Laag | BEVESTIGD voor | 5.15, 8.3. | Database-console: rechten zijn die van de rol, geen statement-scope; PUBLIC CONNECT niet ingetrokken |
| E-19 | Laag | AANNEMELIJK | 8.6. | Geen ResourceQuota/LimitRange per namespace en geen `maxItems` op arrays |
| E-20 | Laag | BEVESTIGD | 8.5, 8.26. | `_wizard_token` is een ongebonden bearer |
| E-21 | Laag | BEVESTIGD | 8.28. | Usage-pagina zet de `namespace`-parameter letterlijk in een PromQL-regex (alleen platform-admin) |
| F-14 | Laag | BEVESTIGD | 8.20, 8.22 | Tenant-egress naar internet staat open, ook op de namespace-annotatie |
| F-17 | Laag | BEVESTIGD | 8.24, 8.15 | `StrictHostKeyChecking=no` op het SSH-pad en credentials in debug-logregels en argv |
| F-18 | Laag | BEVESTIGD | 8.09, 8.32 | Configuratiebeheer (8.09): niet alles staat onder ArgoCD, en de RBAC-laag staat buiten de repo |
| G-10 | Laag | BEVESTIGD voor | 8.12 (voorkomen van gegevenslekken), 8.2 | Restore naar een externe database met wachtwoord in de aanvraag |
| G-11 | Laag | BEVESTIGD | 8.05 (beveiligde authenticatie), 8.06, 8 | Geen rate-limiting op login-callback, invite en API-sleutel; client-IP is spoofbaar |
| G-12 | Laag | BEVESTIGD | 8.15. | Keycloak-events ontbreken in twee realm-definities en zijn niet in de Keycloak-bootstrap van `infrastructure/` |
| H-9 | Laag | BEVESTIGD | 5.24 incidentbeheer, 6.08 melden van geb | Geen SECURITY.md, geen CODEOWNERS, geen threat model |
| H-10 | Laag | BEVESTIGD | 8.08, 8.19. | Eigen ArgoCD-fork en een CMP-sidecar buiten de scanmatrix |
| A-12 | Info | BEVESTIGD | 8.26, 8.28 | Restanten: `/auth/user` debug-endpoint en logout via GET |
| A-13 | Info | BEVESTIGD | 5.17, 8.9 | Hardcoded ontwikkelsleutels in bron en sandbox-overlay |
| A-14 | Info | BEVESTIGD | 8.24, 5.17 | Sessiecookie draagt het id_token; één bearer-token levert alle beheerde project-API-keys |
| A-15 | Info | AANNEMELIJK | 8.28 | Authorization-wall-banner ongevalideerd naar oauth2-proxy `SignInMessage` |
| A-16 | Info | BEVESTIGD | 8.28 | Niet-timing-safe sleutelvergelijking in dode code |
| B-13 | Info | BEVESTIGD | 8.03 | Test- en demoroutes staan aan in productie |
| B-15 | Info | BEVESTIGD | 5.18 | Projectaanmaak: geen quotum, wel juiste rolvastlegging |
| G-13 | Info | BEVESTIGD | 8.15.03, 8.24. | Informatief: taakpayloads en -logs staan onversleuteld in `async_tasks`; `subdomain_registry.created_by` wordt |
| H-11 | Info | AANNEMELIJK | 8.08. | Renovate is geconfigureerd maar lijkt niet te draaien |
| H-12 | Info | BEVESTIGD | 5.27 leren van incidenten, 5.26. | Post-mortems zonder vaste vorm en zonder maatregelenregister |
| C-15 | Info |  |  | Overige waarnemingen (Info) |
| D-18 | Info |  |  | Restpunten (Info) |
| D-23 | Info |  |  | Restpunten uit de repo-hygiëne (Laag) |
| E-22 | Info |  |  | Info-bevindingen (geen actie vereist, wel benoemd) |
| F-19 | Info |  |  | Info: overige observaties |
| F-29 | Info |  |  | Laag en Info rond Keycloak |

Ernst per deel:

| Deel | Kritiek | Hoog | Midden | Laag | Info | Totaal |
|---|---|---|---|---|---|---|
| A | 0 | 0 | 3 | 8 | 5 | 16 |
| B | 0 | 3 | 6 | 6 | 2 | 17 |
| C | 0 | 1 | 8 | 5 | 1 | 15 |
| D | 1 | 4 | 11 | 5 | 2 | 23 |
| E | 0 | 3 | 9 | 9 | 1 | 22 |
| F | 3 | 7 | 14 | 3 | 2 | 29 |
| G | 0 | 3 | 6 | 3 | 1 | 13 |
| H | 0 | 3 | 6 | 2 | 2 | 13 |
| Totaal | 4 | 24 | 63 | 41 | 16 | 148 |

### Bijlage B. Open vragen voor een live verificatie (read-only)

Deze vragen zijn niet uit git te beantwoorden. Ze bepalen of een bevinding van aannemelijk naar bevestigd gaat of van ernst verandert. Alle controles zijn read-only uit te voeren.

**Deel A**
- **Waarde van `DEBUG_MODE` in productie.** Staat in het SOPS-secret; zonder de AGE-sleutel niet te lezen. Als hij niet `production` is, schuift A-6 naar Hoog of Kritiek. Te verifiëren met `kubectl -n rig-prd-operations exec deployment/operations-manager -- env | grep DEBUG_MODE` (read-only, viel buit...
- **authlib `state` en `nonce`.** Voor de hoofd-OIDC-flow leun ik op authlib's default (state in de sessie, nonce bij scope `openid`). De authlib-bron heb ik niet gelezen.
- **oauth2-proxy `SignInMessage`.** Of het veld als HTML wordt gerenderd (A-15) heb ik niet in de oauth2-proxy-bron gecontroleerd.
- **First-broker-login-flow van projectrealms.** Bepaalt of het pre-registratiescenario in A-10 tot koppeling leidt of tot een weigering. Staat in de realm-blueprint van de Keycloak-manager, niet in de hier gelezen code.
- **Redirect-URI-lijst van de OPI-client in Keycloak.** `redirect_uri` wordt uit de `Host`-header gebouwd (`auth_routes.py:49`). Keycloak hoort een niet-geregistreerde URI te weigeren; of de lijst strak is (alleen `zad.rijksapp.nl` en `ADDITIONAL_DOMAINS`) heb ik niet kunnen zien.
- **Overschrijft de ingress `X-Forwarded-For`?** Volgens het geheugenitem voegt de router het clientadres rechts toe (append). Dan is A-4 een bypass; bij overschrijven is het alleen een onjuiste keuze in de code.
- **NetworkPolicy op poort 5678.** Bepaalt de reikwijdte van A-6 in de `debug`-stand; `overlays/odcn-production/network-policy.yaml` niet gelezen.
- **Volledigheid van de XSS-toets.** `opi/forms/renderer.py` (950+ regels) en de widgetsjablonen zijn niet per veld nagelopen. De conventie (autoescape plus `_summary_text`) is sterk, maar door A-5 is één misser voldoende voor sessieovername; een gerichte fuzz met `<script>` in elk formulierveld van d...
- **Keycloak-tokenlevensduur** voor access tokens met audience `zad-api` (relevant voor A-14) heb ik niet uit de code kunnen halen.

**Deel B**
- B-4 is op functieniveau aangetoond (converter, mutation, normalisatie, `validate_service_configs`), niet over HTTP tegen een draaiende instantie. Een reproductie in de sandbox met een owner-sessie en een handgemaakte services-payload bevestigt of de UI-laag (json-enc.js, processor) de dict ongewijzi...
- B-6: of er daadwerkelijk credentials in argv van kindprocessen staan hangt af van de connectors (git-URL met token, sops/age-argumenten). Niet nagelopen per connector.
- Wat de db-console precies blootstelt (pgweb via port-forward of ingress, met welke credentials) is niet tot in de manifests gevolgd; de rolgrens (B-3) staat los daarvan vast.
- De `restrict-access`-instelling onder de authorization-wall (geheugennotitie: deed niets) is in deze ronde niet opnieuw geverifieerd.
- `GET /api/tasks/{task_id}` geeft `result` en `error_message` terug; of een taaktype daar secrets in zet is niet per taaktype geaudit.
- De `/tools/decrypt`-route werkt met een door de gebruiker meegestuurde sleutel; niet nagegaan of de server-AGE-sleutel ooit als fallback wordt gebruikt.
- Rate-limiter op de subdomein-check gebruikt het meest linkse XFF-adres (`router.py:154-160`) achter `ProxyHeadersMiddleware(trusted_hosts=["*"])` (`server.py:645`); of de ingress overschrijft of append bepaalt of dit te omzeilen is (geheugennotitie zegt: client-IP staat rechts).
- Herkenning op de websocket accepteert een ontbrekende Origin (`logs_websocket_router.py:78`); niet-browserclients hebben dat nodig, maar het verdient een expliciete beslissing.

**Deel C**
- **Capsule-tenant op ODCN.** De `Tenant`-resource staat niet in de repo. Wie zijn de owners (alleen `namespace-manager`?), staan er `additionalRoleBindings`, `networkPolicies`, `limitRanges` of `resourceQuotas` in, en welke SCC's mag de tenant gebruiken? Dat bepaalt of C-6 op ODCN Laag of Midden is e...
- **`pg_hba` van `rig-db`.** CNPG-default is `host all all all scram-sha-256`. Als er een striktere regel is, verandert C-8.
- **Metadata-service.** Serveert ODCN (OpenShift op bare metal of VM's) `169.254.169.254`? Zo ja, dan moet de egress-regel die uitsluiten.
- **Prometheus in `rig-prd-operations`.** Staat er een auth-proxy voor de query-API, of is `:9090` vanuit tenants onbeperkt te bevragen (C-7)?
- **ArgoCD-server-SA.** Welke RBAC heeft `argocd-argocd-server` op ODCN precies (operator-default is cluster-admin-achtig binnen de beheerde namespaces)? Bepaalt de ernst van C-2.
- **HAProxy host-conflicten.** Weigert de `rig`-router een tweede Route/Ingress met een host die al in een andere namespace bestaat? Dat is de laatste schakel in "kan A het ingress-verkeer van B ontvangen".
- **Helm via wizard.** Is er een lopend plan om `helm-charts`/`helmfile` via formulier of API open te zetten (`plans/diensten-ontsluiten-definieren-gebruiken-binden.md` wordt genoemd)? Zo ja, dan is C-2 de poort daarvoor.

**Deel D**
- Zijn de 21 secrets uit D-1 sinds 24 september buiten git om vervangen (Keycloak-admin, Postgres-admin, MinIO-admin, ArgoCD-admin, TransIP, vault-init, `SECRET_KEY`, enz.)? In git is daar geen spoor van, en met GitOps zou dat spoor er moeten zijn. Zolang dit niet met een datum en een lijst is beantwo...
- Welke ClusterRole heeft `argocd-argocd-server` op ODCN, en dekt die `secrets: get` in alle namespaces? Dat bepaalt of D-7 van AANNEMELIJK naar BEVESTIGD gaat. Niet in deze repo; opvragen met `kubectl get clusterrolebinding -o wide | grep argocd-argocd-server` (read-only).
- Wie heeft leesrecht op zad-projects, zad-deployments en zad-argo-user-applications op GitHub (organisatie, teams, externe collaborators)? De docs zeggen er niets over; het bepaalt de ernst van D-2 en D-5.
- Is de PAT uit D-4/D-9 fine-grained en beperkt tot zad-deployments, of een classic token met `repo`-scope op de hele organisatie?
- Is stap 8 van de rotatie van 24 september (`--remove-old-key`, opruimen van `pat_*.txt` en `old_key.txt`, intrekken van de oude token bij GitHub) uitgevoerd? De bestanden in `security/` suggereren van niet, maar dat is geen bewijs.
- Staat er op dit moment een `.cmp-env` met plaintext in de productie-zad-deployments (D-2)? Eén `git ls-files '**/.cmp-env'` in een clone beantwoordt dat; zo ja, dan zijn die waarden als gelekt te beschouwen zolang de historie bestaat.
- Is encryption-at-rest voor etcd op ODCN ingeschakeld? Elk `sops-age-key`-Secret en elk door ArgoCD aangemaakt Secret staat anders plat in etcd-backups.
- Krijgen projectleden op ODCN kubectl-rechten in hun eigen namespace via Capsule (tenant owner)? Dan kunnen zij `sops-age-key` en alle andere Secrets in die namespace lezen. Dat is consistent met wat de UI al toont, maar hoort in het model te staan.

**Deel E**
- Wie heeft schrijfrecht op `zad-projects` buiten OPI? Dat bepaalt of E-1 (git-pad naar cluster-admin via AppProject `default`) door een projectlid of alleen door een platform-admin te bereiken is. Als projectleden via Forgejo kunnen pushen, is E-1 Kritiek.
- Rendert een route het formuliermodel `opi/forms/models/project_file.py` (repositories met `username`/`password`/`branch`)? Ik vond geen import in `opi/web` of `opi/api`; als er wel een pad is, geldt E-1 ook via de UI.
- Welke Pod Security Admission-modus staat op de projectnamespaces? Dat bepaalt of een geïnjecteerde `hostPath` of `hostNetwork` (E-2, E-3, E-8) door de apiserver wordt geweigerd.
- Is de odcn-ingress van OPI ergens anders begrensd op body-size (E-7)? De overlay-annotaties bevatten hem niet; een clusterbrede nginx-default is niet in deze repo te lezen.
- Zet `opentelemetry-instrumentation-asyncpg` de volledige statement op de span (E-16)? Niet in deze repo verifieerbaar; OTEL staat standaard uit.
- Worden `remote-sources` (schema 558-573, geen patterns op `host`/`database`/`bucket`) ooit in de backup-pod-templates gebruikt? Nu niet getraceerd; als dat gebeurt, geldt E-3 ook daar.
- Is `REVOKE CONNECT ... FROM PUBLIC` misschien in de CNPG-bootstrap of een init-script buiten deze repo geregeld (E-18)?

**Deel F**
- **GitHub-toegangsbeheer**: wie heeft push op `rig-cluster-projects`, `argo-applications` en de deployments-repo, en staat er branch protection of required review op `main`? Dit bepaalt of F-2 en F-4 vandaag door een ontwikkelaar exploiteerbaar zijn. Niet toetsbaar vanuit de repo.
- **PAT-scope**: classic met `repo` of fine-grained per repo? Eén token voor alle drie plus de per-project Argo-secrets (F-3)?
- **SA `argocd-argocd-server`**: welke Roles genereert de operator in de `managed-by`-namespaces en in `rig-prd-operations`? Als `get secret` in `rig-prd-operations` erbij zit, is F-1 rechtstreeks een platformsleutel-lek.
- **Live secrets**: is `keycloak-db-credentials` in `rig-prd-operations` werkelijk `keycloak` (F-5)? Heeft `keycloak-admin-credentials` live wel `KEYCLOAK_ADMIN_CLIENT_SECRET` en `KEYCLOAK_OTP_ADMIN_*` (F-20, F-21)? Beide zijn read-only met kubectl te toetsen, buiten deze statische scan gehouden.
- **Master-realm live**: staan `bruteForceProtected`, een wachtwoordbeleid en OTP op `admin` handmatig aan? Hoeveel accounts hebben master-rol `admin`?
- **Groepen**: wie zit in `rig-prd-developer-group` (F-7)?
- **Status F-6**: zijn de stappen 1 t/m 3 van het verwijderplan van `emergency-restore-allow-all` uitgevoerd op een enforcerend cluster?
- **Router**: welke `Forwarded`/`X-Forwarded-*`-headers schrijft en stript de ODCN-router, en welk `tlsSecurityProfile` staat op de `rig`-IngressController (geheugen: clusterbreed instelbaar)? Dit bepaalt F-12 (headers) en de cipher-toets die hier niet uit git te halen was.
- **Tenant-API**: kan een tenant `repositories[].url` via API of upload wijzigen (F-15)?
- **Sandbox-blootstelling**: luistert de dev-server-Caddy op 80/443 vanaf internet (F-16)? De docs zeggen alleen dat de naam naar `127.0.0.1` resolvet.
- **busybox-wget**: valideert busybox 1.37 in het gebruikte image TLS-certificaten (F-24)? Eén meting in de sandbox beslist het.
- **Projecten in prod** met de `algoritmeregister`-template of met `additional-clients` zonder `redirect-uris` (F-23, F-26): te toetsen in de prod projects-repo.

**Deel G**
- Welke retentie en lezerskring heeft de Loki-instantie van ODCN voor de `rig-prd-operations`-logs? Zolang G-1 open staat is dat de facto het audit-spoor, en het ligt buiten deze repo.
- Zet Capsule op ODCN een `ResourceQuota` per tenant (`rig-prd`) of per namespace? Zo ja, dan verschuift G-7 naar "niet in git vastgelegd"; zo nee, dan staat hij zoals beschreven.
- Staat `LOGWATCHER_ENABLED` in productie werkelijk op true en is het ntfy-endpoint ntfy.sh of een eigen server? De sleutels staan in het secret, de waarden zijn hier niet ontsleuteld.
- Wie kan pod-specs in `rig-prd-*` lezen (ODCN-operators, Capsule-tenant-owners)? Dat bepaalt de directe blast-radius van G-2.
- Wordt de `rig-db`- en Keycloak-PVC door ODCN buiten deze repo om gesnapshot? Dat zou G-3 verzachten, maar is dan niet toetsbaar en niet getest.
- Welke rechten heeft het `GRAFANA_TOKEN` in Mimir (tenant-header, alleen lezen, welke datasources)? De code gebruikt één token voor alle namespaces (`opi/connectors/grafana_prometheus.py:107-112`); alleen platformbeheerders bereiken de routes, dus het risico zit in het token zelf.
- Is er bij ODCN of de proceseigenaar een DPIA voor ZAD als platform, los van de DPIA's van de tenant-applicaties? De repo verwijst er nergens naar.
- De eerdere geheugennotitie zei dat de Keycloak-audit-events niet gecommit waren; in `main` staan ze in drie van de vier blueprints. Is dat uitgerold op de productierealms (`eventsEnabled` via de admin API te controleren)?

**Deel H**
- Draait Renovate op de organisatie (H-11)? Alleen een org-beheerder kan dat zien.
- Wat betekent de `-dirty`-suffix op de productie-image concreet: is er lokaal gebouwd en gepusht buiten CI om? `docs/sandbox-image-deploy-via-registry.md` en de recente commit e6c08b22c wijzen op een pad waarbij een ontwikkelaar zelf naar de registry pusht.
- Zijn de Trivy-critical-meldingen op `anyio 4.12.1` en `GitPython 3.1.50` inmiddels in `uv.lock` opgelost (uv.lock niet gelezen)?
- Hoe komt code van GitHub main op de Forgejo die de orchestrator gebruikt (`dclaude sync-main`), en geldt de branch protection daar ook? Dat raakt deel F.

### Bijlage C. Werkwijze en bronnen

- Logboek van de scan met alle stappen, verificaties en beslissingen: `logboek.md`.
- Deelrapporten met volledig bewijs, route- en templatetabellen: `deel-A.md` t/m `deel-H.md`.
- Toetskader: BIO2 v1.3 definitief (9 januari 2026), maatregelteksten geciteerd uit de lokale naslag; NORA BIO-thema-uitwerkingen APO, TBV, CLD en CVZ op algemene kennis (noraonline.nl, ISOR).
- Referentiepraktijk: OWASP ASVS 4.0, OpenSSF Scorecard en SLSA, NSA/CISA Kubernetes Hardening Guide, CIS Kubernetes Benchmark, NCSC-richtlijnen voor webapplicaties en CI/CD, NL GOV OpenID Connect-profiel, ArgoCD security-documentatie.
- Beperkingen: statische toets op `main` (e6c08b22c); geen live test; sleutelbestanden niet geopend; `gh` alleen lezend gebruikt voor repo-instellingen en alerts.
