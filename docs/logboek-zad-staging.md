# Logboek: ZAD staging op ODCN

Wat we deden, in welke volgorde, en waarom. Het runbook is `features/zad-staging.md`; dit is de geschiedenis erachter. Een nieuwe regel per stap, nieuwste onderaan.

Het is de eerste keer dat ZAD naast productie een tweede installatie op ODCN krijgt. Het fundament (branch `fundament`, `docs/cluster-toevoegen.md` daar) was een ander fysiek cluster met cluster-admin; daarvan nemen we de lessen mee, niet de opsplitsing.

## Wat we van het fundament meenamen

| les uit het fundament | hier |
|---|---|
| de AGE-sleutel valt niet terug op een andere | `AGE_KEY_FILE` per cluster, de taken weigeren zonder |
| een eigen git-token per cluster | eigen tokens per staging-repo, zie het runbook |
| de CMP eist de doelnamespace en `sops-age-key` daarin | `bootstrap-argo-system` zet de sleutel in de platform- en de ron-namespace |
| twee external-dns met dezelfde owner-id wissen elkaars records | staging filtert alleen `staging.rijksapp.dev`, met owner-id `zad-staging` |
| sync-waves tussen Applications vragen een Lua health check | geen waves; wat op een secret wacht, herstelt zich zelf |
| `allowEmpty: false` op elke Application | ook op de twee nieuwe secrets-apps, en daar `prune: false` |
| een PVC zonder `storageClassName` blijft eeuwig Pending | de overlays van odcn zetten hem al; overgenomen |
| pin de kubecontext | nog niet gebouwd, open punt in het runbook |

Wat niet overkwam: het fundament had cluster-admin en installeerde zijn eigen operators. Op ODCN zijn we een tenant, dus de operators (ArgoCD, CNPG, cert-manager, Quay) zijn van het platform, en `prepare-argocd-operator` weigert daar nu.

## 2026-10-19

- **Gemeten op de cluster (alleen-lezen).** Tenant `rig-staging` bestaat, maximaal 50 namespaces, nog geen. Quota: 32 CPU / 48Gi limits, 16 CPU / 24Gi requests, 300 pods, 3 LoadBalancers, 400Gi storage. Storage classes en regels zijn gelijk aan `rig-prd`. Een gebruiker uit `rig-staging-admin-group` mag namespaces maken. De vier tenant-eigenaren van productie zijn serviceaccounts in `rig-prd-operations`; voor staging moet ODC-Noord hetzelfde doen. De routes van productie vallen onder `rig.prd1.gn2.quattro.rijksapps.nl` (130 stuks). De ingresscontroller zelf is voor ons niet leesbaar.
- **GitHub main gelijkgetrokken met Forgejo main.** De bomen verschilden: GitHub liep achter, en de drie commits die alleen daar stonden zaten inhoudelijk al in Forgejo. Een commit met de boom van Forgejo bovenop GitHub main (`f356a9f5b`), als fast-forward. De ArgoCD van productie zag de nieuwe revisie en hoefde niets te syncen.
- **Repos aangemaakt**, private, topic `zad`: `zad-staging-projects`, `zad-staging-argo-applications`, `zad-staging-deployments`, `zad-staging-secrets`. Eén repo met een branch per omgeving viel af: leesrecht geldt per repo.
- **Besluit: de geheimen in `zad-staging-secrets`**, alle soorten. RIG-Cluster is publiek.
- **Besluit: mail en VLAM zoals de sandbox.** Een relay met een mailsink, een VLAM-plaatshouder.
- **Besluit: eigen Keycloak met de prod-Keycloak als upstream.** De sandbox-stand bleek in het OPI-realm een `admin` met wachtwoord `admin1234` te maken; staging kreeg daarom een eigen stand `staging` zonder lokale gebruiker.
- **Gebouwd** op branch `claude/staging-cluster`: de sleutelkeuze per cluster, `odcn-staging` in `CLUSTER_CONFIG`, de Keycloak-stand, de overlays van tien componenten, de bootstrap- en OPI-overlay, de clusterlijst, en de secrets-repo-route in de Taskfile. Lokaal gerenderd en gecontroleerd dat er geen `rig-prd` in de uitvoer staat; de secret-generatie gedraaid tegen een wegwerprepo en een wegwerpsleutel.
- **Clusterverbinding uitgezet** door de gebruiker, zodat er tijdens de ontwikkeling niets per ongeluk op productie gebeurt. Wat alleen op de cluster te toetsen is, staat als lijst in het runbook.
