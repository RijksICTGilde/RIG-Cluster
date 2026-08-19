# Operators via ArgoCD in plaats van via taken

Status: plan, 19 augustus 2026. Aanleiding: bij het inrichten van fundament-poc bleek dat vier componenten (ingress-nginx, cert-manager, de opslagprovisioner en CloudNativePG) via `kubectl apply` in een Taskfile-taak worden geïnstalleerd, terwijl negen andere componenten wel via ArgoCD gaan. Dit plan beschrijft waarom die grens er ligt, waarom hij op sommige clusters niet nodig is, en wat het kost om hem te verplaatsen.

Dit plan bevat geen wijzigingen. Het is bedoeld om eerst besloten te worden.

## 1. Waar de grens vandaag ligt, en waarom

Gemeten op de sandbox: negen componenten komen uit `infrastructure/bootstrap/clusters/<cluster>/kustomization.yaml` en worden door ArgoCD uitgerold. Drie komen uit `sandbox:setup` via `kubectl apply` (ingress-nginx, CloudNativePG, de CSI-snapshot-CRD's). Op fundament-poc kwam daar in de installatietaak cert-manager en de opslagprovisioner bij.

Die grens is niet willekeurig. De gebouwde infrastructuurlijst bevat **uitsluitend namespaced resources**: ConfigMap, Deployment, Ingress, NetworkPolicy, PersistentVolumeClaim, PodMonitor, Secret, Service, plus de CNPG-`Cluster` en `Database` custom resources. Er zit geen enkele CRD in, geen ClusterRole, geen StorageClass, geen webhook-configuratie.

De reden staat in de ODCN-overlay: ArgoCD praat daar met de Capsule-proxy (`cluster-api.apps.prd1.gn2.quattro.rijksapps.nl`) en is dus tenant-scoped. Een tenant-scoped ArgoCD **kan** geen CRD's of cluster-brede RBAC aanmaken. De operators staan buiten GitOps omdat ArgoCD daar het recht niet heeft.

De impliciete regel is dus: cluster-scoped gaat via een taak, namespaced gaat via ArgoCD. Die regel is nergens opgeschreven, en dat is precies waarom hij bij het inrichten van een nieuw cluster onzichtbaar was.

## 2. Waarom die grens niet overal nodig is

De beperking is een eigenschap van ODCN, niet van ZAD. Op sandboxed-local en fundament-poc draait geen Capsule, praat ArgoCD met de echte API-server en zijn wij cluster-admin. Daar kan ArgoCD de operators wel installeren.

Het project haven (`gitops-flux`) doet precies dat en laat zien dat het werkt. In hun `infrastructure/`-boom staan `cert-manager`, `cloudnative-pg`, `eck-operator`, `sealed-secrets` en `istio` gewoon als component. De enige bootstrap is Flux zelf: `gotk-components.yaml` en `gotk-sync.yaml`. Al het andere komt uit git.

Twee dingen aan hun opzet zijn relevant voor ons.

Ze splitsen per component in `controller/` en `config/`. Dat doet RIG-Cluster al: `keycloak/controller` naast `keycloak/config`, `minio/controller` naast `minio/config`. De boomstructuur is dus al de goede.

En ze beschrijven de volgorde expliciet, met één Flux-`Kustomization` per component:

```yaml
kind: Kustomization
metadata: {name: cert-manager-config}
spec:
  path: ./infrastructure/cert-manager/config/overlays/azure
  wait: true
  dependsOn:
    - name: cert-manager-controller
```

Dat is wat wij missen. Wij hebben één ArgoCD-Application over een platte lijst, dus er is geen plek om "de CRD's eerst, de custom resources daarna" uit te drukken.

## 3. Wat echt bootstrap is

Als de operators naar git kunnen, blijft over wat moet bestaan voordat de GitOps-motor iets kan doen:

1. de namespace waarin hij draait;
2. het `sops-age-key` secret, want git bevat versleutelde secrets die hij moet kunnen lezen;
3. de motor zelf: de ArgoCD-operator en de ArgoCD-instantie;
4. repo-credentials, anders kan hij niet clonen;
5. de root-Application die naar het clusterpad wijst.

Meer niet. Dat is de ondergrens en die is voor elk cluster gelijk. Wat per cluster verschilt is niet de bootstrap maar **welke componenten in de clusterlijst staan**.

Dat schaalt ook vooruit. Landt ZAD later op een haven-achtig cluster waar cert-manager en CloudNativePG al door het platform geleverd worden, dan staan ze simpelweg niet in onze lijst. Zelfde knop, ander antwoord, geen nieuwe machinerie.

## 4. Wat er moet veranderen

### 4.1 Van één Application naar een Application per component

Vandaag wijst één Application naar `infrastructure/bootstrap/clusters/<cluster>` en rendert kustomize daar alles in één keer. Binnen één Application kun je met `argocd.argoproj.io/sync-wave` wel ordenen, maar niet wachten tot een CRD daadwerkelijk geregistreerd is voordat een custom resource wordt aangeboden. Voor operators is dat het verschil tussen werken en niet werken.

De app-of-apps-vorm lost dat op: de clusterlijst wordt een lijst van Applications in plaats van een lijst van kustomize-paden, en elke Application krijgt een wave. ArgoCD verwerkt waves op volgorde en wacht tot de resources van een wave gezond zijn.

Sync-waves zijn geen nieuw concept hier: `argocd-application-user-applications.yaml` gebruikt al `sync-wave: "2"` om na de infrastructuur te komen.

### 4.2 De waves

Voorstel, op basis van wat waarvan afhangt:

| wave | componenten | waarom |
|---|---|---|
| -2 | cluster-resources (namespaces, RBAC, StorageClass) | alles hangt eraan |
| -1 | operators: cert-manager, CloudNativePG, ingress-nginx, opslagprovisioner | leveren CRD's en de ingressklasse |
| 0 | secrets/config | de rest leest deze secrets |
| 1 | postgresql/database, redis, minio/controller | vragen PVC's en de CNPG-CRD |
| 2 | keycloak/controller, prometheus, minio/config | keycloak wacht op de database |
| 3 | keycloak/config, external-dns | realm-configuratie na keycloak |

### 4.3 ServerSideApply

De CRD's van cert-manager en CloudNativePG zijn te groot voor de annotatie die client-side apply gebruikt (`metadata.annotations: Too long`). Die Applications hebben `syncOptions: [ServerSideApply=true]` nodig. Dat staat nu nergens in de repo.

### 4.4 Per cluster aan of uit

De operator-Applications komen in de clusterlijst van sandboxed-local en fundament-poc, en **niet** in die van odcn. Daar blijven ze buitenom geïnstalleerd, precies zoals nu. De ODCN-overlay verandert dus niet.

Dat maakt de regel uit paragraaf 1 expliciet in plaats van impliciet: heeft de ArgoCD van dit cluster cluster-brede rechten, dan staan de operators in de lijst.

## 5. Risico's

**Prune op cluster-scoped resources.** De infrastructuur-Application draait met `prune: true`. Zodra CRD's daaronder vallen, betekent een lege of kapotte render dat CRD's verwijderd worden, en dat cascadeert via finalizers naar alle custom resources. Dat is dezelfde klasse fout die bij de user-applications al is afgevangen met `allowEmpty: false`; die vlag moet hier ook op, en dat moet vóór de eerste sync geregeld zijn en niet erna.

**De sandbox is een werkend pad.** `sandbox:setup` wordt dagelijks gebruikt. De verbouwing moet daar additief kunnen: eerst op fundament-poc bewijzen, dan pas de sandbox migreren.

**De CMP-plugin.** De render loopt via `kustomize-sops-v1.0`. Een Application per component betekent meer renders, en de streaming-optimalisatie (`ARGOCD_REPO_SERVER_PLUGIN_USE_MANIFEST_GENERATE_PATHS`) moet dan per Application kloppen, anders wordt elke render de hele monorepo.

**Remote kustomize-resources zijn onbeproefd bij ArgoCD.** De vier operator-componenten halen hun manifest van een URL op. Gemeten: geen enkele component die vandaag in een clusterlijst staat doet dat, dus of de repo-server met de CMP-sidecar tijdens een render naar buiten mag is nooit getest. Op ODCN is dat sowieso geblokkeerd (daar draait de RCR-mirror om), op fundament staat egress open. Alternatief is de manifests in de repo zetten in plaats van ze op te halen; dat is beter reproduceerbaar en haalt netwerk uit het renderpad, maar het maakt de repo groter en het ophogen van een versie wordt een commit met veel regels. Dit moet in stap 2 beslist en gemeten worden, niet aangenomen.

**Volgorde faalt anders dan een script faalt.** Een taak stopt bij de eerste fout met een leesbare melding. Een sync die op een wave blijft hangen ziet eruit als "Progressing" en vraagt om in ArgoCD te kijken. Dat is een echte verandering in hoe je een mislukte installatie debugt.

## 6. Voorgestelde volgorde

1. Operator-componenten toevoegen aan de infrastructuurboom: `ingress-nginx`, `cert-manager`, `storage` en `cloudnative-pg`, elk met een `controller/`-map en overlays voor sandboxed-local en fundament-poc. De bases bestaan deels al (`bootstrap/rig-system/kustomize/ingress-nginx`, `.../csi-snapshot`).
2. De clusterlijst van fundament-poc omzetten naar Applications met waves, inclusief `allowEmpty: false` en `ServerSideApply=true`.
3. Op fundament-poc van nul draaien en meten: komt alles omhoog, en in de goede volgorde.
4. Pas daarna sandboxed-local migreren en de dan overbodige taken verwijderen.
5. ODCN ongemoeid laten, en de regel uit 4.4 als commentaar bij zijn clusterlijst zetten zodat de volgende lezer weet waarom het daar anders is.

Stap 1 tot en met 3 zijn te doen zonder de sandbox aan te raken. Dat is bewust: er moet een werkend voorbeeld staan voordat er iets verbouwd wordt dat elke dag gebruikt wordt.

## 7. Wat dit plan niet doet

**Geen Flux.** Haven gebruikt Flux, wij ArgoCD. Het patroon is overdraagbaar, het gereedschap hoeft niet mee.

**Geen tenants door ZAD zelf.** Dat kwam ter sprake als vergezicht en valt buiten dit plan.

**Geen wijziging aan ODCN.** De Capsule-beperking blijft, en daarmee blijft daar de imperatieve installatie van operators.
