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

- **Gemeten op de cluster (alleen-lezen).** Tenant `rig-staging` bestaat, maximaal 50 namespaces, nog geen. Quota: 32 CPU / 48Gi limits, 16 CPU / 24Gi requests, 300 pods, 3 LoadBalancers, 400Gi storage. Storage classes en regels zijn gelijk aan `rig-prd`. Een gebruiker uit `rig-staging-admin-group` mag namespaces maken. De vier tenant-eigenaren van productie zijn serviceaccounts in `rig-prd-operations`; toen leek het dat ODC-Noord dat voor staging ook moest doen (achterhaald, zie de proef hieronder). De routes van productie vallen onder `rig.prd1.gn2.quattro.rijksapps.nl` (130 stuks). De ingresscontroller zelf is voor ons niet leesbaar.
- **GitHub main gelijkgetrokken met Forgejo main.** De bomen verschilden: GitHub liep achter, en de drie commits die alleen daar stonden zaten inhoudelijk al in Forgejo. Een commit met de boom van Forgejo bovenop GitHub main (`f356a9f5b`), als fast-forward. De ArgoCD van productie zag de nieuwe revisie en hoefde niets te syncen.
- **Repos aangemaakt**, private, topic `zad`: `zad-staging-projects`, `zad-staging-argo-applications`, `zad-staging-deployments`, `zad-staging-secrets`. Eén repo met een branch per omgeving viel af: leesrecht geldt per repo.
- **Besluit: de geheimen in `zad-staging-secrets`**, alle soorten. RIG-Cluster is publiek.
- **Besluit: mail en VLAM zoals de sandbox.** Een relay met een mailsink, een VLAM-plaatshouder.
- **Besluit: eigen Keycloak met de prod-Keycloak als upstream.** De sandbox-stand bleek in het OPI-realm een `admin` met wachtwoord `admin1234` te maken; staging kreeg daarom een eigen stand `staging` zonder lokale gebruiker.
- **Gebouwd** op branch `claude/staging-cluster`: de sleutelkeuze per cluster, `odcn-staging` in `CLUSTER_CONFIG`, de Keycloak-stand, de overlays van tien componenten, de bootstrap- en OPI-overlay, de clusterlijst, en de secrets-repo-route in de Taskfile. Lokaal gerenderd en gecontroleerd dat er geen `rig-prd` in de uitvoer staat; de secret-generatie gedraaid tegen een wegwerprepo en een wegwerpsleutel.
- **Clusterverbinding uitgezet** door de gebruiker, zodat er tijdens de ontwikkeling niets per ongeluk op productie gebeurt. Wat alleen op de cluster te toetsen is, staat als lijst in het runbook.
- **Clusterverbinding weer aan voor een proef.** Op productie zijn de vier serviceaccounts tenant-eigenaar, zichtbaar in `status.owners` van `rig-prd`; in `rig-staging` stond alleen `rig-staging-admin-group`. Ze dragen geen promote-label en hebben dezelfde rollen als de admin-groep, dus vermoedelijk zijn het `TenantOwner`-objecten van ODC-Noord; die kunnen wij niet lezen. `rig-prd-operations` zelf is door ODC-Noord aangemaakt (change request `W2505_118`).
- **`rig-staging-operations` aangemaakt**, zoals de OPI een projectnamespace maakt: met `egressGatewayPolicy: internet`, en daarna het label `argocd.argoproj.io/managed-by`. Capsule, Kyverno en OpenShift accepteerden hem. OpenShift gaf hem UID-bereik `1003110000/10000`; de CMP-sidecar in de bootstrap-overlay stond op `1001620000` (dat van productie) en is aangepast.
- **Owner promotion werkt.** Serviceaccount `namespace-manager` aangemaakt met `owner.projectcapsule.dev/promote=true`: hij staat direct in `status.owners` met `capsule-namespace-provisioner`, `capsule-namespace-deleter` en `admin`. Met een token van tien minuten getoetst: `can-i create namespaces` is yes, een server-dry-run van `rig-staging-proef` slaagt, `rig-prd-proef` weigert Capsule. Het tenant-eigenaarschap hoeft dus niet bij ODC-Noord aangevraagd te worden. ODC-Noord bevestigde daarna dat dit de bedoelde self-service is voor een serviceaccount binnen één tenant; eigenaarschap over al je tenants tegelijk gaat via TopDesk. De namespace en de serviceaccount blijven staan; de bootstrap neemt ze over.
- **`task staging:setup` gebouwd**: één commando dat staging inricht, naar het voorbeeld van `sandbox:setup` en `cluster:bootstrap` uit het fundament. Getoetst met een kubectl-stub en een lokale remote in plaats van GitHub: de run kwam tot de controle op het OPI-image (terecht rood), een tweede run veranderde niets. Onderweg gevonden: de shell van Task kent `read -s`, `umask` en `${!naam+woord}` niet, en Task 3.41 negeert `if:` op een taakaanroep stil. `generate-env-secrets-for-operations-manager` toonde het secret onversleuteld (base64) in de terminal; dat is eruit. Het eigen leestoken voor ArgoCD staat geparkeerd in `TODO.md`.
- **Besluit: DNS via external-dns met het TransIP-account van productie**, net als de andere DNS-records. `staging:setup` vraagt de accountnaam en het pad naar de PEM-sleutel. De OPI op staging krijgt de TransIP-variabelen niet: zijn CAA-records liggen op dezelfde zones als die van productie. De client `zad-staging` in de prod-Keycloak maak je vooraf met de hand; door de OPI laten aanmaken staat in `TODO.md`, bij een hub-spoke-model.
