# Deel F: GitOps-keten en infrastructuurconfiguratie

Datum: 2026-09-26. Toets op branch `main` (e6c08b22c), statisch: alleen code en configuratie in git. Geen kubectl, geen decrypt van SOPS-bestanden, geen wijzigingen. Productie-overlay (`odcn` / `odcn-production`) is leidend; waar sandbox of local zwakker is, staat dat apart. Elke bevinding draagt bewijs als pad:regel met citaat en een zekerheid: BEVESTIGD (staat letterlijk in de code) of AANNEMELIJK (volgt uit de code, maar hangt van iets af dat buiten de repo ligt, zoals RBAC in het cluster of toegangsbeheer op GitHub).

## Onderzocht

- Git-keten: `operations-manager/python/opi/connectors/git.py`, `opi/core/git_monitor.py`, `opi/jobs/reconciliation.py`, `opi/utils/project_utils.py`, `features/git-push-serialization.md`, `features/git-diff-driven-reconciliation.md`, `docs/git-diff-driven-reconciliation.md`, `features/sops-sleutel-roteren.md`.
- ArgoCD: `bootstrap/rig-system/kustomize/overlays/odcn-production/*` (ArgoCD-CR, Applications, netpols, pull-secrets), `bootstrap/rig-system/kustomize/components/sops-plugin/`, `configmap-sops-plugin.yaml`, `sops-plugin.sh`, `images/cmp-kustomize-sops/`, `manifests/argocd-appproject.yaml.jinja`, `argocd-application.yaml.jinja`, `argo-repository*.yaml.jinja`, `opi/connectors/argo.py`, `features/argocd-token-cache.md`, `features/argocd-render-error-surfacing.md`.
- Keycloak: `infrastructure/bootstrap/infrastructure/keycloak/` (realm-config, controller, overlays), `opi/connectors/keycloak.py`, `opi/manager/keycloak_manager.py`, `opi/bootstrap/keycloak_setup.py`, `opi/configs/keycloak/`, de features `keycloak-*.md`, `features/futures/keycloak-sso-bypass-voorkomen.md`, `docs/post-mortems/user-impersonation-oidc-email-claim.md`.
- Forgejo: `infrastructure/bootstrap/infrastructure/forgejo/` (statefulset, ingress, bootstrap-job en de sandboxed-local overlay), `Taskfile.yaml` (sandbox-init), `docs/sandbox-on-dev-server.md`.
- Componenten: `infrastructure/bootstrap/infrastructure/{minio,postgresql,redis,external-dns,prometheus,secrets,mail,registry,chisel,backup-destination,cert-manager}` met de odcn-overlays, `infrastructure/bootstrap/clusters/odcn/kustomization.yaml`, `bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/*`, `bootstrap/rig-system/kustomize/ingress-nginx/`.
- Tenant-templates: `operations-manager/python/manifests/{ingress,namespace,deployment,tenant-baseline-network-policy,network-policy,pod-security-context,project-serviceaccount,sidecar-authorization-wall,db-console-pod,decrypt-sops,issuer-letsencrypt,kustomization}.yaml.jinja` en de voedende code in `opi/handlers/project_file_handler.py`, `opi/manager/project_manager.py`, `opi/core/cluster_config.py`, `opi/connectors/subdomain.py`.
- Configuratiebeheer: `docs/een-nieuw-cluster-installeren.md`, `docs/odcn-todo/*`, `features/handmatig-gezette-resources.md` (gaat over resource-tuning, niet over handmatige clusterresources), `features/publish-on-web-tls-modes.md`, `features/external-domains-letsencrypt.md`, `features/domain-restrictions.md`, `features/caa-records.md`, `features/bio-network-access-no-vpn-compliance.md`, `features/restrictive-network-policies.md`.

Niet gevonden in de checkout: `features/argocd-3x-upgrade.md` (wel in het geheugen genoemd). De ArgoCD-config is getoetst op het operator-CR in `overlays/odcn-production/argocd-deployment.yaml`.

## Keten zoals hij in de code staat

Wie schrijft waar:

1. **Gebruikers** schrijven via de portal of de API van OPI. Zij schrijven nooit rechtstreeks in git. OPI valideert (schema, enforcers, `enforce_namespace_pin`) en commit.
2. **OPI** (`rig-prd-operations`, SA `namespace-manager`) is de enige bedoelde schrijver van de drie repos. In productie staan die op GitHub: `RijksICTGilde/rig-cluster-projects` (zad-projects) en `RijksICTGilde/argo-applications` (`bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/configmap.yaml:12,20`); de deployments-repo komt uit `PROJECT_REPO_URL` in het SOPS-secret. Auth is HTTPS met gebruiker `git` en een AGE-versleutelde PAT (`configmap.yaml:13-14,21-22`). OPI pusht rechtstreeks naar `main` met `--no-verify` (`opi/connectors/git.py:1434,1484`), met een vaste committer zonder handtekening (`git.py:53-54`).
3. **De platform-PAT** reist mee: elk projectbestand krijgt hem in `repositories[0].password` (`opi/utils/project_utils.py:466-474`) en OPI rendert hem per project als ArgoCD repository-Secret met het wachtwoord plat in `stringData` (`manifests/argo-repository-https.yaml.jinja:15-16`).

Wie leest waar:

4. **ArgoCD** (namespaced, in `rig-prd-operations`, RIG-eigen image v3.5.1-rig2) leest drie bron-Applications: `production-infrastructure` (deze repo, pad `infrastructure/bootstrap/clusters/odcn`), `user-applications` (argo-repo, app-of-apps, `project: default`) en `ron-infrastructure`. Alle drie `automated` met `prune: true`, `selfHeal: true`, `allowEmpty: false`.
5. Per tenant-project maakt OPI een **AppProject** met `destinations` gepind op één namespace en `clusterResourceWhitelist: []` (`manifests/argocd-appproject.yaml.jinja:16-20`), en per deployment een **Application** die via de CMP-plugin `kustomize-sops-v1.0` rendert (`argocd-application.yaml.jinja:22-26`).
6. **De CMP-sidecar** draait onder SA `argocd-argocd-server` (`components/sops-plugin/kustomization.yaml:14`), haalt `sops-age-key` uit de destination-namespace met `kubectl get secret` (`configmap-sops-plugin.yaml:44-51`), sourcet een tenant-geschreven `.cmp-env` als bash (`:25-30`) en draait `kustomize build --enable-alpha-plugins --enable-exec --enable-helm` en/of `helmfile template` (`:190,218,301`). Dezelfde sidecar rendert ook `production-infrastructure` met destination `rig-prd-operations`, dus de SA kan de platformsleutel lezen.
7. **De rand**: op ODCN is de OpenShift-router (HAProxy) de ingress; ingress-nginx bestaat alleen voor Kind. OPI, Keycloak en tenant-apps staan open op internet (`ip_whitelist: "0.0.0.0/0,::/0"`), ArgoCD-UI en Prometheus achter een IP-whitelist.

Waar de vertrouwensgrens zit:

- De grens ligt bij OPI's validatie van het projectbestand. Alles wat daar doorheen komt wordt zonder verdere controle op herkomst door ArgoCD uitgerold (geen signed commits, geen branch protection in de repo zichtbaar, geen reviewstap).
- Wie kan schrijven in de argo-repo of de deployments-repo, of kan sturen wat de CMP uitvoert, staat feitelijk op het niveau van de ArgoCD-controller (namespace-admin over alle tenant-namespaces plus `rig-prd-operations`).
- De git-monitor (reageren op vreemde commits in zad-projects) staat in productie uit (`configmap.yaml:11` `ENABLE_GIT_MONITOR=false`), en faalt gesloten waar hij aan staat (`opi/core/git_monitor.py:143,174`).

## Sterke plekken

- **Secrets in git zijn SOPS/AGE** voor vrijwel alles op odcn: 11 secrets via `decrypt-sops.yaml` (`infrastructure/bootstrap/infrastructure/secrets/config/overlays/odcn/decrypt-sops.yaml`), OPI-env via `operations-manager-env-secrets.yaml` (ENC-waarden), PAT's als `base64+age:` in de configmap (`configmap.yaml:14,22`). Provided TLS-certs van tenants worden als SOPS-secret gerenderd (`opi/manager/project_manager.py:6396-6420`).
- **Plaintext-guard voor elke commit**: een `*.to-sops.yaml` in de werkboom breekt de commit af (`git.py:1181-1213`, aangeroepen op `:1230`). Credentials worden uit gelogde git-commando's gestript (`git.py:57-76,383`); de push-lock is credential-vrij (`git.py:39-48`).
- **Push-serialisatie met compare-and-swap**: lock per (repo, branch) (`git.py:1482`) en `allow_rebase=False` vanuit de ProjectStore (`git.py:1503-1510`, `opi/services/project_store.py:794-807`), zodat een tekst-merge nooit een ongevalideerd projectbestand oplevert.
- **Namespace-pin**: `enforce_namespace_pin` weigert een deployment die een andere namespace dan de projectnaam claimt (`opi/manager/project_manager.py:222-242`); AppProject pint destination op één namespace en sluit cluster-scoped resources uit; Applications `CreateNamespace=false` (`argocd-application.yaml.jinja:36`).
- **Cross-tenant bestandslezen in de CMP is dicht**: kustomize draait zonder `--load-restrictor LoadRestrictionsNone` (`configmap-sops-plugin.yaml:301`) en `ARGOCD_REPO_SERVER_PLUGIN_USE_MANIFEST_GENERATE_PATHS=true` streamt alleen de eigen app-map (`overlays/odcn-production/kustomization.yaml:66`).
- **ArgoCD-hardening**: `defaultPolicy: role:none` met expliciete deny's, SSO via dex/OpenShift OAuth, `scopes: [groups]` (`argocd-deployment.yaml:85-105`); route edge-TLS met redirect en één whitelist-IP (`:49-56`); `exec.enabled` nergens aan; CMP-sidecar non-root, caps gedropt, seccomp (`:146-154`); Argo-netpol egress alleen DNS en 443 (`network-policies/argocd-network-policy.yaml:40-60`).
- **Prune-veiligheid**: `allowEmpty: false` op alle drie bron-Applications met uitleg, `Prune=false` op het CNPG-cluster (`postgresql/database/base/cluster.yaml:12`), `PrunePropagationPolicy=foreground` en `PruneLast` op tenant-apps. Auto-orphan-purge bewust niet geautomatiseerd na een near-miss (`opi/jobs/reconciliation.py:397`).
- **Drift-detectie staat aan**: `selfHeal: true` en `prune: true` op infra en tenants (`argocd-application-production-infrastructure.yaml:26-28`, `argocd-application.yaml.jinja:31-33`). Het CNPG-cluster, dat handmatig is aangeraakt, wordt door ArgoCD dus teruggezet.
- **Per-component deny-by-default netpols** op odcn voor MinIO, PostgreSQL en Redis, met alleen DNS als egress (`minio/config/overlays/odcn/network-policies/minio-networkpolicy.yaml:12`, `postgresql/.../postgresql-networkpolicy.yaml:12`, `redis/.../redis-networkpolicy.yaml:12`). MinIO-console (9001) alleen vanuit de ops-namespace (`minio-networkpolicy.yaml:15-23`); geen MinIO-ingress op odcn. OPI-netpol egress beperkt tot 443, 53, backup:9000 en mailrelay (`operations-manager/overlays/odcn-production/network-policy.yaml:17-51`).
- **Pod-hardening overal consistent**: `runAsNonRoot`, `drop: ALL`, `allowPrivilegeEscalation: false`, `seccompProfile: RuntimeDefault` op OPI (`operations-manager/base/deployment.yaml:36-45`), MinIO (`:34-41`), Redis (`:68-75`), Forgejo (`:129-134`), tenant-pods (`deployment.yaml.jinja:114-122`) en de authorization-wall-sidecar (`:58-66`). `automountServiceAccountToken: false` op MinIO en Redis.
- **Tenant-isolatie**: per-deployment baseline-netpol die alleen eigen deployment, ingress-router (pod-specifiek op odcn), ops-namespace en backup-namespace toelaat (`tenant-baseline-network-policy.yaml.jinja:29-83`); Redis-ACL per tenant met key-prefix (`opi/manager/redis_manager.py:493`); eigen ServiceAccount per project zonder RoleBinding in de repo.
- **Ingress-invoer van tenants wordt gefilterd**: `_SAFE_PATH_PATTERN = ^/[A-Za-z0-9/_.~-]*$` op match en rewrite, met als reden "voorkomt nginx-snippet-injection" (`opi/handlers/project_file_handler.py:170-172`); alle andere tenant-waarden gaan door `tojson` (`ingress.yaml.jinja:8,90,95,112`). De annotatieset is vast; een tenant kan geen eigen annotaties toevoegen.
- **TLS-materiaal modern**: ECDSA P-384 met `rotation-policy: Always` (`ingress.yaml.jinja:82-84`), HSTS 1 jaar met preload op HAProxy en nginx (`:28,45-48`), CAA-records op de drie zones (`features/caa-records.md`), ACME-account per namespace-Issuer. Reserved-subdomain-lijst en approval-flow voor subdomeinen (`opi/connectors/subdomain.py:581,913`, `features/domain-restrictions.md`).
- **Keycloak-admin niet meer op het gedeelde wachtwoord**: client-credentials via `KEYCLOAK_ADMIN_CLIENT_SECRET` met uitleg, OTP op het menselijke adminaccount en `KEYCLOAK_ENFORCE_ADMIN_OTP=true` voor de per-project realm-admins (`operations-manager/overlays/odcn-production/patches/deployment.yaml:33-64`, `configmap.yaml:54`).
- **Supply chain**: OPI-image CalVer-gepind via de RCR-proxy met imagePullSecrets (`patches/deployment.yaml:8-12`); MinIO uit eigen build op een bekende source-tag omdat upstream niet meer distribueert (`minio/controller/overlays/odcn/kustomization.yaml:19-28`); external-dns met `--domain-filter` op precies de drie eigen zones en `--txt-prefix` ownership (`external-dns/controller/overlays/odcn/kustomization.yaml:14-27`), ClusterRole alleen `get/watch/list ingresses` (`external-dns/controller/base/clusterrole.yaml:6-8`).
- **Bewuste risicoafweging vastgelegd** voor SSO zonder VPN (`features/bio-network-access-no-vpn-compliance.md`), en de tijdelijke allow-all-netpol draagt een verwijderplan in vier stappen (`emergency-restore-allow-all.yaml:13-19`).

## Bevindingen

### F-1 Tenant-gestuurde code-executie in de CMP-sidecar, met de SA die alle `sops-age-key`-secrets kan lezen

- **Ernst**: Kritiek
- **Zekerheid**: BEVESTIGD voor de codepaden (bash-source, `--enable-exec`, tenant-URL zonder allowlist, stderr aan de gebruiker getoond). AANNEMELIJK voor de reikwijdte van de SA-rechten (operator-gegenereerde Roles staan niet in de repo).
- **BIO2**: 8.04 (toegang tot broncode), 8.24 (cryptografie: sleutelbeheer), 8.20/8.22 (netwerkscheiding), 8.32 (wijzigingsbeheer)
- **Beschrijving**: de CMP voert inhoud uit die een tenant bepaalt, in een proces dat de AGE-sleutel in zijn omgeving heeft en met `kubectl` secrets kan lezen. Drie routes:
  1. `.cmp-env` wordt door OPI ongequoted geschreven uit `env-vars` van een helmfile-deployment en door de CMP als bash gesourced. Het schema eist alleen `string`.
  2. Een helmfile-deployment kloont een door de tenant opgegeven git-URL zonder allowlist en kopieert de inhoud in de projectmap van de deployments-repo; de CMP draait daar `helmfile template` op (gotmpl kent `exec` en `readFile`), en bij een `kustomization.yaml` `kustomize build --enable-alpha-plugins --enable-exec --enable-helm`.
  3. Tenant-`files` kunnen `helmfile.yaml.gotmpl` overschrijven.
  De sidecar draait onder één SA voor alle apps, ook `production-infrastructure` met destination `rig-prd-operations`; de plugin haalt de sleutel per render uit `${ARGOCD_APP_NAMESPACE}`, dus die SA moet de platformsleutel kunnen lezen. Plugin-stderr wordt aan de gebruiker getoond.
- **Bewijs**:
  - `bootstrap/rig-system/kustomize/configmap-sops-plugin.yaml:25-30` `if [ -f ".cmp-env" ]; then ... source .cmp-env`
  - `configmap-sops-plugin.yaml:51` `SOPS_KEY_B64=$(kubectl get secret ${SOPS_KEY_SECRET} -n ${ARGOCD_APP_NAMESPACE} -o jsonpath='{.data.key}')`
  - `configmap-sops-plugin.yaml:301` `kustomize build --enable-alpha-plugins --enable-exec --enable-helm "$folder"`
  - `operations-manager/python/opi/manager/project_manager.py:4951` `cmp_env_vars.append(f"{key}={value}")` en `:4953-4955` schrijft `.cmp-env`
  - `operations-manager/python/opi/schemas/project_v2.json:471-474` `"env-vars": { "type": "object", "additionalProperties": { "type": "string" } }`
  - `project_manager.py:4778-4808` `_clone_helmfile_source` met `source_url = helmfile_def.get("url")`, geen host-check
  - `bootstrap/rig-system/kustomize/components/sops-plugin/kustomization.yaml:14` `serviceaccount: argocd-argocd-server` en `:21-35,56-58` projected SA-token gemount
  - `overlays/odcn-production/argocd-application-production-infrastructure.yaml:24` `namespace: rig-prd-operations` met dezelfde plugin (`:16`)
  - `features/argocd-render-error-surfacing.md:24,59` de conditie draagt de plugin-stderr en wordt in de UI getoond
- **Aanvalsscenario**: een legitieme projectgebruiker maakt een helmfile-deployment met `env-vars: {X: "x; kubectl get secret sops-age-key -n rig-prd-operations -o yaml >&2"}` of met een eigen repo waarin een gotmpl `exec` staat. Bij de eerstvolgende render leest de CMP de platformsleutel en de projectsleutels van andere tenants en lekt ze via stderr (zichtbaar in de portal als render-fout) of via de eigen gerenderde manifests. Met de platformsleutel zijn alle SOPS-secrets in de repos leesbaar, inclusief de PAT (F-3) en alle admin-wachtwoorden.
- **Aanbeveling**: (a) `.cmp-env` niet sourcen maar per regel parsen met een strikte sleutel-regex en `printf %q`-quoting, of de waarden via ArgoCD `plugin.env` meegeven; (b) `--enable-exec` en `--enable-alpha-plugins` uit voor tenant-apps (de plugin doet de decryptie zelf, dus ksops als exec-generator is niet nodig); (c) helmfile voor tenants alleen met een allowlist van bronrepos, of in een aparte sidecar zonder sleutels; (d) een aparte CMP-sidecar met een SA die alleen tenant-namespaces mag lezen, en de platform-apps op een andere sidecar; (e) render-stderr niet ongefilterd aan gebruikers tonen.

### F-2 App-of-apps `user-applications` op `project: default`: schrijfrecht op de argo-repo is cluster-admin binnen het ArgoCD-bereik

- **Ernst**: Kritiek
- **Zekerheid**: BEVESTIGD voor de configuratie; exploitatie AANNEMELIJK, afhankelijk van wie op GitHub kan pushen (niet toetsbaar vanuit de repo).
- **BIO2**: 8.32, 8.04, 8.02 (speciale toegangsrechten)
- **Beschrijving**: de app-of-apps synct elke Application-CR uit de argo-repo. Een CR die daar landt kiest zelf zijn `project`; het `default`-AppProject wordt nergens in de repo ingeperkt, en default staat alle repos, alle destinations en alle cluster-scoped resources toe (binnen wat de namespaced ArgoCD mag). De OPI-gegenereerde AppProjects beschermen alleen CR's die naar dát project verwijzen. De opmerking "OPI is the only writer of this repo" is een aanname, geen afgedwongen eigenschap.
- **Bewijs**:
  - `bootstrap/rig-system/kustomize/overlays/odcn-production/argocd-application-user-applications.yaml:9` `project: default`, `:26` "OPI is the only writer of this repo"
  - `grep -rn "kind: AppProject" bootstrap infrastructure` levert alleen de CRD in de operator-install op; geen beperkt `default`-project
  - `manifests/argocd-appproject.yaml.jinja:14-15` `sourceRepos: - '*'`
- **Aanvalsscenario**: iemand met push op `argo-applications` (of met de PAT, F-3) commit een Application met `project: default`, destination `rig-prd-operations` en een repo naar keuze. ArgoCD rolt het uit met de rechten van de controller; de CMP rendert bovendien met de platformsleutel (F-1).
- **Aanbeveling**: het `default`-project dichtzetten (lege `sourceRepos` en `destinations`) en een eigen, beperkt AppProject voor de app-of-apps; branch protection met required review of signed commits op de argo-repo; OPI-eigen deploy key of fine-grained PAT die alleen die repo kan schrijven.

### F-3 Eén schrijf-PAT voor alle repos, in elk projectbestand en elk ArgoCD-repository-Secret

- **Ernst**: Hoog
- **Zekerheid**: BEVESTIGD voor de verspreiding; scope van de PAT AANNEMELIJK (één token volgens de rotatiedocumentatie).
- **BIO2**: 8.05 (beveiligde authenticatie), 5.17 (authenticatie-informatie), 8.04
- **Beschrijving**: elk nieuw projectbestand krijgt `PROJECT_REPO_USERNAME`/`PROJECT_REPO_PASSWORD`; OPI schrijft die per project als ArgoCD repository-Secret met het wachtwoord plat in `stringData`. ArgoCD hoeft alleen te lezen, maar krijgt zo de schrijf-token.
- **Bewijs**:
  - `operations-manager/python/opi/utils/project_utils.py:466-474` PAT in `repositories[0].password`
  - `manifests/argo-repository-https.yaml.jinja:15-16` `username: {{ username }}` / `password: {{ password }}`
  - `features/sops-sleutel-roteren.md:3,21-28` "de GitHub-PAT", "met het wachtwoord er PLAT in"
  - `operations-manager/python/opi/core/config.py:236-239` code-default met token-literal
- **Aanvalsscenario**: één Secret-lezer in `rig-prd-operations`, of één render-fout die de sleutel lekt (F-1), levert een token met schrijfrecht op zad-projects, argo-applications en de deployments-repo, en daarmee F-2 en F-4.
- **Aanbeveling**: leesrechten voor ArgoCD (deploy key read-only of fine-grained PAT `contents: read` per repo) scheiden van het schrijfrecht van OPI; per repo een eigen credential; de PAT niet in projectbestanden opnemen maar server-side uit settings halen; verlooptijd en eigenaar vastleggen.

### F-4 Schrijfrecht op de deployments-repo geeft controle over andermans namespace, zonder controle op herkomst

- **Ernst**: Hoog
- **Zekerheid**: BEVESTIGD voor het ontwerp; afhankelijk van GitHub-toegangsbeheer (niet toetsbaar).
- **BIO2**: 8.32, 8.04, 8.09
- **Beschrijving**: de map per project is `{cluster}/{project}/{deployment}`; de Application van project A rendert alles in A's map met A's sleutel in A's namespace. Er is geen auteur- of handtekeningcontrole: vaste committer, geen signing, push rechtstreeks naar `main` met `--no-verify`, ArgoCD synct HEAD met `selfHeal` en `prune`. De Forgejo-bootstrap (sandbox) maakt repos aan zonder branch protection; voor GitHub staat niets in de repo.
- **Bewijs**:
  - `operations-manager/python/opi/utils/naming.py:1009-1027` mapindeling
  - `opi/connectors/git.py:53-54` vaste committer `operations-manager@example.com`; `:1434,1484` `--no-verify`
  - `manifests/argocd-application.yaml.jinja:31-33` `prune: true` / `selfHeal: true`
  - `infrastructure/bootstrap/infrastructure/forgejo/config/overlays/sandboxed-local/bootstrap-job-patch.yaml:47-63` repo-aanmaak zonder protection
- **Aanvalsscenario**: een ontwikkelaar met push op de deployments-repo legt een Deployment-manifest in de map van project B; ArgoCD rolt het uit in `rig-prd-b` met B's secrets binnen bereik. Een `kustomization.yaml` die alle YAML in de map opsomt neemt een vreemd bestand mee.
- **Aanbeveling**: geen menselijke schrijvers op de drie repos (alleen de OPI-identiteit); OPI-commits signeren (SSH/GPG-sleutel in het cluster) en ArgoCD `signatureKeys` op de AppProjects; branch protection met required status check.

### F-5 Keycloak-databasewachtwoord staat plain in de productie-overlay en wordt zo door ArgoCD uitgerold

- **Ernst**: Kritiek
- **Zekerheid**: BEVESTIGD voor het bestand, de include-keten en de netpol; AANNEMELIJK dat de waarde in het cluster nog "keycloak" is (de sops-equivalent ontbreekt in `decrypt-sops.yaml`, en het bestand is sinds de eerste commit ongewijzigd).
- **BIO2**: 5.17, 8.05, 8.24, 8.09
- **Beschrijving**: de odcn-overlay van `secrets` bevat een onversleuteld Secret met `username: keycloak` en `password: keycloak`. De `@secret-gen:random:20`-annotatie is bedoeld voor de generator, maar het bestand is nooit vervangen door een `.sops.yaml` en staat als gewone resource in de kustomization. CNPG beheert de rol `keycloak` met precies dit secret en Keycloak logt ermee in. Elke tenant-namespace mag poort 5432 van `rig-db` bereiken. De `secrets/TODO.MD` benoemt het als "THIS NEEDS TO BE FIXED".
- **Bewijs**:
  - `infrastructure/bootstrap/infrastructure/secrets/config/overlays/odcn/keycloak-db-credentials.yaml:12-14` `stringData: username: keycloak / password: keycloak # @secret-gen:random:20`
  - `.../secrets/config/overlays/odcn/kustomization.yaml:7-8` `resources: - keycloak-db-credentials.yaml`
  - `.../secrets/config/overlays/odcn/decrypt-sops.yaml` (11 `.sops.yaml`-bestanden, geen `keycloak-db-credentials`)
  - `infrastructure/bootstrap/clusters/odcn/kustomization.yaml:5` `../../infrastructure/secrets/config/overlays/odcn`
  - `bootstrap/.../odcn-production/argocd-application-production-infrastructure.yaml:13,26-28` pad `clusters/odcn`, `prune`+`selfHeal`
  - `infrastructure/bootstrap/infrastructure/postgresql/database/base/cluster.yaml:68-76` managed role `keycloak`, `passwordSecret: keycloak-db-credentials`
  - `infrastructure/bootstrap/infrastructure/keycloak/controller/base/deployment.yaml:112-122` `KC_DB_URL jdbc:postgresql://rig-db-rw:5432/keycloak` met dat secret
  - `.../postgresql/database/overlays/odcn/network-policies/postgresql-networkpolicy.yaml:22-29` ingress 5432 vanuit `created-by: operations-manager`
  - `git log -- .../odcn/keycloak-db-credentials.yaml`: `278588e29 Initial commit`, daarna alleen de uv-migratie
- **Aanvalsscenario**: een willekeurige tenant-pod verbindt met `rig-db-rw.rig-prd-operations:5432` als `keycloak/keycloak` en heeft de volledige Keycloak-database: alle realms, client-secrets, gebruikers met wachtwoord-hashes, OTP-secrets en sessies. Daarmee is elk project-realm en de platform-realm over te nemen. Ook iedereen met leesrecht op de GitHub-repo kent het wachtwoord.
- **Aanbeveling**: direct roteren (CNPG past een gewijzigd secret toe op de rol), het bestand vervangen door een SOPS-versie via `task generate-infrastructure-secrets-for-cluster`, en de netpol op `rig-db` aanscherpen tot Keycloak-pods voor de Keycloak-database (poort per database kan niet, maar per bron-pod wel). Voeg een CI-guard toe die een `stringData:` zonder `sops:`-metadata onder `overlays/odcn` weigert (het bestaande `scripts/scan-secrets.py` heeft dit niet gevangen).

### F-6 `emergency-restore-allow-all` staat nog in de productie-kustomization

- **Ernst**: Hoog
- **Zekerheid**: BEVESTIGD
- **BIO2**: 8.20, 8.22 (netwerkscheiding), 8.09
- **Beschrijving**: een allow-all NetworkPolicy over alle pods in `rig-prd-operations` staat sinds de storing van 2026-06-10 in de bootstrap-kustomization. NetworkPolicies zijn additief, dus alle deny-by-default policies voor MinIO, Redis, PostgreSQL, ArgoCD en OPI in dezelfde namespace zijn effectief uitgeschakeld. De OPI-netpol zegt dit zelf. Het verwijderplan (per-component policies, testen op een enforcerend cluster) staat erbij, maar de status van stap 1 t/m 3 is niet in git terug te vinden.
- **Bewijs**:
  - `bootstrap/rig-system/kustomize/overlays/odcn-production/kustomization.yaml:20` `- network-policies/emergency-restore-allow-all.yaml`
  - `.../network-policies/emergency-restore-allow-all.yaml:25-32` `podSelector: {}` / `ingress: - {}` / `egress: - {}`
  - `.../operations-manager/overlays/odcn-production/network-policy.yaml:40-42` "door de emergency-restore-allow-all die live over de namespace ligt, en daar mag niets op leunen"
- **Aanvalsscenario**: een tenant-pod (die volgens de baseline naar de hele ops-namespace mag, F-9) bereikt elke poort van elke platform-pod: argocd-server 8080 plain HTTP, Prometheus 9090 zonder auth, MinIO-console 9001, Keycloak 8080 intern, de OPI-probepoort. In combinatie met F-5 is het databasepad sowieso open.
- **Aanbeveling**: de vier stappen uit het bestand afronden en het bestand verwijderen; voor de tussentijd de allow-all beperken tot ingress vanuit de eigen namespace plus egress, zodat de tenant-kant alvast dicht is.

### F-7 `rig-prd-developer-group` is `role:admin` in ArgoCD

- **Ernst**: Hoog als deze groep tenant-ontwikkelaars bevat, anders Midden
- **Zekerheid**: BEVESTIGD voor de configuratie; groepsinhoud onbekend
- **BIO2**: 8.02, 5.15, 8.32
- **Beschrijving**: drie groepen krijgen `role:admin`. Een ArgoCD-admin kan via UI of API een Application in `project: default` aanmaken (F-2), repositories toevoegen en elke tenant-app syncen of verwijderen. De route is IP-whitelisted, maar de API is in-cluster op 8080 vanuit elke pod bereikbaar (netpol-ingress zonder `from`).
- **Bewijs**:
  - `bootstrap/rig-system/kustomize/overlays/odcn-production/argocd-deployment.yaml:96-99` `g, rig-prd-admin-group, role:admin` / `g, rig-prd-developer-group, role:admin`
  - `.../network-policies/argocd-network-policy.yaml:33-39` ingress `ports: 8080, 8443, 8083` zonder `from`
- **Aanbeveling**: developers `role:readonly` of een per-project rol; `server.rbac.log.enforce.enable: "true"`; ingress op 8080 beperken tot de OPI-pod.

### F-8 OPI gebruikt het lokale ArgoCD-adminaccount over plain HTTP

- **Ernst**: Midden
- **Zekerheid**: BEVESTIGD
- **BIO2**: 8.05, 8.24, 8.02
- **Beschrijving**: OPI logt in als `admin` op `http://argocd-server:80`; de server draait `insecure: true`; het admin-Secret bestaat, dus `admin.enabled` staat niet uit. Het 24-uurs JWT wordt procesbreed gecachet.
- **Bewijs**:
  - `operations-manager/overlays/odcn-production/configmap.yaml:59-60` `ARGOCD_HOST=argocd-server` / `ARGOCD_PORT=80`
  - `overlays/odcn-production/argocd-deployment.yaml:48` `insecure: true`; `argocd-admin-secret.yaml.sops.yaml` aanwezig
  - `operations-manager/python/opi/core/config.py:252` account `admin`; `opi/connectors/argo.py:22-33` token-cache
- **Aanbeveling**: een lokaal `opi`-account met project-gescoped rol en API-key, `admin.enabled: "false"`, TLS tussen OPI en argocd-server (of ten minste de netpol dicht, zie F-6/F-7).

### F-9 Tenant-baseline laat egress naar de hele ops-namespace toe, en OPI's eigen ingress-regel heeft geen bron

- **Ernst**: Midden
- **Zekerheid**: BEVESTIGD
- **BIO2**: 8.20, 8.22
- **Beschrijving**: elke tenant-deployment mag naar elke pod en poort in `rig-prd-operations` (bedoeld voor de gedeelde Postgres, Redis, MinIO en Keycloak). De per-component policies van die diensten beperken dat weer tot de juiste poort, maar OPI zelf laat 8000 vanuit overal binnen (nodig voor de router, maar zonder `from`), Prometheus heeft geen netpol en geen auth, en argocd-server staat op 8080 open. Zolang F-6 ligt, telt bovendien geen enkele beperking.
- **Bewijs**:
  - `manifests/tenant-baseline-network-policy.yaml.jinja:106-110` egress `to: namespaceSelector kubernetes.io/metadata.name: {{ ops_namespace }}` zonder poorten
  - `operations-manager/overlays/odcn-production/network-policy.yaml:12-16` `ingress: - from: [] ports: 8000`
  - `infrastructure/bootstrap/infrastructure/prometheus/controller/overlays/odcn/` bevat geen network-policy; `prometheus/controller/base/deployment.yaml` geen auth-flags
- **Aanbeveling**: in de tenant-baseline de ops-egress per dienst en poort uitschrijven (5432, 6379, 9000, Keycloak 8080/8443); OPI-ingress beperken tot de router-namespace en de monitoring-namespace; Prometheus achter een netpol en met auth (of alleen via OPI proxyen).

### F-10 Gedeelde platform-PostgreSQL met superuser-toegang aan, bereikbaar vanuit alle tenant-namespaces, zonder afgedwongen TLS

- **Ernst**: Midden
- **Zekerheid**: BEVESTIGD
- **BIO2**: 8.20, 8.24, 8.03
- **Beschrijving**: Keycloak, de mailrelay, OPI en alle projectdatabases delen `rig-db` met `instances: 1`. `enableSuperuserAccess: true` staat aan (nodig voor OPI's provisioning, maar het superuser-secret ligt daarmee in de namespace). De netpol laat elke tenant-namespace op 5432. Keycloak's JDBC-URL zet geen `sslmode`, dus verbinding volgt de serverdefault (CNPG staat TLS toe maar dwingt het niet af via `pg_hba` in deze config).
- **Bewijs**:
  - `infrastructure/bootstrap/infrastructure/postgresql/database/base/cluster.yaml:14,17` `instances: 1` / `enableSuperuserAccess: true`
  - `postgresql-networkpolicy.yaml:22-29` tenant-ingress op 5432
  - `keycloak/controller/base/deployment.yaml:113` `jdbc:postgresql://rig-db-rw:5432/keycloak` (geen `sslmode`)
- **Aanbeveling**: per-database isolatie op netwerkniveau is in Postgres niet mogelijk, dus: sterke, geroteerde wachtwoorden (F-5), `sslmode=require` in alle clients, `pg_hba` met `hostssl` only, en overweeg de Keycloak-database op een eigen CNPG-cluster zoals projecten dat al kunnen krijgen (`project_infra_namespace`).

### F-11 Ongepinde of dubbelzinnige images in de productieketen

- **Ernst**: Midden
- **Zekerheid**: BEVESTIGD
- **BIO2**: 8.09, 8.19 (installatie van software), 8.32
- **Beschrijving**: de CMP-sidecar (die met de sleutels werkt) draait op `:latest` met `imagePullPolicy: Always` in de component en wordt in de odcn-overlay opnieuw op `:latest` gezet; de sleep-mode-waker op `:latest`; CNPG op de floating major `postgresql:17`; Redis op `redis:7-alpine`; en de OPI-productie-image draagt de suffix `-dirty`, wat betekent dat hij van een niet-gecommitte werkboom is gebouwd en niet herleidbaar is tot één commit.
- **Bewijs**:
  - `bootstrap/rig-system/kustomize/overlays/odcn-production/kustomization.yaml:78` `value: ghcr.io/minbzk/base-images/rig-cmp-argo-kustomize-sops:latest`
  - `components/sops-plugin/kustomization.yaml:38-39` `image: ...:latest` / `imagePullPolicy: Always`
  - `operations-manager/overlays/odcn-production/configmap.yaml:82` `zad-waker:latest`
  - `postgresql/database/overlays/odcn/kustomization.yaml:110` `rcr.rijksapps.nl/ghcr-rig/cloudnative-pg/postgresql:17`
  - `redis/controller/base/deployment.yaml:28,58` `redis:7-alpine` (odcn-overlay pint niet)
  - `operations-manager/overlays/odcn-production/patches/deployment.yaml:12` `operations-manager:2026.09.02.2241-d81cdab4-dirty`
- **Aanbeveling**: digest-pin voor de CMP en de waker, minor-tag voor CNPG en Redis met Renovate, en een build-guard die een `-dirty` tag weigert voor de odcn-overlay (zie `project_calver_image_tags.md`).

### F-12 Security-headers uit de tenant-ingress-template werken alleen op nginx; op ODCN levert het platform alleen HSTS

- **Ernst**: Midden
- **Zekerheid**: BEVESTIGD
- **BIO2**: 8.26 (applicatiebeveiligingseisen), 8.24
- **Beschrijving**: `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy` en `Permissions-Policy` staan als nginx-annotaties en in een nginx `configuration-snippet`. De OpenShift-router negeert die; in productie blijft alleen `haproxy.router.openshift.io/hsts_header` over. In passthrough-modus is ook HSTS weg (de router ziet geen HTTP). De template zegt zelf dat het een baseline is die applicaties moeten aanscherpen, maar op productie is er dus geen baseline.
- **Bewijs**:
  - `manifests/ingress.yaml.jinja:25-29` HSTS voor HAProxy; `:55-71` nginx-only headers en snippet
  - `opi/core/cluster_config.py:224-228` odcn-ingressconfig zonder header-instellingen
- **Aanbeveling**: op ODCN een HAProxy-equivalent (router-annotaties `haproxy.router.openshift.io/set-forwarded-headers` dekken dit niet; overweeg de headers in de authorization-wall-sidecar of een platform-sidecar, of leg in de documentatie vast dat de app ze zelf moet zetten en test dat in de wizard).

### F-13 Prometheus op internet achter alleen een IP-range, zonder auth

- **Ernst**: Midden
- **Zekerheid**: BEVESTIGD
- **BIO2**: 8.05, 8.20, 8.15 (logging)
- **Beschrijving**: de Prometheus-UI en -API staan op een publieke hostnaam met `ip_whitelist: 147.181.0.0/16` (het hele ODCN-VPN-bereik) en zonder authenticatie; TLS via de cluster-wildcard (`tls: - {}`). Iedereen in dat bereik ziet alle metrics van alle tenants (hostnames, poorten, foutpercentages, resourcegebruik) en kan de Prometheus-API bevragen.
- **Bewijs**: `infrastructure/bootstrap/infrastructure/prometheus/controller/overlays/odcn/ingress.yaml:6` `ip_whitelist: "147.181.0.0/16"`, `:20-21` `tls: - {}`; geen auth-configuratie in `prometheus/controller/base/deployment.yaml`.
- **Aanbeveling**: oauth2-proxy (de authorization-wall-sidecar bestaat al) met Keycloak-groep voor het platformteam, of de ingress weghalen en Prometheus alleen via OPI ontsluiten (`PROMETHEUS_URL` is al intern).

### F-14 Tenant-egress naar internet staat open, ook op de namespace-annotatie

- **Ernst**: Laag (bewuste keuze, gedocumenteerd als uitgesteld)
- **Zekerheid**: BEVESTIGD
- **BIO2**: 8.20, 8.22
- **Beschrijving**: elke tenant-namespace krijgt `egressGatewayPolicy: internet` en de baseline-netpol laat 80 en 443 naar `0.0.0.0/0` en `::/0` toe. Data-exfiltratie of C2 vanuit een gecompromitteerde tenant-pod is daarmee vrij. `features/restrictive-network-policies.md` heeft status "Planned".
- **Bewijs**: `manifests/namespace.yaml.jinja:10` `egress.projectcalico.org/egressGatewayPolicy: "internet"`; `tenant-baseline-network-policy.yaml.jinja:131-140` `cidr: 0.0.0.0/0` / `cidr: ::/0` op 443 en 80.
- **Aanbeveling**: een opt-in "geen internet" per deployment in de wizard, en op termijn een egress-allowlist per project (FQDN-policy via Calico is op ODCN beschikbaar).

### F-15 Repository-URL uit het projectbestand stuurt waar OPI met de platform-PAT naartoe kloont

- **Ernst**: Midden
- **Zekerheid**: AANNEMELIJK
- **BIO2**: 8.05, 5.17
- **Beschrijving**: `_construct_repo_url_with_credentials` zet `username:password@` op elke `https://`-host zonder allowlist; het schema staat elke git-URL toe. De wizard maakt `url` readonly bij bewerken, maar dat is UI-niveau.
- **Bewijs**: `opi/connectors/git.py:297-326`; `opi/schemas/project_v2.json:95`; `opi/forms/models/project_file.py:403-407` (`readonly_on_edit=True`), `:414-424` (username/password wel bewerkbaar).
- **Aanvalsscenario**: als `repositories[0].url` via API of upload op een eigen host te zetten is terwijl `password` de platform-PAT blijft, ontvangt die host de PAT als basic-auth.
- **Aanbeveling**: URL-wijziging server-side weigeren; bij een niet-platform-host de platform-PAT nooit meesturen.

### F-16 Sandbox is wezenlijk zwakker en staat op een publiek resolvende naam

- **Ernst**: Midden voor de sandbox, Info voor productie
- **Zekerheid**: BEVESTIGD
- **BIO2**: 8.31 (scheiding omgevingen), 8.05, 8.04
- **Beschrijving**: Forgejo op de sandbox draait met site-admin `rig-admin/admin1234` (in git, in de Taskfile en in de docs), de vier repos zijn `private: false` en `REQUIRE_SIGNIN_VIEW=false`, dus zad-projects met alle (versleutelde) secrets en de argo-repo zijn anoniem leesbaar. Webhooks `ALLOWED_HOST_LIST=*` met `SKIP_TLS_VERIFY=true`, DB `SSL_MODE=disable`. De sandbox-registry heeft een bcrypt-htpasswd in git; chisel draait `:latest`. `*.sandbox.rijksapp.dev` resolvet naar `127.0.0.1` en de dev-server zet Caddy ervoor; of Caddy's poorten 80/443 vanaf internet bereikbaar zijn, staat niet in de docs. ingress-nginx op Kind heeft `allow-snippet-annotations: "true"` met `annotations-risk-level: Critical` (de tenant-invoer is gefilterd, maar een platformfout in de template wordt zo direct RCE in de controller).
- **Bewijs**:
  - `infrastructure/bootstrap/infrastructure/forgejo/config/overlays/sandboxed-local/bootstrap-job-patch.yaml:26-27` `ADMIN_USER="rig-admin"` / `ADMIN_PASSWORD="admin1234"`, `:55` `"private":false`
  - `forgejo/controller/overlays/sandboxed-local/statefulset-patch.yaml:57-58,65-68,51-52`
  - `Taskfile.yaml:3072,3134,3227` `rig-admin:admin1234@forgejo.sandbox.rijksapp.dev`
  - `docs/sandbox-on-dev-server.md:77,86-89,189`
  - `infrastructure/bootstrap/infrastructure/registry/controller/overlays/sandboxed-local/htpasswd-secret.yaml:8`
  - `bootstrap/rig-system/kustomize/ingress-nginx/base/kustomization.yaml:28-29,62`
  - `operations-manager/overlays/sandboxed-local/configmap.yaml:21-22` OPI als site-admin
- **Aanbeveling**: repos privé, een bot-user met repo-scoped token, een gegenereerd wachtwoord uit `_generate-secrets-shared`, en vastleggen dat de dev-server-Caddy niet publiek luistert (of een allowlist).

### F-17 `StrictHostKeyChecking=no` op het SSH-pad en credentials in debug-logregels en argv

- **Ernst**: Laag (productie gebruikt HTTPS en DEBUG staat uit)
- **Zekerheid**: BEVESTIGD
- **BIO2**: 8.24, 8.15
- **Bewijs**: `opi/connectors/git.py:405` `ssh_cmd += " -o StrictHostKeyChecking=no"` (vastgelegd in `tests/test_git_ssh.py:42`); `git.py:509,1950` `logger.debug(f"... {self.repo_url_with_path}")` met `user:PAT@` (`:192-194`), buiten de obfuscator van `:57`; de PAT staat in de argv van `git clone`/`ls-remote` (`:509-512,643-650`).
- **Aanbeveling**: `accept-new` met beheerd known_hosts of SSH niet ondersteunen; overal `_obfuscate_git_command()`; credentials via `GIT_ASKPASS` of een credential helper in plaats van in de URL.

### F-18 Configuratiebeheer (8.09): niet alles staat onder ArgoCD, en de RBAC-laag staat buiten de repo

- **Ernst**: Laag
- **Zekerheid**: BEVESTIGD
- **BIO2**: 8.09, 8.32
- **Beschrijving**: bewust buiten ArgoCD staan (a) de backup-MinIO (`task bootstrap-backup-destination`, "Managed outside ArgoCD by design (prune-safe for the backup PVC)"), (b) de namespace `rig-prd-ron` met zijn labels en het `sops-age-key`-secret daarin (kale `kubectl apply` in de bootstrap-task), (c) de bootstrap zelf (`task bootstrap-argo-system` doet `kustomize build | kubectl apply`), en (d) de effectieve RBAC van OPI: op odcn komt die van Capsule (admin-RoleBinding per tenant-namespace) en van ODCN-aanvragen (`docs/odcn-todo/rbac-deployment-monitoring.md`), niet uit deze repo; de `_blueprint-cluster-role.yaml` is documentatie. Ook de router-instellingen (tlsSecurityProfile, HTTP/2) zijn ODCN-beheer. Drift op deze onderdelen wordt niet gedetecteerd.
- **Bewijs**: `Taskfile.yaml:699-720`; `bootstrap/rig-system/kustomize/overlays/odcn-production/kustomization.yaml:10-13` en `Taskfile.yaml:664-666`; `Taskfile.yaml:653`; `operations-manager/overlays/odcn-production/_blueprint-cluster-role.yaml:1-6` "Op odcn-production NIET gedeployd: Capsule maakt per tenant-namespace automatisch een RoleBinding naar de built-in admin ClusterRole".
- **Aanbeveling**: een runbook-sectie "wat staat buiten ArgoCD en hoe controleer je het" (deels al in `docs/een-nieuw-cluster-installeren.md`), en een periodieke read-only vergelijking (bijvoorbeeld een OPI-taak die `kubectl get rolebinding -A` tegen een verwachting legt) voor de Capsule-RBAC.

### F-19 Info: overige observaties

- `scripts`/tracing: `opi/core/tracing.py:61` OTLP-exporter `insecure=True`; `prometheus/controller/base/configmap.yaml:23,38` `insecure_skip_verify: true`; `opi/connectors/skopeo.py:225` `--dest-tls-verify=false` alleen bij `REGISTRY_VERIFY_TLS=False` (prod `True`, `configmap.yaml:76`); `opi/connectors/argo.py:46` `verify_ssl=False` als default (prod praat http).
- `bootstrap/rig-system/kustomize/sops-plugin.sh` is een verouderde variant zonder helm die nergens gerefereerd wordt; opruimen voorkomt dat iemand hem voor de actuele plugin aanziet.
- `bootstrap/rig-system/kustomize/backup-destination/base/secret.yaml:13-14` bevat plaintext local-credentials (`backup-secret-key-local`), gemarkeerd als template; de odcn-overlay heeft een `secret.sops.yaml`.
- ArgoCD-UI is met `ip_whitelist: 147.181.15.230` tot één adres beperkt (`argocd-deployment.yaml:52`), OPI en Keycloak staan bewust publiek (`operations-manager/overlays/odcn-production/ingress.yaml:7-8` toont de uitgecommentarieerde VPN-regel; `keycloak/controller/overlays/odcn/ingress-legacy.yaml:10` "publiek: OIDC-flow voor end-users"). De afweging staat in `features/bio-network-access-no-vpn-compliance.md`.
- De TransIP-API-sleutel (zone-beheer voor de drie eigen domeinen) staat in twee pods: external-dns en OPI (`patches/deployment.yaml:107-118`). Wie OPI compromitteert kan DNS van `rijksapp.nl` herschrijven. Geen bevinding op zich (OPI zet de CAA-records), maar het vergroot de impact van F-1/F-6.

### F-20 Keycloak-adminconsole en master-realm staan op internet; master zonder brute-force-bescherming, `admin` zonder OTP in productie

- **Ernst**: Hoog
- **Zekerheid**: publieke bereikbaarheid BEVESTIGD; master-realm-instellingen AANNEMELIJK (niets in de code raakt ze, Keycloak-default is uit)
- **BIO2**: 8.05, 8.02, 8.20, 5.17
- **Beschrijving**: de Keycloak-Ingress publiceert `path: /` op `keycloak.rijksapp.nl` (plus de legacy-hostnaam) zonder IP-beperking, dus `/admin/` en `/realms/master/` zijn vanaf internet bereikbaar. Er is geen `KC_HOSTNAME_ADMIN`. Geen enkele plek in OPI of de overlays zet `bruteForceProtected`, een `passwordPolicy` of `otpPolicy` op `master`. Het productie-adminsecret bevat alleen `KEYCLOAK_ADMIN` en `KEYCLOAK_ADMIN_PASSWORD`, niet de `KEYCLOAK_OTP_ADMIN_*`-sleutels die het OTP-adminpad voeden (sandbox heeft ze wel). De eigen feature-doc adviseert al om `/admin` af te schermen.
- **Bewijs**:
  - `infrastructure/bootstrap/infrastructure/keycloak/controller/base/ingress.yaml:20-21` `path: / pathType: Prefix`
  - `.../keycloak/controller/overlays/odcn/kustomization.yaml:16-21` host `keycloak.rijksapp.nl`, `ip_whitelist: "0.0.0.0/0,::/0" # publiek: OIDC-flow voor end-users`; `.../overlays/odcn/ingress-legacy.yaml:10,13`
  - `grep -rn KC_HOSTNAME_ADMIN infrastructure/` leeg
  - `.../secrets/config/overlays/odcn/keycloak-admin-secret.yaml.sops.yaml:8,10` alleen `KEYCLOAK_ADMIN` en `KEYCLOAK_ADMIN_PASSWORD`; `.../sandboxed-local/keycloak-admin-secret.yaml.sops.yaml:31-35` wel `KEYCLOAK_OTP_ADMIN_*`
  - `features/opi-keycloak-service-account.md:104` "Restrict the /admin console at the ingress (VPN/allowlist)"
  - Voorspelbare adminnamen: `opi/utils/naming.py:1300-1305` `{project}_{cluster}_admin`
- **Aanvalsscenario**: password-spraying op `admin` en op de voorspelbare realm-adminnamen vanaf internet; op master geen lockout. De project-admins hebben in prod TOTP (`KEYCLOAK_ENFORCE_ADMIN_OTP=true`), `admin` zelf niet.
- **Aanbeveling**: aparte admin-hostname met `KC_HOSTNAME_ADMIN` en IP-allowlist (of een router-regel op `/admin`), master-realm `bruteForceProtected` plus wachtwoordbeleid via OPI's boot-setup, het prod-secret regenereren zodat het OTP-adminpad bestaat.

### F-21 OPI in productie draait aannemelijk nog op het gedeelde Keycloak-adminwachtwoord

- **Ernst**: Hoog
- **Zekerheid**: AANNEMELIJK (git-staat; het live secret is niet gelezen)
- **BIO2**: 8.05, 8.02, 5.17
- **Beschrijving**: de connector-factory kiest client-credentials alleen als `KEYCLOAK_ADMIN_CLIENT_SECRET` gezet is; de env is `optional: true` en het odcn-secret in git mist die sleutel. Het adminwachtwoord staat wel in OPI's env-secret. Zolang dat zo is kan `admin` geen OTP krijgen zonder OPI buiten te sluiten, en houdt elke env-dump of geheugenlek van OPI het super-adminwachtwoord op dat ook op het publieke `/admin/` werkt (F-20). Circa 15 call sites geven het adminwachtwoord nog expliciet mee.
- **Bewijs**:
  - `operations-manager/python/opi/connectors/keycloak.py:4206-4220` factory-keuze
  - `bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/patches/deployment.yaml:41-46` `KEYCLOAK_ADMIN_CLIENT_SECRET ... optional: true`
  - `.../odcn/keycloak-admin-secret.yaml.sops.yaml:8,10` (twee sleutels); `.../odcn-production/operations-manager-env-secrets.yaml:17` `KEYCLOAK_ADMIN_PASSWORD: ENC[...]`
  - `opi/core/config.py:301` default `"changeMe123!"`; call sites o.a. `keycloak_manager.py:395-396,985-986,1059-1060`, `delete_project_manager.py:333-334`, `jobs/service_orphan_sweep.py:187-188`, `jobs/reconciliation.py:579-580`
- **Aanbeveling**: prod-secret regenereren met `KEYCLOAK_ADMIN_CLIENT_SECRET`, na convergentie `KEYCLOAK_ADMIN_PASSWORD` uit OPI's env halen en de expliciete argumenten uit de call sites verwijderen.

### F-22 Lokale invite laat de gebruiker zijn eigen e-mailadres kiezen; met account-linking op e-mail is dat een impersonatie- en blokkeerpad

- **Ernst**: Hoog
- **Zekerheid**: AANNEMELIJK (code gelezen, niet uitgevoerd)
- **BIO2**: 8.05, 5.16 (identiteitsbeheer), 8.26
- **Beschrijving**: op een `sso-support`-realm registreert een genodigde met een vrij gekozen adres, alleen beperkt door een optionele domeinrestrictie. Kiest hij het adres van een collega die nog nooit via SSO inlogde, dan botst die collega bij eerste SSO-login op "account bestaat al" en wordt gekoppeld aan het account van de aanvaller (bij `account-link: automatic` stil). Daarna heeft de aanvaller een wachtwoord op een account met het geverifieerde adres van het slachtoffer. De post-mortem-maatregelen (`_lock_identity_fields`, `email_verified`-check) dekken de IdP-kant, niet een lokaal gekozen adres; `features/keycloak-auto-link.md:83-87` erkent dat linking op e-mail alleen veilig is als de IdP het adres autoritatief levert.
- **Bewijs**: `operations-manager/python/opi/manager/invite_manager.py:387` `email = form_data.get("email", "").strip()`, `:404` alleen domeincheck, `:89-92` `restrict_domain` optioneel, `:417-425` alleen weigering als het adres al bestaat; `opi/connectors/keycloak.py:3639-3652` `create_user` zet `VERIFY_EMAIL` maar bindt niets aan de invite; auto-link-flow `keycloak.py:2868-2900`; login-formulier zichtbaar `opi/configs/keycloak/sso-support.yaml:113,191`.
- **Aanbeveling**: invite per adres uitgeven (adres in de invite, formulier read-only), of lokale accounts niet als link-doel toestaan; `idp-auto-link` alleen op realms zonder lokale registratie.

### F-23 Extra clients zonder `redirect-uris` krijgen `redirectUris: ["*"]` en `webOrigins: ["*"]`; alle deployment-clients hebben ROPC en service-accounts aan

- **Ernst**: Midden
- **Zekerheid**: BEVESTIGD
- **BIO2**: 8.05, 8.26
- **Bewijs**: `operations-manager/python/opi/manager/keycloak_manager.py:2169` `redirect_uris = client_config.get("redirect-uris", ["*"])`; `opi/connectors/keycloak.py:560-565` `"redirectUris": redirect_uris or ["*"]`, `"webOrigins": web_origins or ["*"]`, `"directAccessGrantsEnabled": True`, `"serviceAccountsEnabled": True`; deployment-client `:736-739` idem; `features/keycloak-additional-clients.md:46` documenteert `*` als default; `fullScopeAllowed` wordt nergens op false gezet; open punt al in `features/futures/keycloak-sso-bypass-voorkomen.md:195`.
- **Aanvalsscenario**: `redirect_uri=https://attacker/` op een `*`-client geeft de authorization code weg; ROPC maakt wachtwoord-brute-force zonder browser mogelijk tegen elke deployment-client.
- **Aanbeveling**: `redirect-uris` verplicht en nooit `*`; `directAccessGrantsEnabled`, `serviceAccountsEnabled` en `fullScopeAllowed` standaard uit.

### F-24 Custom Keycloak-jars komen bij elke podstart ongeverifieerd van GitHub

- **Ernst**: Midden (supply chain)
- **Zekerheid**: geen checksum BEVESTIGD; ontbrekende TLS-validatie in busybox-wget AANNEMELIJK
- **BIO2**: 8.19, 8.09, 8.32
- **Bewijs**: `infrastructure/bootstrap/infrastructure/keycloak/controller/base/deployment.yaml:42-45` `wget https://github.com/MinBZK/keycloak-theme/releases/download/v1.4.2/keycloak-nl-design-system.jar` en `.../RijksICTGilde/RIG-Cluster/releases/download/v1.1.0/keycloak-saml-nameid-mapper-1.1.0.jar` naar `/opt/keycloak/providers/`, `:47` `image: busybox:1.37.0`; geen sha256 in de repo; de mapper-jar bevat een authenticator, IdP-mapper, logout-endpoint en metrics-endpoint (`keycloak-migration/custom-mapper/README.md:5-8`). Contrast: de relay-jar staat in git via `configMapGenerator` en wordt door CI herbouwd en vergeleken (`controller/base/kustomization.yaml:21-24`, `Taskfile.yaml:1804-1829`).
- **Aanbeveling**: hetzelfde patroon als de relay-jar, of een eigen image met de jars gebakken en op digest gepind; minimaal sha256-controle na download.

### F-25 Custom `rig-metrics`-endpoint zonder authenticatie op de publieke poort

- **Ernst**: Midden
- **Zekerheid**: BEVESTIGD in broncode; live niet getoetst
- **BIO2**: 8.05, 8.15
- **Bewijs**: `keycloak-migration/custom-mapper/src/main/java/nl/minbzk/rig/keycloak/metrics/MetricsResource.java:29-35` `@GET ... PrometheusExporter.instance().export(session)` zonder auth-check; `features/keycloak-rig-metrics.md:17,20` "GET /realms/master/rig-metrics ... returns metrics for all realms"; Ingress `path: /` (F-20). `features/metrics-endpoint-security.md:17` claimt "Secured by default", maar dat gaat over poort 9000.
- **Aanbeveling**: bearer-token of alleen op de management-interface (staat als TODO in de feature-doc `:194`), plus router-blokkade op `/realms/*/rig-metrics`.

### F-26 `algoritmeregister`-blueprint maakt demo-gebruikers met wachtwoord `demo123` en rol `admin`, en een publieke client met ROPC

- **Ernst**: Midden (Hoog als een productieproject deze template kiest)
- **Zekerheid**: BEVESTIGD
- **BIO2**: 8.05, 5.17
- **Bewijs**: `operations-manager/python/opi/configs/keycloak/algoritmeregister.yaml:32,41,50,59` `password: "demo123"`, `:27-59` rollen incl. `admin`, `:159` `publicClient: true`, `:168` `directAccessGrantsEnabled: true`, `:195-235` service-client met `manage-users`, `manage-realm`; template kiesbaar in projectbestanden (`features/keycloak-redirect-uri-configuration.md:29`).
- **Aanbeveling**: demo-gebruikers alleen op `local`/`sandboxed-local` of achter een expliciete variabele; ROPC uit op de publieke client.

### F-27 Platformrealm `rig-platform` heeft een confidential client met `http://localhost:*/*`-redirects en mist de zelfbedienings-restricties

- **Ernst**: Midden
- **Zekerheid**: BEVESTIGD
- **BIO2**: 8.05, 8.26
- **Bewijs**: `operations-manager/python/opi/configs/keycloak/bootstrap.yaml:252-269` client `development-clusters` met `"http://keycloak.kind/*"`, `"https://localhost:*/*"`, `"http://localhost:*/*"`; het secret staat op elke dev-machine en in de sandbox (`operations-manager/overlays/sandboxed-local/patches/deployment.yaml:85-89`, `opi/core/config.py:337-338` default `dummy-client-secret-123`); `bootstrap.yaml` bevat geen `disabledRequiredActions`/`removeFromDefaultRoles` (open punt `features/futures/keycloak-sso-bypass-voorkomen.md:194`).
- **Aanvalsscenario**: wie het breed verspreide secret heeft en een slachtoffer naar `keycloak.rijksapp.nl` met `redirect_uri=http://localhost:<port>/` lokt, ontvangt via een luisteraar op de machine van het slachtoffer tokens van de productie-platformrealm met SSO-Rijk-claims.
- **Aanbeveling**: aparte client per omgeving met alleen `https`-redirects naar de sandbox-hostnaam, secret roteren, dezelfde restricties als op de projectrealms.

### F-28 Keycloak 25.0.6 is sinds oktober 2024 zonder security-fixes; image op tag

- **Ernst**: Midden
- **Zekerheid**: BEVESTIGD
- **BIO2**: 8.08 (technische kwetsbaarheden), 8.19
- **Bewijs**: `infrastructure/bootstrap/infrastructure/keycloak/controller/base/deployment.yaml:67` `image: quay.io/keycloak/keycloak:25.0.6`; `features/keycloak-26-upgrade.md:3-9` "25.0 has been out of support since October 2024".
- **Aanbeveling**: upgrade volgens het bestaande plan (geheugen: nog niet uitvoeren, wel plannen); digest-pinning.

### F-29 Laag en Info rond Keycloak

- **Proxy-headers dubbel en tegenstrijdig** (Laag, BEVESTIGD): `deployment.yaml:82` `--proxy-headers=forwarded` naast `:94-95` `KC_PROXY_HEADERS: "xforwarded"` en `:108-109` het vervallen `KC_PROXY_ADDRESS_FORWARDING`. CLI wint, dus Keycloak leest `Forwarded`. De router appendt en het client-IP staat rechts (geheugen); wie het eerste `for=` neemt laat de client zijn IP kiezen (brute-force per IP, `ipAddress` in audit-events). Eén instelling kiezen die bij de ODCN-router past.
- **Stored tokens en `read-token`-rol op elke OIDC-IdP** (Laag, BEVESTIGD): `opi/connectors/keycloak.py:1203-1204` `"storeToken": True, "addReadTokenRoleOnCreate": True`; niets in ZAD gebruikt `/broker/rig-platform-oidc/token`. Uitzetten.
- **Dode configuratie die anders leest dan wat draait** (Info): `infrastructure/bootstrap/infrastructure/keycloak/config/base/main-realm.yaml` wordt nergens gemount of geïmporteerd (ArgoCD-client met `argocd.example.com` en ROPC aan); duplicaat `.../overlays/odcn/keycloak-admin-secret.yaml.yaml.sops.yaml` staat niet in `decrypt-sops.yaml`; `bootstrap.yaml:23` `bruteForceProtected` wordt niet gelezen (alleen de hardcoded seed in `keycloak.py:205-221` geldt); drempels (`failureFactor`, `waitIncrementSeconds`, `permanentLockout`), `passwordPolicy`, `otpPolicy`, `accessTokenLifespan`, `defaultSignatureAlgorithm` en security-headers worden nergens gezet, dus Keycloak-defaults gelden (30 fouten, RS256, 5 min access token); `operations-manager/python/keycloak_debug.py:33` `verify_ssl: bool = False` (debugscript); SMTP naar de relay zonder STARTTLS met vast account, bewust (`deployment.yaml:155-163`).
- **Wat goed is**: `verify=True` op de admin-API (`keycloak.py:113,125`); SAML naar SSO-Rijk met gepind certificaat (geldig tot 2028-11-08), `validateSignature`, `wantAssertionsSigned`, `forceAuthn` (`bootstrap.yaml:55,63,74-75`); post-mortem-maatregelen in code en in de prod-image-commit (`keycloak.py:312-337` `_lock_identity_fields`, `opi/api/auth_routes.py:119-124` en `opi/api/user_token_auth.py:212-213` `email_verified`); zelfbediening dicht op projectrealms (`sso-support.yaml:103-106`, `keycloak_yaml_handler.py:490-520`); realm-seed gesloten (`registrationAllowed`, `resetPasswordAllowed`, `duplicateEmailsAllowed` False, `bruteForceProtected` True); audit-events 90 dagen incl. admin-events (`bootstrap.yaml:34-37`, `sso-support.yaml:92-95`); client-secrets 32 tekens en realm-adminwachtwoorden AGE-versleuteld (`keycloak.py:651-658`, `keycloak_manager.py:1888-1894`); PKCE `S256` op de publieke clients (`sso-support.yaml:198-215`); management-poort 9000 in odcn niet ontsloten; `automountServiceAccountToken: false` en volledige pod-hardening (`deployment.yaml:20-24,175-198`).

## Tabel: componenten

| Component | Productie-overlay pad | Bereikbaarheid | Auth | TLS intern | Versie gepind | Oordeel |
|---|---|---|---|---|---|---|
| OPI (operations-manager) | `bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/` | Publiek: `zad.rijksapp.nl`, `router.rijksapp.nl`, legacy-host; `ip_whitelist 0.0.0.0/0` | Keycloak-OIDC (rig-platform), API-key per project, `ALLOWED_EMAILS`/`ADMIN_EMAILS` | HTTP 8000 achter router; naar Keycloak `https` met verify; naar ArgoCD plain HTTP | CalVer via RCR, maar `-dirty` (F-11) | Goed met kanttekening: RBAC via Capsule buiten repo, netpol ingress zonder bron |
| ArgoCD (operator-CR) | `bootstrap/rig-system/kustomize/overlays/odcn-production/argocd-deployment.yaml` | UI/route: 1 whitelist-IP, edge-TLS redirect; API in-cluster 8080 vanuit elke pod | dex/OpenShift OAuth, `role:none` default; lokaal `admin` aan en door OPI gebruikt | `insecure: true` (plain 8080) | `v3.5.1-rig2` via RCR; operator `v0.14.0` | Matig: developer-group admin (F-7), default-project open (F-2), admin-account (F-8) |
| CMP-sidecar kustomize-sops | `components/sops-plugin/`, `configmap-sops-plugin.yaml`, patch in `overlays/odcn-production/kustomization.yaml:74-79` | Alleen in repo-server-pod | SA `argocd-argocd-server`, leest `sops-age-key` per destination-ns | n.v.t. | `:latest`, `imagePullPolicy: Always` | Onvoldoende: tenant-gestuurde executie met sleutels (F-1), ongepind (F-11) |
| Keycloak | `infrastructure/bootstrap/infrastructure/keycloak/controller/overlays/odcn/` | Publiek `keycloak.rijksapp.nl` + legacy, `path: /` incl. `/admin` | Realm-login; master zonder BF/OTP; OPI aannemelijk op adminwachtwoord | HTTP 8080 achter router; JDBC zonder `sslmode` | Tag `25.0.6` (EOL) | Onvoldoende (F-5, F-20, F-21, F-24, F-28) |
| PostgreSQL (CNPG `rig-db`) | `infrastructure/bootstrap/infrastructure/postgresql/database/overlays/odcn/` | ClusterIP; 5432 vanuit ops-, alle tenant- en RON-namespaces | Per-rol wachtwoorden uit secrets; superuser aan; Keycloak-rol met plain `keycloak` | CNPG-TLS beschikbaar, niet afgedwongen | `postgresql:17` floating major via RCR | Kritiek door F-5; verder Matig (F-10); `Prune=false` goed |
| MinIO (platform) | `infrastructure/bootstrap/infrastructure/minio/controller/overlays/odcn/` + `config/overlays/odcn/` | ClusterIP; 9000 vanuit ops en tenants, console 9001 alleen ops; geen ingress | Root-creds SOPS | HTTP intern | Eigen build op source-tag `RELEASE.2025-07-23T15-54-02Z` via RCR | Goed |
| MinIO backup-destination | `infrastructure/bootstrap/infrastructure/backup-destination/controller/overlays/odcn/` (buiten ArgoCD, `task bootstrap-backup-destination`) | ClusterIP in `rig-prd-backup`; 9000 vanuit OPI | SOPS `secret.sops.yaml` | HTTP intern | zelfde eigen build | Goed; buiten drift-detectie (F-18) |
| Redis | `infrastructure/bootstrap/infrastructure/redis/controller/overlays/odcn/` | ClusterIP; 6379 vanuit ops en tenants | ACL-file, per-tenant users met prefix | Geen TLS | `redis:7-alpine` (F-11) | Goed, tag pinnen |
| Prometheus | `infrastructure/bootstrap/infrastructure/prometheus/controller/overlays/odcn/` | Publieke hostnaam met `ip_whitelist 147.181.0.0/16`; in-cluster 9090 open | Geen | HTTP; scrape `insecure_skip_verify` | `prom/prometheus:v2.54.1` | Onvoldoende (F-13) |
| external-dns | `infrastructure/bootstrap/infrastructure/external-dns/controller/overlays/odcn/` | Geen inkomend | TransIP-sleutel SOPS; ook in OPI | n.v.t. | `v0.15.0` via RCR | Goed (domain-filter, txt-prefix, read-only RBAC) |
| Mailrelay (Stalwart) | `infrastructure/bootstrap/infrastructure/mail/controller/overlays/odcn/` (eigen Application, ns `rig-prd-ron`) | 587 en 8080 vanuit OPI; RON-egress | Accounts in rig-db, admin-creds SOPS | Geen STARTTLS vanaf Keycloak (bewust) | `stalwartlabs/mail-server:v0.11.8` | Goed; namespace handmatig (F-18) |
| cert-manager | Geen odcn-overlay; per-namespace `Issuer` uit `manifests/issuer-letsencrypt.yaml.jinja` en `operations-manager/overlays/odcn-production/issuer-letsencrypt.yaml` | HTTP-01 via publieke ingress | ACME-account per Issuer | n.v.t. | ODCN-beheerd | Goed (ECDSA-384, rotation Always, CAA) |
| ingress / router | ODCN OpenShift-router (`openshift-ingress`, controller `rig`); ingress-nginx alleen Kind (`bootstrap/rig-system/kustomize/ingress-nginx/`) | Publiek | n.v.t. | Edge-TLS; passthrough per tenant mogelijk | Router: ODCN; nginx `controller-v1.15.1` | Matig: headers nginx-only (F-12); Kind: snippets Critical (F-16) |
| Forgejo | Niet op odcn (`clusters/odcn/kustomization.yaml` bevat hem niet); alleen `overlays/sandboxed-local` | Sandbox: `forgejo.sandbox.rijksapp.dev` | `rig-admin/admin1234`, repos publiek | HTTP, DB `SSL_MODE=disable` | `forgejo:14-rootless` | n.v.t. prod; sandbox Onvoldoende (F-16) |
| pgadmin, vault | Uitgecommentarieerd in `clusters/odcn/kustomization.yaml:7-8,11-12` | Niet uitgerold | n.v.t. | n.v.t. | n.v.t. | n.v.t. |
| registry, chisel | Alleen sandbox/base; geen odcn-overlay | Niet uitgerold op prod | htpasswd in git; chisel auth-secret | n.v.t. | `registry:2`, `chisel:latest` | n.v.t. prod; sandbox Laag |
| Tenant-namespace (templates) | `operations-manager/python/manifests/*.jinja` | Per deployment publiek via router; egress internet open | SA zonder RoleBinding in repo; Capsule-admin voor OPI | TLS-modi standard/passthrough/provided | Tenant-image vrij, via RCR-proxy | Goed op isolatie (baseline-netpol, hardening); Midden op ops-egress (F-9) en internet (F-14) |

## Open vragen

1. **GitHub-toegangsbeheer**: wie heeft push op `rig-cluster-projects`, `argo-applications` en de deployments-repo, en staat er branch protection of required review op `main`? Dit bepaalt of F-2 en F-4 vandaag door een ontwikkelaar exploiteerbaar zijn. Niet toetsbaar vanuit de repo.
2. **PAT-scope**: classic met `repo` of fine-grained per repo? Eén token voor alle drie plus de per-project Argo-secrets (F-3)?
3. **SA `argocd-argocd-server`**: welke Roles genereert de operator in de `managed-by`-namespaces en in `rig-prd-operations`? Als `get secret` in `rig-prd-operations` erbij zit, is F-1 rechtstreeks een platformsleutel-lek.
4. **Live secrets**: is `keycloak-db-credentials` in `rig-prd-operations` werkelijk `keycloak` (F-5)? Heeft `keycloak-admin-credentials` live wel `KEYCLOAK_ADMIN_CLIENT_SECRET` en `KEYCLOAK_OTP_ADMIN_*` (F-20, F-21)? Beide zijn read-only met kubectl te toetsen, buiten deze statische scan gehouden.
5. **Master-realm live**: staan `bruteForceProtected`, een wachtwoordbeleid en OTP op `admin` handmatig aan? Hoeveel accounts hebben master-rol `admin`?
6. **Groepen**: wie zit in `rig-prd-developer-group` (F-7)?
7. **Status F-6**: zijn de stappen 1 t/m 3 van het verwijderplan van `emergency-restore-allow-all` uitgevoerd op een enforcerend cluster?
8. **Router**: welke `Forwarded`/`X-Forwarded-*`-headers schrijft en stript de ODCN-router, en welk `tlsSecurityProfile` staat op de `rig`-IngressController (geheugen: clusterbreed instelbaar)? Dit bepaalt F-12 (headers) en de cipher-toets die hier niet uit git te halen was.
9. **Tenant-API**: kan een tenant `repositories[].url` via API of upload wijzigen (F-15)?
10. **Sandbox-blootstelling**: luistert de dev-server-Caddy op 80/443 vanaf internet (F-16)? De docs zeggen alleen dat de naam naar `127.0.0.1` resolvet.
11. **busybox-wget**: valideert busybox 1.37 in het gebruikte image TLS-certificaten (F-24)? Eén meting in de sandbox beslist het.
12. **Projecten in prod** met de `algoritmeregister`-template of met `additional-clients` zonder `redirect-uris` (F-23, F-26): te toetsen in de prod projects-repo.
