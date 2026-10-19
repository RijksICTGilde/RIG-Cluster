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

Tenant-eigenaarschap: de vier serviceaccounts (`namespace-manager`, `argocd-argocd-application-controller`, `argocd-argocd-server`, `external-dns`) promoveren we zelf met het label `owner.projectcapsule.dev/promote: "true"`. ODC-Noord biedt dat als self-service voor een serviceaccount binnen één tenant; alleen eigenaarschap over al je tenants tegelijk gaat via een TopDesk-aanvraag. De labels staan in de staging-overlays. Gemeten op 19 oktober 2026: de gepromoveerde `namespace-manager` komt in `status.owners` met `capsule-namespace-provisioner`, `capsule-namespace-deleter` en `admin`, en mag `rig-staging-*` aanmaken maar geen `rig-prd-*`. Alleen een eigenaar mag het label zetten, dus de bootstrap past het met jouw account toe.

Zelf:

1. **Tokens per repo, met een eigen token per omgeving**, zodat het intrekken van een gelekt staging-token productie niet raakt. ArgoCD heeft leesrecht nodig op `zad-staging-argo-applications` en `zad-staging-secrets`; de OPI heeft schrijfrecht nodig op `zad-staging-projects`, `zad-staging-argo-applications` en `zad-staging-deployments`. RIG-Cluster is publiek en vraagt geen token.
2. **Een client `zad-staging`** in het realm `rig-platform` van de productie-Keycloak, als kopie van `development-clusters` maar met als redirect-URI alleen `https://keycloak.staging.rijksapp.dev/*`. Het client secret gaat in het env-secret als `KEYCLOAK_MASTER_OIDC_CLIENT_SECRET`. Gebruik niet `development-clusters` zelf: dat is de sandbox.
3. **DNS**: zie de open beslissing over TransIP hieronder.

### Inrichten met één commando

```bash
task select-cluster      # 3) odcn-staging
task staging:setup
```

`staging:setup` is opnieuw te draaien: wat er al is, slaat hij over. Hij doet achter elkaar:

1. controleert de context: je account moet in `rig-staging-admin-group` zitten, en je typt `staging` om door te gaan;
2. maakt `security/odcn-staging-key.txt` als die ontbreekt, en clonet `zad-staging-secrets` naast het project (een afgebroken vorige run hervat hij; een clone die achterloopt op GitHub weigert hij);
3. genereert de infra-, bootstrap- en backup-geheimen in die clone;
4. vraagt het PAT van ArgoCD (lezen op `zad-staging-argo-applications`, `zad-staging-secrets` en `zad-staging-deployments`), het PAT van de OPI (schrijven op de drie datarepos), het client secret van `zad-staging` (leeg = geen SSO) en het TransIP-account van productie (accountnaam en het pad naar de PEM-sleutel) voor external-dns, en zet ze versleuteld op hun plek; het env-bestand van de OPI maakt hij alleen als het env-secret nog niet in de secrets-repo staat;
5. commit en pusht de secrets-repo na bevestiging;
6. stopt als het gepinde OPI-image `odcn-staging` niet kent;
7. draait `bootstrap-argo-system` en `bootstrap-backup-destination`;
8. wacht tot de Applications Synced en Healthy zijn, en toont de tenant-eigenaren.

Zonder vragen: zet `STAGING_CONFIRM=staging`, `STAGING_ARGOCD_TOKEN`, `STAGING_OPI_TOKEN`, `STAGING_KEYCLOAK_CLIENT_SECRET` (gezet maar leeg betekent geen SSO), `STAGING_TRANSIP_ACCOUNT` en `STAGING_TRANSIP_KEY_FILE`.

Wat hij bewust niet doet: het OPI-image bouwen (`task publish-operations-manager`, dan `task pin-operations-manager-image OVERLAY=odcn-staging IMAGE_TAG=<tag>`), de ArgoCD-operator installeren (die is op ODCN van het platform; `prepare-argocd-operator` weigert daar), en de client `zad-staging` in de prod-Keycloak aanmaken (dat doe je vooraf met de hand).

DNS doet external-dns, net als op productie: hij maakt de records aan zodra er een ingress is, met het TransIP-account van productie maar alleen voor `staging.rijksapp.dev` en met owner-id `zad-staging`. De OPI krijgt op staging geen TransIP: zijn CAA- en no-mail-records in `rijks.app`, `rijksapp.nl` en `rijksapp.dev` beheert productie al, en een tweede schrijver daarop hoort niet in de pentestomgeving.

Ruim na afloop de `secrets-overview-*.yaml` in de root van RIG-Cluster op: daar staan de gegenereerde wachtwoorden in platte tekst. Ze staan bewust daar (gitignored) en niet in de secrets-repo. Het env-bestand `operations-manager/python/.env.odcn-staging.secrets` (gitignored, alleen voor jou leesbaar) bevat de PAT's en is de bron voor het env-secret.

De losse taken (`generate-*-for-cluster`, `generate-backup-destination-secret`, `generate-env-secrets-for-operations-manager`, `bootstrap-argo-system`, `bootstrap-backup-destination`) blijven bruikbaar; buiten `staging:setup` weigeren de generatietaken op een clone die niet schoon is of niet gelijk loopt met GitHub.

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

- **TransIP.** Besloten: external-dns op staging gebruikt het TransIP-account van productie, met eigen filter en owner-id. TransIP kent geen sleutels per zone, dus wie staging overneemt kan met die sleutel ook `rijksapp.nl` en `rijks.app` wijzigen; een eigen TransIP-sleutel (in te trekken zonder productie te raken) is het minimum om later te regelen.
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
