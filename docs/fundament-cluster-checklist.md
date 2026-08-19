# Fundament-cluster: wat staat er, wat missen we, wat installeren we

Status: gemeten op 13 augustus 2026, cluster `019ffa0b-211b-7de7-9a35-cab73fdeacc2` (organisatie `zad`), toen het cluster 40 minuten oud was.

Dit is het meetrapport bij `docs/een-nieuw-cluster-installeren.md`. Dat document stelt in paragraaf 3 een vragenlijst aan het platformteam; een flink deel daarvan is inmiddels gewoon te meten, want we hebben clustertoegang met cluster-admin. Wat hier staat is gemeten, niet aangenomen. Wat nog een vraag is, staat als vraag.

Namen liggen nog steeds niet vast, zie de waarschuwing bovenaan het hoofddocument.

## 0. Toegang

Kubeconfig uit `kubeconfig-zad-cluster.yaml`, gemergd als context `zad-fundament`. Auth loopt via een exec-plugin: `functl cluster token <uuid>`, met `functl` uit [github.com/fundament-oss/fundament](https://github.com/fundament-oss/fundament) op `/usr/local/bin`. De API-key komt uit de webconsole op https://console.fundament-poc.nl (gebruikersmenu rechtsboven, API keys) en landt in `~/.config/fundament/credentials`.

Let op voor de installatiedocumentatie later: dit is een rolling pre-release van `functl` die bij elke push naar master overschreven wordt. Nu `efb62de-master-21`.

## 1. Wat voor cluster dit is

| eigenschap | waarde | beantwoordt |
|---|---|---|
| distributie | Gardener shoot op metal-stack, vanilla Kubernetes | 3.1-1 |
| versie | v1.35.6 (node Ubuntu 24.04.4, containerd 2.1.5) | 3.1-1 |
| nodes | 1 worker, 32 CPU, 123 GiB geheugen | 3.2-8 |
| onze rechten | cluster-admin: CRD's, namespaces, clusterroles allemaal toegestaan | 3.2-4, 3.2-5 |
| policy-engine | geen. Nul ValidatingWebhookConfigurations. Alleen Gardener's eigen mutating webhooks plus de VPA-webhook | 3.1-2 |
| Pod Security | geen enforce/warn-labels op enige namespace, geen SCC | 3.1-3 |
| LimitRange / ResourceQuota | geen, op geen enkele namespace | 3.2-8 |
| multi-tenancy | geen Capsule, geen alternatief | 3.2-6 |
| egress | ClusterwideNetworkPolicy `allow-to-https` staat 443 naar `0.0.0.0/0` toe, idem http | 3.3-13, 3.4-16 |

**Geen OpenShift dus, en geen policy-engine.** Dat is de gunstige kant van de twee grote onzekerheden uit het hoofddocument. De egress-annotatie uit punt 2.2c (`egress.projectcalico.org/egressGatewayPolicy`) kan hier geen namespace weigeren, want er is niets dat weigert. Calico draait wel, maar zonder egressgateway-CRD en zonder admission-controle. De annotatie is hier dus inert in plaats van gevaarlijk.

Dat maakt punt 2.3-1 minder urgent dan het document inschat, maar niet minder juist: een zinloze annotatie op elke namespace blijft rommel, en de volgende cluster is misschien wel streng.

## 2. Wat er draait

| component | aanwezig | opmerking |
|---|---|---|
| Calico | ja | CNI plus NetworkPolicy, dus onze netwerkpolicies werken |
| MetalLB | ja | LoadBalancer-services krijgen echt een adres. Belangrijk voor de ingress, zie 4.1 |
| metal-stack firewall | ja | namespace `firewall`, egress geregeld via ClusterwideNetworkPolicy |
| metrics-server | ja | |
| VPA | ja, en werkend | CRD's plus webhook, en bestaande VPA's tonen `PROVIDED=True` met echte aanbevelingen |
| csi-lvm | ja | de enige StorageClass, tevens default |
| Gardener cert-extensie | ja | `certificates.cert.gardener.cloud`, niet `cert-manager.io` |
| Gardener DNS-extensie | ja | `dnsentries.dns.gardener.cloud`, nul entries, geen provider zichtbaar |
| VolumeSnapshot-CRD's | ja | door Gardener geïnstalleerd |
| CSIDriver-registraties | **geen** | nul objecten, zie 3.1 |
| VolumeSnapshotClass | **geen** | zie 3.1 |
| IngressClass | **geen** | er draait geen ingresscontroller |

Dat VPA werkt is de meevaller van deze inventaris. `supports_vpa: true` kan meteen aan, en de resource-tuner werkt vanaf dag één zonder dat wij een recommender hoeven mee te brengen.

Dat egress naar 443 openstaat is de tweede meevaller: **ghcr.io en docker.io zijn direct bereikbaar.** Geen registry-mirror, geen `rcr.rijksapps.nl`-verhaal, geen registry-rewrite-extensie. Stap 19 uit het hoofddocument wordt `extensions: []`, en punt 2.2k (de docker.io-defaults voor de console-images) hoeft niet overschreven te worden.

## 3. De gaten

### 3.0 Opslag werkt vandaag helemaal niet (gemeten 13 augustus)

Ernstiger dan 3.1 hieronder beschrijft, en pas zichtbaar bij het daadwerkelijk aanmaken van een PVC: **er kan op dit cluster geen enkele PersistentVolumeClaim gebonden worden.** De claim blijft `Pending` en de provisioner faalt met `create process timeout after 120 seconds`.

De oorzaak staat in de log van de provisioner-pod:

```
create vg with command: vgcreate -v csi-lvm /dev/nvme0n1 /dev/nvme1n1 --addtag vg.metal-stack.io/csi-lvm
Can't open /dev/nvme0n1 exclusively.  Mounted filesystem?
```

De node heeft twee NVMe-schijven van elk 3 TB. `nvme0n1` is de systeemschijf (drie partities, root op p3). `nvme1n1` is volledig vrij: geen partities, niet gemount. De controller draait met `CSI_LVM_DEVICE_PATTERN=/dev/nvme[0-1]n[0-9]`, en dat patroon pakt ze allebei. `vgcreate` probeert dus de systeemschijf op te nemen en faalt. Daarbovenop staat het type op `mirror`, wat minstens twee vrije schijven vraagt terwijl er één is.

Dit is niet van ons te repareren. Zowel de StorageClass als de controller dragen `resources.gardener.cloud/managed-by: gardener` met de annotatie "DO NOT EDIT - Any modifications are discarded and the resource is returned to the original state". De configuratie komt uit de metal-stack-extensie in de seed (`extension-controlplane-storageclasses`).

Zonder werkende opslag is ZAD hier niet uit te rollen: PostgreSQL, MinIO en Keycloak vragen allemaal een PVC.

**Het antwoord van platformbeheer (14 augustus): csi-lvm wordt niet gerepareerd, het gaat weg.** Opslag is nog niet stabiel op Fundament, clusters gaan standaard *zonder* StorageClass geleverd worden zodat je er zelf een kiest via het plugin-mechanisme, en er wordt gewerkt aan een storage-plugin op basis van Rook/Ceph. Dat komt overeen met hun eigen ADR-0015.

Dat maakt het een keuze van ons in plaats van een verzoek aan hen. De pluginlijst bevat vandaag `cert-manager`, `external-dns`, `gateway-api`, `openfsc` en `sandbox`, en dus nog geen storage.

**Wat we gekozen hebben: `local-path-provisioner`**, met het pad op `/var/lib/local-path-provisioner`. De default van die chart is `/opt/local-path-provisioner` en dat staat op de 24 GB rootpartitie; `/var/lib` zit op de grote partitie met 2,7 TB vrij. Gemeten schijfindeling van de node:

| substraat | grootte | staat |
|---|---|---|
| `/` (`nvme0n1p2`) | 24 GB, 21 vrij | te klein |
| `/var/lib` (`nvme0n1p3`) | 2,8 TB, 2,7 TB vrij | deelt met kubelet en containerd |
| `nvme1n1` | 3 TB | volledig vrij, rauw |

Dit is bewust de snelle en niet de mooie keuze: het doel was ZAD kunnen testen. Wat je ermee inlevert: geen snapshots, dus geen PVC-back-ups; geen volume-uitbreiding; en de data staat als gewone mappen op één node, dus bij een node-vervanging is hij weg. Prima om op te testen, niets om productiedata op te zetten.

De betere kandidaat voor later is **TopoLVM op `nvme1n1` met een thin pool**: dat geeft wel een VolumeSnapshotClass, gebruikt de eigen vrije schijf, en de node-afhankelijkheid (LVM) is aantoonbaar aanwezig. Longhorn viel af omdat het `open-iscsi` op het worker-image vraagt en dat beheren wij niet. Beide zijn hoe dan ook tijdelijk tot de Rook/Ceph-plugin er is; die brengt de snapshotclass mee en is dezelfde technologie als ODCN's ODF.

**Dit wordt niet teruggedraaid door Gardener.** De resource-manager reconcilieert alleen wat uit een ManagedResource in de seed komt, herkenbaar aan `resources.gardener.cloud/managed-by: gardener`. Onze componenten dragen dat label niet. Empirisch bevestigd: ingress-nginx en cert-manager draaiden 24 uur met nul herstarts, ongewijzigd. Het verschil met een `set env` op de gardener-beheerde csi-lvm-controller is fundamenteel: die werd binnen één seconde teruggezet.

### 3.1 Opslag en back-up: dit is het scherpste punt

`csi-lvm` is de enige StorageClass en is node-lokale LVM. Gemeten eigenschappen: provisioner `metal-stack.io/csi-lvm`, `WaitForFirstConsumer`, **geen** `allowVolumeExpansion`, geen parameters. Er zijn **nul CSIDriver-objecten** en **nul VolumeSnapshotClasses**, terwijl de VolumeSnapshot-CRD's er wel staan.

Drie gevolgen, oplopend in ernst:

1. **Geen volume-uitbreiding.** Een PVC die vol raakt kun je niet vergroten. Op ODCN kan dat wel.
2. **Geen ReadWriteMany.** Alleen RWO, net als ODCN, dus dit verandert niets aan wat we vandaag doen. Vraag 3.5-19 is hiermee beantwoord.
3. **Geen snapshots.** De CRD's aanwezig hebben is niet genoeg; er moet een CSI-driver zijn die snapshots kán, plus een class. csi-lvm registreert zichzelf niet eens als CSIDriver.

Op dat derde punt is het hoofddocument te somber. Vraag 3.5-20 zegt "de hele back-upketen leunt erop", en dat klopt niet. De back-upmodule heeft drie takken: `database_backup.py`, `bucket_backup.py` en `pvc_backup.py`, en **alleen die laatste maakt een VolumeSnapshot**. Databases en MinIO-buckets gaan rechtstreeks via Kopia naar S3.

Op fundament werken database- en bucketback-ups dus gewoon, en vallen alleen PVC-back-ups weg. Dat is een beperking, geen blokkade van de keten. De snapshot dient daar om een consistent moment te pakken zonder de draaiende applicatie te storen: `_backup_pvc` maakt een snapshot, kloont die naar een tijdelijke PVC, en laat een Kopia-pod díe kloon lezen.

Bovendien: **één node, en node-lokale opslag.** Alle data staat op dezelfde machine als waar de pods draaien. Dat is geen redundantie en geen HA. Voor een tweede productiecluster is dat een gesprek waard voordat er klantdata op staat, los van ZAD.

Wat te doen, in volgorde van voorkeur:

- **Vragen aan het platformteam** of er een andere StorageClass beschikbaar te maken is, met een echte CSI-driver en snapshot-ondersteuning. Dit is vraag 3.5-19 en 3.5-20, en het is nu een gerichte vraag in plaats van een open vraag. Het lost meteen ook het ontbreken van volume-uitbreiding op.
- Levert dat niets op, dan is er een terugval denkbaar die hier werkt maar elders niet: **de PVC rechtstreeks mounten**. RWO betekent toegang vanaf één node, niet vanaf één pod, en dit cluster heeft één node. Een back-uppod kan de PVC dus naast de applicatie mounten. Nadeel: geen consistent moment, de applicatie schrijft door tijdens het lezen. Voor bestandsopslag vaak acceptabel, voor iets databaseachtigs op een PVC niet. Niet bouwen voordat het platformantwoord er is.
- Stilleggen (workload naar 0, back-up, weer omhoog) geeft wel consistentie maar kost downtime bij elke back-up. Voor een productiecluster geen serieuze optie.

### 3.2 Ingress: opgelost, en zelf te doen (gemeten 13 augustus)

Bij het schrijven leek dit een platformvraag. Dat is het niet. Gemeten door het daadwerkelijk neer te zetten:

- Een `LoadBalancer`-service krijgt **automatisch een publiek IP**. De metal-ccm in de seed verwerft er een en configureert MetalLB zelf: er verschijnt een IPAddressPool `internet-fire-ephemeral` met een `/32`, aangekondigd via BGP vanaf de metal-stack firewall, niet via L2. Bij verwijderen van de service wordt het adres netjes vrijgegeven.
- ingress-nginx v1.15.1 (cloud-variant, niet de Kind-variant) draait en kreeg `194.135.48.69`. Van buitenaf bereikbaar op zowel 80 als 443.
- Een testdeployment met een Ingress erachter gaf HTTP 200 over het publieke adres, en de logs van die pod tonen ons eigen publieke IP als client. De hele keten van internet tot pod werkt dus.
- Die testpod draaide met exact de securityContext die onze gefixte template nu genereert voor een cluster zonder SCC (`runAsNonRoot` plus vastgepinde UID 1001 en fsGroup). Dat bevestigt punt 3.3 van de andere kant: het werkt hier, en zonder die fix zou het niet werken.
- cert-manager v1.21.1 is geïnstalleerd en gezond.

Wat dus overblijft van deze paragraaf is alleen nog de DNS-kant, en die is van ons: zie hieronder. Het ephemere karakter van het IP is wel een aandachtspunt. Voor een echte ingress wil je een statisch adres, anders verschuift het bij herbouw en klopt DNS niet meer. Of dat via `functl` aan te vragen is, is nog niet uitgezocht; de CLI toont geen `ip`-commando.

Op het cluster staan nu ingress-nginx en cert-manager. Beide werkend, geen van beide onder GitOps.

### 3.2b Wat er nog wel open is: DNS-zone en cert-pad

Er is geen IngressClass en geen controller. Er zijn nul DNSEntries en er is geen zichtbare DNS-provider. Er zijn nul Gardener-certificaten.

Dat betekent dat vragen 3.3-9 tot en met 3.3-11 en 3.3-14 nog volledig openstaan, en dat ze samen één ding vormen: er is nog geen enkele manier waarop verkeer van buiten bij een pod komt. Wij kunnen zelf een ingresscontroller neerzetten (we zijn cluster-admin, en MetalLB kan een adres leveren), maar dat lost de twee andere helften niet op:

- **Welke DNS-zone krijgen wij, en met een wildcard?** Zonder wildcard is elke deployment een aanvraag. `ingress_postfix` en `nice_url.supported_domains` kunnen niet ingevuld worden voordat dit er is.
- **Hoe komen we aan certificaten?** Het cluster brengt de Gardener cert-extensie mee (`cert.gardener.cloud`), niet cert-manager. Onze manifests genereren cert-manager-resources. Dat is een keuze: cert-manager er zelf naast zetten (mag, we zijn admin), of de Gardener-extensie gebruiken en de templates daarop aanpassen. Het eerste is verreweg het goedkoopst en houdt fundament gelijk aan ODCN en de sandbox.

Let ook op dat het punt uit 2.2d hier scherper wordt dan het document schat: op nginx doen de HAProxy-annotaties niets, en `ip_whitelist` is er één van. Als het antwoord op vraag 3.3-12 "afgeschermd" is, dan is punt 2.3-8 niet meer voorwaardelijk maar verplicht, want anders denken we afgeschermd te zijn en zijn we het niet.

### 3.3 Geen SCC betekent dat 2.2e gaat bijten

`manifests/pod-security-context.yaml.jinja:8` test op een lijst clusternamen. Een nieuwe clustersleutel valt automatisch in de else-tak: `runAsNonRoot: true` zonder UID, want "op OpenShift wijst de SCC er een toe".

Hier is geen SCC en geen Pod Security Admission. Pods die geen non-root `USER` in hun image declareren krijgen dus `CreateContainerConfigError`. Het hoofddocument voorspelt dit voor de db-console- en job-pods.

Punt 2.3-3 is daarmee niet "hoog" maar feitelijk blokkerend voor dit cluster, en het is nu bevestigd in plaats van vermoed.

### 3.4 Wat ZAD verder zelf moet meebrengen

Niets van de ZAD-stack staat er. Dat is op zich prima, want in de sandbox zetten we het ook allemaal zelf neer. Voor de volledigheid, afgezet tegen `infrastructure/bootstrap/clusters/odcn/kustomization.yaml`:

| ZAD heeft nodig | staat op fundament | hoe we het in de sandbox doen |
|---|---|---|
| ArgoCD | nee | `prepare-argocd-operator` + `bootstrap-argo-system` (operator v0.14.0) |
| CloudNativePG | nee | `install-cnpg-operator` (1.27.3) |
| ingresscontroller | nee | `install-ingress-nginx` (v1.15.1, maar de Kind-variant) |
| cert-manager | nee, wel Gardener-certs | sandbox gebruikt nep-CRD's plus een geïmporteerd wildcard-cert |
| VolumeSnapshot-CRD's | ja, al aanwezig | `sandbox:install-csi-snapshot` is hier dus niet nodig |
| PostgreSQL (rig-db) | nee | `postgresql/database/overlays/<cluster>` |
| Keycloak | nee | `keycloak/controller` + `keycloak/config` |
| MinIO | nee | `minio/controller` + `minio/config` |
| Redis | nee | `redis/controller` |
| Prometheus | nee | `prometheus/controller` |
| external-dns | nee | `external-dns/controller`, maar zie 3.2: eerst moet de DNS-vraag beantwoord zijn |
| Capsule | nee | draait ook niet in de sandbox; `uses_capsule: false` |
| VPA-recommender | **ja, werkend** | in de sandbox niet aanwezig; hier dus beter dan de sandbox |

## 4. Wat we zouden installeren, en hoe

Dit is de volgorde uit paragraaf 4.3 van het hoofddocument, ingevuld met wat hier echt nodig is. Nog niet uitvoeren: stap 0 en de vragen uit 3.1 en 3.2 gaan hieraan vooraf.

### 4.0 Eerst dit, anders is de rest onveilig

De drie blokkerende punten zijn **gedaan** op branch `claude/fundament`, elk als losse commit. Ze staan hier omdat ze voorwaarde zijn voor alles daaronder, en omdat twee van de drie groter bleken dan het hoofddocument schatte.

1. **AGE-sleutel per cluster** (punt 2.3-2), commit `06e73e3d`. Het hoofddocument noemt drie plaatsen; het zijn er zes. Naast de ternair op `842`, `924` en `974` doen `604`, `653` en `1457` hetzelfde via `{{default "security/key.txt" .KEY_FILE}}`, waaronder `bootstrap-backup-destination`. De keuze komt nu uit `AGE_KEY_FILE` in `.env-taskfile-<cluster>`, bewust zonder terugval: een precondition stopt de taak als de waarde ontbreekt. Een expliciete `KEY_FILE` blijft winnen, dus de sandbox-aanroepen werken ongewijzigd.
2. **UID op een clustereigenschap** (punt 2.3-3), commit `9ec64acb`. Ook hier drie templates in plaats van één, en het oordeel over `deployment.yaml.jinja` in het hoofddocument klopt niet: zie 3.3. De keuze zit nu in `cluster_config` als `assigns_uid_via_scc`, hetzelfde patroon als `uses_capsule` en `supports_vpa`. Geen enkel gerenderd veld verandert voor de bestaande drie clusters; de goldens wijzigen alleen in commentaar.
3. **Snapshotclass uit één bron** (punt 2.3-4), commit `246e1cbe`. `BACKUP_SNAPSHOT_CLASS` en de dataclass-default zijn weg, `get_volume_snapshot_class` is de enige bron. Leegmaken alleen was niet genoeg: een lege waarde rendert `volumeSnapshotClassName: ""`, en die snapshot wordt nooit ready, dus de run liep de volle timeout vol. `_backup_pvc` stopt nu vooraf met een melding die zegt welk cluster het betreft en dat database- en bucketback-ups wel werken.

Punt 2.3-1 (de egress-annotatie) is hier ongevaarlijk en is bewust blijven staan, zie paragraaf 1.

### 4.1 Ingresscontroller

Niet de Kind-variant gebruiken. `bootstrap/rig-system/kustomize/ingress-nginx/base` haalt `deploy/static/provider/kind/deploy.yaml` en patcht hostPort-gedrag omdat een Kind-cluster poortmappings op de control-plane node heeft. Hier is MetalLB aanwezig, dus de juiste keuze is de cloud-variant met een echte LoadBalancer-service:

```
kubectl --context zad-fundament apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.15.1/deploy/static/provider/cloud/deploy.yaml
```

Daarna controleren dat de service een adres van MetalLB krijgt, en dat adres is wat de DNS-zone moet aanwijzen. Dit vraagt dus een eigen `overlays/fundament/` naast de bestaande base, niet een hergebruik van de Kind-base.

### 4.2 CloudNativePG

Identiek aan de sandbox, geen aanpassing nodig:

```
kubectl --context zad-fundament apply --server-side -f https://raw.githubusercontent.com/cloudnative-pg/cloudnative-pg/release-1.27/releases/cnpg-1.27.3.yaml
```

De namespace wordt `cnpg-system`, wat overeenkomt met `database_operator_namespace` in het clusterblok.

### 4.3 CSI-snapshots

`sandbox:install-csi-snapshot` overslaan: de CRD's staan er al. Maar zie 3.1, de CRD's zijn hier niet het probleem.

### 4.4 cert-manager

Als we voor cert-manager kiezen in plaats van de Gardener-extensie, is dit een echte installatie en niet de nep-CRD's uit `bootstrap/crd/cert-manager/fake-cert-manager.yaml`. Die bestaan alleen zodat ArgoCD in de sandbox niet struikelt over onbekende types; er draait daar geen cert-manager.

Beslis dit expliciet, want het raakt `issuer-letsencrypt.yaml.jinja` en de hele TLS-keten. Voorstel: cert-manager, omdat het de templates ongemoeid laat.

### 4.5 ArgoCD

```
task prepare-argocd-operator      # operator v0.14.0
task bootstrap-argo-system        # met KEY_FILE naar de fundament-sleutel
```

Let op dat `bootstrap-argo-system` de AGE-sleutel meekrijgt die bij dit cluster hoort, zie 4.0-1.

### 4.6 De infrastructuur zelf

`infrastructure/bootstrap/clusters/fundament/kustomization.yaml` samenstellen. Begin met de negen die op ODCN echt draaien, en let op de twee dingen die het hoofddocument in 2.2n en 2.2o noemt:

- De vijf plaatsen waar `ocs-storagecluster-ceph-rbd` staat worden hier `csi-lvm`.
- De backup-networkpolicy selecteert op `capsule.clastix.io/tenant: rig-prd`. Er is hier geen Capsule, dus die selector matcht niets en de policy faalt dicht: de back-uppods komen niet bij MinIO. Deze overlay moet een eigen selector krijgen, bijvoorbeeld op een namespacelabel. Dit is geen kopieerwerk.

### 4.7 OPI

Als laatste, want de configmap wijst naar database, MinIO, Redis en Keycloak die er dan pas staan. De checklist voor die configmap is punt 2.2l van het hoofddocument.

## 5. Wat dit oplevert voor de vragenlijst

Beantwoord door meting, dus deze hoeven niet meer gevraagd:

- 3.1-1 vanilla Kubernetes 1.35.6, Gardener op metal-stack
- 3.1-2 geen policy-engine
- 3.1-3 geen PSS, geen SCC, geen UID-toewijzing
- 3.2-4 wij hebben cluster-admin
- 3.2-5 wij mogen CRD's en operators installeren
- 3.2-6 geen multi-tenancy aanwezig
- 3.2-8 geen LimitRange of ResourceQuota
- 3.3-13 egress naar internet is open op 80 en 443
- 3.4-15 t/m 3.4-18 geen mirror nodig, ghcr en docker.io zijn bereikbaar
- 3.5-19 alleen RWO, node-lokale LVM, geen volume-uitbreiding
- 3.5-20 geen VolumeSnapshotClass en geen CSI-driver die het kan
- 3.7-29 VPA draait en levert aanbevelingen

Nog steeds open, en nu de kritieke pad:

- 3.3-9 t/m 3.3-12 en 3.3-14: ingress, DNS-zone, wildcard, external-dns, certificaten, en of het portaal publiek mag. Dit is nu één samenhangende vraag en geen vijf losse.
- 3.5-20 vervolg: is er een andere StorageClass te krijgen met snapshot-ondersteuning?
- 3.5-21 waar landt de back-updoelbestemming
- 3.6-23 t/m 3.6-25: één Keycloak of twee, ongewijzigd een ontwerpbeslissing
- 3.7-26 t/m 3.7-28: er is geen Prometheus of Grafana op dit cluster, dus die brengen we zelf mee. De vraag is of het platform onze pods al scrapet
- 3.8-30 t/m 3.8-32: de git-repo's, ongewijzigd

En de beslissingen A tot en met E en J uit paragraaf 5.7 staan nog steeds allemaal open. Niets in deze meting raakt ze.

## 6. Wat opvalt

Het cluster is vriendelijker dan het hoofddocument vreesde. Geen OpenShift, geen policy-engine, geen SCC, cluster-admin, open egress, werkende VPA. De helft van de "dit kan de installatie laten stranden"-punten valt weg omdat er simpelweg niets is dat weigert.

Daar staat één ding tegenover dat zwaarder weegt dan alle vervallen punten samen: **de opslag.** Eén node, node-lokale LVM, geen uitbreiding, geen snapshots. Dat raakt back-up direct en HA fundamenteel. Als het antwoord op 3.5-19 blijft "csi-lvm en verder niets", dan is dit geen tweede productiecluster maar een testomgeving, en dan hoort dat besluit expliciet genomen te worden in plaats van er per ongeluk in te rollen.

## 7. Wat er nu staat: het clusterblok en de installatietaak

Clustersleutel **`fundament-poc`**, DNS-zone **`.fundament-poc.rijksapp.dev`**. Beide zijn keuzes, geen gegevenheden; de sleutel bepaalt vijf bestandsnamen, dus hernoemen is werk.

### 7.1 Het clusterblok

`CLUSTER_CONFIG["fundament-poc"]` in `opi/core/cluster_config.py`. Elke waarde is op het cluster gemeten en niet van een ander cluster overgenomen. De opvallende:

| sleutel | waarde | grond |
|---|---|---|
| `supports_vpa` | `True` | VPA draait en levert aanbevelingen, anders dan op de andere niet-ODCN clusters |
| `uses_capsule` | `False` | geen Capsule aanwezig |
| `assigns_uid_via_scc` | `False` | geen SCC en geen Pod Security Admission |
| `storage.storage_class_name` | `local-path` | zie paragraaf 3.0 |
| `storage.volume_snapshot_class` | ontbreekt bewust | local-path kan het niet; PVC-back-ups melden dat en stoppen |
| `extensions` | `[]` | egress open op 443, dus geen registry-mirror nodig |
| `namespace_metadata` | leeg | geen policy-engine die iets van een namespace verlangt |
| `ingress_controller_selector` | ns `ingress-nginx`, label `app.kubernetes.io/name` | gemeten op de draaiende controller |

Dat `storage_class_name` expliciet gezet wordt, lost meteen een probleem op: `csi-lvm` staat nog als default gemarkeerd en blijft stuk, maar ZAD vraagt nooit om de default en raakt hem dus niet aan.

### 7.2 De installatietaken

Drie taken, allemaal idempotent. Opnieuw draaien is veilig en de aangewezen manier om bij te werken; dat is geen luxe, want bij een node-vervanging moet dit opnieuw.

- **`task fundament:check`** controleert de voorwaarden: staat `functl` op de PATH, is hij ingelogd, bestaat de context, en wijst die context echt naar een Fundament-cluster. Dat laatste is de belangrijkste: zonder die controle landt een installatie op wat er toevallig in `kubectl config current-context` staat.
- **`task fundament:install-platform`** installeert de laag die het platform niet levert: ingress-nginx v1.15.1 (cloud-variant, niet de Kind-variant), cert-manager v1.21.1, local-path-provisioner v0.0.37 inclusief de padwijziging, en CloudNativePG 1.27.3. Alle versies gepind, niet `latest`.
- **`task fundament:verify`** bewijst dat het werkt: de ingresscontroller heeft een adres, een PVC bindt en is beschrijfbaar door een non-root pod met exact de securityContext die `deployment.yaml.jinja` hier rendert, en cert-manager en CNPG draaien. De testresources worden altijd opgeruimd, ook als de taak faalt.

Gedraaid op 14 augustus, tweede keer over bestaande componenten, alles groen. Ingressadres `194.135.48.69`.

### 7.3 Wat hierna nog moet

- Een A-record `router.fundament-poc.rijksapp.dev` naar het ingressadres. Let op dat dat adres ephemeer is: het verschuift als de LoadBalancer-service herbouwd wordt.
- De AGE-sleutel `security/fundament-poc-key.txt` genereren en de secrets ermee versleutelen.
- De overlays: `infrastructure/bootstrap/clusters/fundament-poc/` en de twee onder `bootstrap/rig-system/kustomize/`. Begin bij de componenten die op ODCN echt draaien, en let op de twee punten uit 2.2n en 2.2o van het hoofddocument: de storageclass staat op vijf plaatsen, en de backup-networkpolicy selecteert op een Capsule-tenantlabel dat hier niets matcht.
- external-dns, bij voorkeur als Fundament-plugin in plaats van uit onze eigen tree.
- Overwegen om cert-manager om te zetten naar de Fundament-plugin, zodat het platformbeheerd is in plaats van iets van ons.

## 8. Twee dingen om te onthouden bij het beheren van dit cluster

### 8.1 Gebruik `deploy-operations-manager` hier niet

Die taak doet `kustomize build | kubectl apply` op dezelfde overlay die ArgoCD sinds de app-of-apps bezit. Met `selfHeal: true` wint ArgoCD, dus je wijziging wordt binnen een minuut teruggedraaid en je bent aan het vechten met de reconciler zonder dat er iets fout lijkt te gaan.

Op dit cluster is de weg: commit naar de `fundament`-branch, ArgoCD synchroniseert. De taak blijft nuttig voor clusters waar OPI niet in GitOps zit.

### 8.2 De configuratie van OPI komt volledig uit git, de bron van de secrets niet

Wat OPI meekrijgt zit in de wave-4 Application: een ConfigMap met 28 variabelen (waaronder `CLUSTER_MANAGER=fundament-poc`, dus hij weet welk cluster hij beheert) en een SOPS-versleuteld secret met 21 sleutels. Beide staan in git.

Wat niet in git staat is `operations-manager/python/.env.fundament-poc.secrets`, de platte bron waaruit `generate-env-secrets-for-operations-manager` dat secret maakt. Dat is de goede kant op: platte secrets horen niet in git. Maar het betekent wel dat dat bestand op één laptop staat.

Raakt het kwijt, dan zijn de waarden niet verloren: het versleutelde secret in git is te ontsleutelen met `security/fundament-poc-key.txt`. De echte enkelvoudige afhankelijkheid is dus die AGE-sleutel, en die staat ook maar op één plek. Dat geldt voor elk cluster, maar hier is het nieuw en dus het opschrijven waard.
