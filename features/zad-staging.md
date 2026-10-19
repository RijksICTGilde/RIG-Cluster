# ZAD staging op ODCN

## Wat het is

Een tweede ZAD-installatie op dezelfde OpenShift-cluster als productie, in de Capsule-tenant `rig-staging`. Het doel is een pentest: een omgeving die zich gedraagt als productie, zonder dat er productiedata, productiegeheimen of productiegebruikers in zitten.

| | productie | staging |
|---|---|---|
| clusternaam (`CLUSTER_MANAGER`) | `odcn-production` | `odcn-staging` |
| tenant, prefix | `rig-prd`, `rig-prd-` | `rig-staging`, `rig-staging-` |
| platformnamespace | `rig-prd-operations` | `rig-staging-operations` |
| portaal | `zad.rijksapp.nl` | `zad.staging.rijksapp.dev` |
| Keycloak | `keycloak.rijksapp.nl`, SSO-Rijk direct | `keycloak.staging.rijksapp.dev`, met de prod-Keycloak als upstream |
| projecthostnamen | `rijksapp.nl`, `rijks.app`, `rijksapp.dev` | `staging.rijksapp.dev` |
| mail | relay naar de mailserver van de Rijksoverheid (RON) | relay naar een mailsink, er gaat niets naar buiten |
| VLAM | via RON | plaatshouder, net als de sandbox |
| repos | `rig-cluster-projects`, `argo-applications`, `rig-cluster-application-test` | `zad-staging-projects`, `zad-staging-argo-applications`, `zad-staging-deployments` |
| geheimen | SOPS-bestanden in de overlays van RIG-Cluster | SOPS-bestanden in de private repo `zad-staging-secrets` |
| AGE-sleutel | `security/key.txt` | `security/odcn-staging-key.txt` |

Alles wat de cluster zelf bepaalt is gelijk aan productie: router-shard `rig`, storage classes, de Quay-proxycaches van de klant `rig`, Capsule. Het logboek van de opzet staat in `docs/logboek-zad-staging.md`.

## Waarom de geheimen in een eigen repo staan

RIG-Cluster is een publieke repo. De SOPS-bestanden van productie staan daar versleuteld op internet. Voor staging, waar pentesters juist naar zoiets zoeken, staan de geheimen in de private repo `zad-staging-secrets`. Ze blijven daar ook met SOPS versleuteld, met de eigen staging-sleutel: de private repo is een tweede laag, geen vervanging.

Leesrecht op GitHub geldt per repo en niet per branch. Daarom heeft staging eigen repos en geen branch in de repos van productie: wie staging overneemt, kan anders de productiebranch lezen.

```
zad-staging-secrets/
  infrastructure/       Application infrastructure-secrets  -> rig-staging-operations
  ron/                  Application ron-secrets             -> rig-staging-ron (de twee mailgeheimen)
  bootstrap/            task bootstrap-argo-system          (repo-credentials van ArgoCD)
  operations-manager/   task bootstrap-argo-system, task deploy-operations-manager (env-secret van de OPI)
  backup-destination/   task bootstrap-backup-destination   (root van de backup-MinIO)
```

`bootstrap/` en `operations-manager/` synct ArgoCD niet: ze moeten er zijn voordat ArgoCD draait, of naast ArgoCD. De OPI-configmap staat wel in de publieke repo, maar zonder wachtwoorden: de git-wachtwoorden komen uit het env-secret.

De Taskfile kiest dit pad met `SECRETS_DIR` in `.env-taskfile-odcn-staging`. Zonder `SECRETS_DIR` (productie, sandbox) verandert er niets.

## Opzetten

### Wat er moet zijn voordat je begint

Bij ODC-Noord:

1. **Of de ArgoCD-operator een instantie in `rig-staging-operations` mag beheren.** Niet geverifieerd.

Tenant-eigenaarschap voor de vier serviceaccounts hoeft niet aangevraagd te worden. Op productie zijn `namespace-manager`, `argocd-argocd-application-controller`, `argocd-argocd-server` en `external-dns` eigenaar via ODC-Noord, vermoedelijk met `TenantOwner`-objecten. Op staging promoveren we ze zelf met het label `owner.projectcapsule.dev/promote: "true"` (owner promotion van Capsule, `allowOwnerPromotion` staat aan). De labels staan in de staging-overlays. Gemeten op 19 oktober 2026: de gepromoveerde `namespace-manager` komt in `status.owners` met `capsule-namespace-provisioner`, `capsule-namespace-deleter` en `admin`, en mag `rig-staging-*` aanmaken maar geen `rig-prd-*`. Alleen een eigenaar mag het label zetten, dus de bootstrap past het met jouw account toe.

Zelf:

2. **Tokens per repo, met een eigen token per omgeving**, zodat het intrekken van een gelekt staging-token productie niet raakt. ArgoCD heeft leesrecht nodig op `zad-staging-argo-applications` en `zad-staging-secrets`; de OPI heeft schrijfrecht nodig op `zad-staging-projects`, `zad-staging-argo-applications` en `zad-staging-deployments`. RIG-Cluster is publiek en vraagt geen token.
3. **Een client `zad-staging`** in het realm `rig-platform` van de productie-Keycloak, als kopie van `development-clusters` maar met als redirect-URI alleen `https://keycloak.staging.rijksapp.dev/*`. Het client secret gaat in het env-secret als `KEYCLOAK_MASTER_OIDC_CLIENT_SECRET`. Gebruik niet `development-clusters` zelf: dat is de sandbox.
4. **DNS**: zie de open beslissing over TransIP hieronder.

### De volgorde

Vanuit de root van RIG-Cluster, op een commit waarvan de staging-overlays al op GitHub main staan: ArgoCD op staging leest RIG-Cluster van GitHub.

```bash
task select-cluster                          # 3) odcn-staging
task generate-age-key                        # maakt security/odcn-staging-key.txt
git clone https://github.com/RijksICTGilde/zad-staging-secrets.git ../zad-staging-secrets

task generate-infrastructure-secrets-for-cluster   # infrastructure/ en ron/
task generate-bootstrap-secrets-for-cluster        # bootstrap/
task generate-backup-destination-secret            # backup-destination/
```

Dan de waarden die niet te genereren zijn:

- de twee repo-tokens in `bootstrap/*.sops.yaml` (het veld `password` staat op `PLACEHOLDER`): `SOPS_AGE_KEY="$(sed -n '3p' security/odcn-staging-key.txt)" sops ../zad-staging-secrets/bootstrap/<bestand>`;
- `operations-manager/python/.env.odcn-staging.secrets` (gitignored) met `SECRET_KEY`, `KEYCLOAK_MASTER_OIDC_CLIENT_SECRET`, `GIT_PROJECTS_SERVER_PASSWORD`, `GIT_ARGO_APPLICATIONS_PASSWORD`, `PROJECT_REPO_PASSWORD`, `BACKUP_S3_ENDPOINT=http://minio.rig-staging-backup.svc.cluster.local:9000`, `BACKUP_S3_ACCESS_KEY=backup-admin` en `BACKUP_S3_SECRET_KEY` (het `root-password` uit het overzicht van de vorige stap), en dan `task generate-env-secrets-for-operations-manager`.

Commit en push `zad-staging-secrets`, en ruim de `secrets-overview-*.yaml` op: daar staan de wachtwoorden in platte tekst. Die staan bewust in RIG-Cluster (gitignored) en niet in de secrets-repo, waar ze zo mee gecommit zouden worden. De generatietaken weigeren op een clone die niet schoon is of niet gelijk loopt met GitHub: op een verouderde clone maakt de generator een secret dat er al was opnieuw aan, met een ander wachtwoord.

Het OPI-image moet `odcn-staging` kennen. Het image dat nu in de overlay staat (dat van productie) doet dat niet, en de OPI valt dan bij het starten om op `Cluster 'odcn-staging' not found`:

```bash
task publish-operations-manager
task pin-operations-manager-image OVERLAY=odcn-staging IMAGE_TAG=<tag>
```

Dan de cluster:

```bash
task bootstrap-argo-system          # namespaces, ArgoCD, de Applications, repo-credentials, sops-age-key, OPI
task bootstrap-backup-destination   # de backup-MinIO, bewust buiten ArgoCD
```

**Nooit `task prepare-argocd-operator` op staging.** De operator is op ODCN clusterbreed en gedeeld met productie; de taak weigert daar sinds deze opzet ook zelf.

## Wat er op de cluster nog getoetst moet worden

Lokaal is alles gebouwd en gerenderd; dit kan alleen op de cluster:

- [ ] De eerste route in `rig-staging-operations` krijgt de zone `rig.prd1.gn2.quattro.rijksapps.nl`, en de router-shard `rig` bedient de staging-namespaces.
- [ ] De pull-secrets van de proxycaches (`ghcr-rig-robot-pull-secret` en de andere) komen in de staging-namespaces terecht.
- [ ] ODCN laat `rig-staging-ron` met `egressGatewayPolicy: internet` toe.
- [ ] De ArgoCD-instantie in `rig-staging-operations` start (operator, rechten), en de CMP-sidecar draait met UID 1003110000.
- [ ] De operator laat het promote-label op `argocd-argocd-application-controller` en `argocd-argocd-server` staan, en ArgoCD mag het label op `external-dns` zetten: alle vier staan in `status.owners` van `rig-staging`.
- [ ] De CMP rendert `infrastructure-secrets` en `ron-secrets` uit de private repo.
- [ ] Keycloak en de OPI starten zodra hun secrets er zijn, zonder sync-waves.
- [ ] Inloggen via de upstream: staging-Keycloak, prod-Keycloak, SSO-Rijk.
- [ ] Een mail van Keycloak komt aan in de mailsink, en de sink-UI is alleen vanaf het VPN bereikbaar.
- [ ] De OPI maakt een project aan in `rig-staging-<project>`, schrijft naar de drie staging-repos, en ArgoCD synct het.
- [ ] Niets in `rig-prd-*` is veranderd.

## Open beslissingen

- **TransIP.** external-dns en de CAA-reconciler van de OPI gebruiken een TransIP-sleutel, en TransIP kent geen sleutels per zone: een staging-sleutel kan ook `rijksapp.nl` en `rijks.app` wijzigen. Het alternatief is een wildcard `*.staging.rijksapp.dev` als CNAME naar `router.rijksapp.dev`, met de hand gezet, en dan geen external-dns en geen `transip-credentials` op staging. De OPI slaat zijn DNS-werk dan zelf over. Tot dit besloten is, staat external-dns in de clusterlijst maar ontbreekt het secret.
- **`emergency-restore-allow-all`.** Productie draagt een allow-all-netpol in `rig-prd-operations` sinds de storing van 10 juni; staging heeft hem overgenomen om gelijk aan productie te zijn. Voor de pentest is dat een bewuste keuze: met de policy toets je wat productie heeft, zonder toets je wat productie zou moeten hebben.
- **Wie er bij mag.** `ALLOWED_EMAILS` en `ADMIN_EMAILS` zijn die van productie.
- **De kubecontext.** Staging en productie bereik je met dezelfde context. Het fundament pinde `KUBE_CONTEXT` per cluster en controleerde hem voor elke taak; hier is dat nog niet gebouwd. De overlays van staging noemen hun namespaces expliciet, maar een verkeerd geselecteerd cluster past de overlays van productie toe.

## Bekende verschillen die blijven

- Staging en productie volgen allebei RIG-Cluster `main`. Een wijziging in een gedeelde base raakt beide tegelijk; promotie via een branch is een latere stap.
- Grafana en de registry-upload staan uit op staging: de Mimir-datasources tonen productie, en de registry-organisatie `rig/zad` is die van productie.
- De mailsink krijgt een zelfondertekend certificaat met de naam `rig-ron` in plaats van `rig-staging-ron`; de relay accepteert dat certificaat met `MAIL_UPSTREAM_ALLOW_INVALID_CERTS`, net als in de sandbox.

## Bestanden

| wat | waar |
|---|---|
| clusterconfiguratie | `operations-manager/python/opi/core/cluster_config.py`, sleutel `odcn-staging` |
| Keycloak-stand | `opi/bootstrap/keycloak_setup.py`, `KEYCLOAK_BOOTSTRAP_CONFIG=staging` |
| Taskfile-omgeving | `.env-taskfile-odcn-staging` |
| clusterlijst | `infrastructure/bootstrap/clusters/odcn-staging` |
| componenten | `infrastructure/bootstrap/infrastructure/*/overlays/odcn-staging` |
| ArgoCD en Applications | `bootstrap/rig-system/kustomize/overlays/odcn-staging` |
| OPI | `bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-staging` |
| bootstrap-templates | `bootstrap/rig-system/kustomize/secrets/templates/odcn-staging` |
| backup-template | `infrastructure/bootstrap/infrastructure/backup-destination/controller/secret-templates` |
