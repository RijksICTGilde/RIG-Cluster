# Een nieuw cluster installeren

Status: uitzoekdocument, 12 augustus 2026. Aanleiding: ZAD draait vandaag op ODCN. Er komt een
tweede productiecluster bij, werknaam **fundament**. We weten nog niet wat daar aan
platformdiensten staat, wie er beheert, en of het portaal daar publiek bereikbaar mag zijn.

Dit document is de inventaris en het stappenplan dat daaraan voorafgaat. Het bevat **geen
codewijzigingen**; wat er aan code moet gebeuren staat hieronder als lijst, met bestand en
regelnummer, zodat het als losse taken te shippen is.

Alle regelnummers zijn gemeten op commit `cf6c2485`.

> **Namen liggen niet vast.** `fundament-production` als clustersleutel, `rig-fnd-` als
> namespaceprefix en `fundament` als mapnaam zijn in dit document **voorstellen**, geen
> besluiten. Ze zijn hier gebruikt om de tekst leesbaar te houden. De echte namen komen uit
> het gesprek met het fundament-platformteam (paragraaf 3) en uit de naamgevingsafspraak die
> ODC-Noord hanteert.

---

## 1. De drie lagen

Een cluster zit in ZAD op drie plaatsen tegelijk. Ze zijn onafhankelijk van elkaar: je kunt
laag 1 doen zonder laag 2, en dan werkt er nog niets. Voor fundament moeten alle drie.

### Laag 1 — de code (`operations-manager/python/opi/`)

`opi/core/cluster_config.py` is 1133 regels: één `CLUSTER_CONFIG`-dict met drie sleutels
(`local` op regel 12, `sandboxed-local` op regel 76, `odcn-production` op regel 130) en 48
accessorfuncties eromheen. **56 bestanden onder `opi/` importeren eruit.**

Dit is het bedoelde inhaakpunt. Een vierde sleutel in `CLUSTER_CONFIG` erbij, en het meeste
volgt vanzelf, omdat vrijwel alle clusterafhankelijke code via die accessors loopt en niet via
een `if cluster == "odcn-production"`.

**Wat er voor een nieuw cluster bij moet:** één nieuw blok in `CLUSTER_CONFIG`, en een oordeel
over elk punt in paragraaf 2.2 (de waarden die ODCN-specifiek zijn maar *buiten* dat blok
staan). Dat tweede deel is het werk; het eerste is invullen.

### Laag 2 — kustomize (`bootstrap/` en `infrastructure/`)

Twee bomen, allebei met een map per cluster:

| boom | mappen vandaag | wat erin zit |
|---|---|---|
| `bootstrap/rig-system/kustomize/overlays/` | `local`, `odcn-production`, `sandboxed-local` | ArgoCD-deployment, namespace, repo-secrets, netwerkbeleid |
| `bootstrap/rig-system/kustomize/operations-manager/overlays/` | `local`, `odcn-production`, `sandboxed-local`, `sandboxed-local-debug`, `sandboxed-local-dev` | OPI zelf: ingress, configmap, networkpolicy, letsencrypt-issuer, billing-prometheusrule, image-pin |
| `infrastructure/bootstrap/clusters/` | `local`, `odcn`, `sandboxed-local` | de lijst van infracomponenten die op dat cluster meegaan |

Onder `infrastructure/bootstrap/infrastructure/` staan **20 `overlays/`-mappen met samen 40
clusteroverlays**: 15 voor `local`, 16 voor `odcn`, 9 voor `sandboxed-local`.

> Correctie op het uitzoekplan: dat sprak van "80 overlaymappen". Gemeten zijn het er 40
> (`find infrastructure/bootstrap/infrastructure -type d -name overlays` geeft 20 `overlays/`-
> mappen, met samen 40 kinderen).

De odcn-overlaymappen (16) zijn niet allemaal in gebruik:
`infrastructure/bootstrap/clusters/odcn/kustomization.yaml` heeft **vault** (regels 7-8) en
**pgadmin** (regels 11-12) uitgecommentarieerd. Daadwerkelijk uitgerold op ODCN zijn **9**
componenten: secrets/config, postgresql/database, keycloak (controller+config), minio
(controller+config), prometheus/controller, redis/controller, external-dns/controller.
`backup-destination`, `postgresql/config` en `redis/config` hebben wel een odcn-overlay maar
staan niet in de clusterlijst.

**Wat er voor een nieuw cluster bij moet:** één nieuwe map in elk van de drie bomen, plus een
overlay per infracomponent dat meegaat. Begin met de 9 die op ODCN echt draaien, niet met de
mappenlijst.

### Laag 3 — de taakketen (`Taskfile.yaml` en `.env-taskfile-*`)

- `.env-taskfile-local`, `.env-taskfile-odcn-production`, `.env-taskfile-sandboxed-local`.
  Elk zet `CLUSTER_TYPE`, `KIND_CLUSTER_NAME`, `RIG_NAMESPACE`,
  `INFRASTRUCTURE_CLUSTER_FOLDER` en `BOOTSTRAP_CLUSTER_FOLDER`.
  **Valkuil:** ODCN gebruikt twee verschillende mapnamen — `INFRASTRUCTURE_CLUSTER_FOLDER=odcn`
  maar `BOOTSTRAP_CLUSTER_FOLDER=odcn-production`. Wie voor fundament één naam aanneemt, krijgt
  een kustomize-build die naar een niet-bestaande map wijst.
- `Taskfile.yaml:33` `select-cluster`: de drie clusters staan als hardgecodeerde `case`-takken
  (regels 49-61) met een hardgecodeerde menutekst (regels 41-43) en prompt `[1-3]` (regel 45).
  Een vierde erbij is handwerk op vier plaatsen in dezelfde taak.
- De AGE-sleutels, zie 2.3.
- `Taskfile.yaml:1209` `publish-operations-manager` en `Taskfile.yaml:1249`
  `pin-operations-manager-image` kennen alleen het odcn-pad.

---

## 2. De inventaris

Per punt: waar het staat, wat het is, en het oordeel — **naar configuratie** of **blijft
staan**.

### 2.1 Wat al in `cluster_config.py` staat (invullen, geen code)

Dit hoeft alleen beantwoord te worden voor fundament; de code leest het al via een accessor.

| sleutel | regel (odcn-blok) | ODCN-waarde | wat het van het cluster vraagt |
|---|---|---|---|
| `ingress_postfix` | `cluster_config.py:131` | `.rig.prd1.gn2.quattro.rijksapps.nl` | een defaultzone die de ingresscontroller serveert |
| `namespace_prefix` / `namespace` / `argo_namespace` | `:132`, `:133`, `:134` | `rig-prd-` / `rig-prd-operations` | naamgevingsafspraak met het platformteam |
| `keycloak_discovery_url` | `:135` | `https://keycloak.rijksapp.nl` | zie 3.6 (één Keycloak of twee) |
| `database_server` / `minio_host` / `minio_port` / `redis_server` | `:136`-`:139` | `*.rig-prd-operations.svc.cluster.local` | de platformdiensten in dát cluster |
| `backup_namespace` | `:140` | `rig-prd-backup` | waar de back-updoelbestemming landt |
| `database_operator_namespace` | `:142` | `cnpg-system` | waar CloudNativePG draait (NetworkPolicy laat die ingress toe) |
| `ingress_controller_selector` | `:143`-`:148` | ns `openshift-ingress`, podlabel `ingresscontroller.operator.openshift.io/deployment-ingresscontroller: rig` | de NetworkPolicy moet de juiste router kunnen aanwijzen |
| `ingress.enable_tls` / `ingress.ip_whitelist` | `:149`-`:153` | `true` / `0.0.0.0/0,::/0` | of het portaal publiek mag (zie paragraaf 5) |
| `storage` | `:154`-`:158` | `ocs-storagecluster-ceph-rbd`, RWO, `ocs-storagecluster-rbdplugin-snapclass` | een RWO-storageclass én een VolumeSnapshotClass |
| `keycloak.support_http` | `:159`-`:161` | `false` | alleen HTTPS-redirect-URI's |
| `min/max_memory_*`, `min/max_cpu_*` | `:162`-`:168` | 25 Mi tot 4096 Mi, 25m tot 4000m | quota- en LimitRange-grenzen van het cluster |
| `uses_capsule` | `:165` | `true` | multi-tenancy waar OPI op wacht bij namespace-creatie |
| `supports_vpa` | `:169` | `true` | een draaiende VPA-recommender, waar de resource-tuner op leunt |
| `letsencrypt.contact_email` | `:170`-`:172` | `rig-platform@rijksoverheid.nl` | contactadres voor de ACME-account |
| `nice_url.supported_domains` | `:173`-`:197` | `rijks.app`, `rijksapp.nl`, `rijksapp.dev`, elk met `external_dns_target` | eigen DNS-zones plus een werkende external-dns |
| `extensions` | `:198` | `["odcn-registry-rewrite"]` | een registry-mirror; ghcr is op ODCN geblokkeerd |
| `create_wizard_clusters` | alleen in `local` (`:16`) | — | ontbreekt bewust in productie: de wizard biedt dan alleen het eigen cluster |

Oordeel voor alle regels in deze tabel: **staat al goed** — het is een invulopgave, geen
codewijziging. De vragen om ze te kunnen invullen staan in paragraaf 3.

### 2.2 ODCN-waarden die *buiten* `cluster_config` staan

Dit is het gevaarlijke deel: een nieuw cluster erft ze stilzwijgend.

#### a. `opi/core/config.py:438` — `BACKUP_SNAPSHOT_CLASS: str = "ocs-storagecluster-rbdplugin-snapclass"`

Een ODCN/OpenShift-Data-Foundation-naam als **default van een globale setting**. Op een cluster
zonder ODF bestaat die VolumeSnapshotClass niet en faalt elke snapshot-back-up, zonder dat
iemand de setting bewust heeft aangeraakt.

**Oordeel: naar configuratie.** De waarde staat al per cluster in
`cluster_config.py:157` (`storage.volume_snapshot_class`). Dit is een tweede bron voor
dezelfde waarde. Laat de setting default `""` zijn en val terug op de clusterwaarde, of laat
hem weg. *Losse taak.*

#### b. `opi/manager/backup/base.py:467` — `snapshot_class: str = "ocs-storagecluster-rbdplugin-snapclass"`

Dezelfde string, nu als defaultargument in een dataclass/functiesignatuur — een **derde** bron.

**Oordeel: naar configuratie**, samen met (a). Eén bron: `get_storage_config(cluster)`.
*Losse taak, hoort bij (a).*

#### c. `manifests/namespace.yaml.jinja:10` — `egress.projectcalico.org/egressGatewayPolicy: "internet"`

Staat onvoorwaardelijk op **elke** gegenereerde namespace, met er letterlijk boven de notitie
`# NOTE: we need manifests per cluster` (regel 8). Volgens `docs/ron-koppeling.md:37` is dit op
ODCN een door **Kyverno** gecontroleerde annotatie met een beperkte waardenlijst (`internet`
of een klantgateway `rig-*`), en "bij een foutieve annotatie wordt de namespace onbruikbaar".

Op een cluster zonder Calico-egressgateway is de annotatie zinloos; op een cluster met een
andere policy-engine of een andere waardenlijst kan hij de namespace **weigeren**. Dit is het
punt dat een fundament-installatie het hardst kan laten stranden.

**Oordeel: naar configuratie.** Een clustersleutel `namespace_annotations: dict` (leeg voor
clusters die niets willen), gelezen in de template. Niet als vaste sleutel `egress_policy`,
want de volgende platformafspraak heet anders. *Losse taak — de eerste die af moet.*

#### d. `manifests/ingress.yaml.jinja:15,16,20,23,28` — `haproxy.router.openshift.io/*` en `route.openshift.io/termination`

Vijf OpenShift-Router-annotaties op **elke** gegenereerde Ingress: `ip_whitelist`, `timeout`,
`termination: passthrough`, `rewrite-target`, `hsts_header`. Direct daaronder (regels 30-35)
staan de nginx-equivalenten. `manifests/issuer-letsencrypt.yaml.jinja:22` heeft dezelfde
`ip_whitelist`-annotatie.

Nginx negeert onbekende annotaties, dus dit **breekt niets** op een vanilla-cluster — het
zwijgt alleen. Het gevaar is een ander: `ip_whitelist` en `hsts_header` zijn
**beveiligingsinstellingen**. Op ODCN doet de HAProxy-annotatie het werk; op een nginx-cluster
doet ze niets en is er geen nginx-equivalent voor `ip_whitelist` aanwezig. Een cluster dat op
IP wil afschermen (zie 5.1) denkt dan beschermd te zijn en is dat niet.

**Oordeel: blijft staan, met één uitzondering.** De annotaties zelf mogen blijven: ze zijn
inert op nginx en het alternatief (een per-cluster if in vijf takken) maakt de template
onleesbaar. **Maar** `ip_whitelist` moet een equivalent krijgen zodra een cluster hem echt
gebruikt, anders is het een stille beveiligingsgat. Zet dat pas om als het antwoord op vraag
3.3 daarom vraagt. *Losse taak, voorwaardelijk.*

#### e. `manifests/pod-security-context.yaml.jinja:8` — een hardgecodeerde clusternamenlijst

```jinja
{% if cluster in ['local', 'sandboxed-local'] %}
    runAsNonRoot: true
    runAsUser: 1001
    ...
{% else %}
    runAsNonRoot: true
{% endif %}
```

De commentaarregel erboven zegt het doel: "On Kind clusters we pin a numeric UID (no SCC); on
OpenShift we let the SCC assign one." De **implementatie** is echter een lijst met clusternamen,
geen eigenschap. Een nieuw cluster valt automatisch in de `else`-tak en krijgt dus
"laat de SCC een UID toewijzen". Op een vanilla-Kubernetes fundament is er **geen** SCC: de pod
krijgt `runAsNonRoot: true` zonder UID, en start alleen als het image zelf een non-root `USER`
declareert. Doet het dat niet, dan is het een `CreateContainerConfigError` op de
db-console- en job-pods — niet op de gewone deployments.

**Oordeel: naar configuratie.** Een clustersleutel `assigns_uid_via_scc: bool` (of de inverse),
en de template test daarop in plaats van op namen. Dit is precies het patroon dat
`uses_capsule` en `supports_vpa` al volgen. *Losse taak.*

`manifests/deployment.yaml.jinja:62-64` is een ander geval: dáár is 1001 een **default** die het
projectbestand kan overschrijven (`opi/manager/project_manager.py:5716-5719` leest
`run-as-user`/`run-as-group`/`fs-group` uit de component-security). Dat is een bewuste keuze.
**Oordeel: blijft staan** — er is een ontsnapping en die wordt gebruikt.

#### f. `opi/web/router_usage.py:57` — `prometheus!="openshift-monitoring/k8s"`

Een filter in de PromQL van de gebruiksweergave, dat de OpenShift-platform-Prometheus uitsluit
om dubbeltelling te voorkomen bij twee scrapers over dezelfde metriek.

Op een cluster zonder `openshift-monitoring` matcht het label nooit en is het filter een no-op.
Het **breekt niets** en is niet fout. Het is wel een aanname die stil verkeerd kan worden: als
fundament twee scrapers over dezelfde metriek heeft onder een *andere* naam, telt de weergave
dubbel en merkt niemand het.

**Oordeel: blijft staan.** Noteren als aanname, meten zodra fundament een Prometheus heeft
(vraag 3.7). Niet vooraf configureerbaar maken — dat is een abstractie voor één geval.

#### g. `opi/connectors/kubectl.py:631` — `wait_for_capsule_tenant_label`

Wacht tot Capsule's admission-webhook het tenantlabel op de namespace heeft gezet, aangeroepen
vanuit `opi/manager/project_manager.py:1954-1959` **achter** `if uses_capsule(...)`.

**Oordeel: blijft staan.** Dit is al correct gebouwd: de vlag zit in `cluster_config`
(`:165`), de code test erop. Een cluster zonder Capsule slaat het over.

#### h. `opi/core/cluster_config.py:627` — verwijzing naar `docs/knowledge/odcn-ingress-controller.md`

De docstring van `get_ingress_controller_selector` verwijst naar dat bestand. **`docs/knowledge/`
bestaat niet** in deze repo; het bestand ook niet.

**Oordeel: op te ruimen, buiten deze taak.** Niet gerepareerd hier. Wie de vraag "hoe kies ik
het ingresscontroller-label voor fundament?" beantwoordt (3.3), schrijft die kennis alsnog op —
en dan is dit de plek waar hij hoort.

#### i. `infrastructure/bootstrap/infrastructure/external-dns/controller/base/deployment.yaml:28`

```yaml
image: rcr.rijksapps.nl/k8s-rig/external-dns/external-dns:v0.15.0
```

De ODCN-registry-mirror staat in de **base**, niet in een overlay. Elk cluster dat external-dns
uit deze boom haalt, trekt het image uit `rcr.rijksapps.nl` — een registry die alleen vanaf
ODCN bereikbaar is.

**Oordeel: naar configuratie.** Base op de upstream-referentie
(`registry.k8s.io/external-dns/external-dns:v0.15.0`), en de odcn-overlay patcht de mirror
erin. Dit is de bestaande conventie; hier is hij één keer overgeslagen. *Losse taak.*

#### j. `opi/api/router.py:3386` — `"url": "rcr.rijksapps.nl/rig"`

Een ODCN-registry-URL in een API-antwoord. Naar behoren hoort dit uit `settings.REGISTRY_URL`
(`opi/core/config.py:291`) te komen, dat de odcn-configmap wél zet
(`bootstrap/.../odcn-production/configmap.yaml:53`).

**Oordeel: naar configuratie.** Lees `settings.REGISTRY_URL`/`REGISTRY_ORG`. *Losse taak.*
Let op: dit is een hardcode in een codepad, niet in een template — hij overleeft dus ook een
correcte fundament-configmap.

#### k. `opi/core/config.py:501-502` — `DB_CONSOLE_PGWEB_IMAGE` / `DB_CONSOLE_DBGATE_IMAGE`

Defaults zijn docker.io-referenties, met er letterlijk boven (`:498-500`): "production overlays
MUST override these with a mirror reachable on the cluster (e.g. rcr.rijksapps.nl), since
docker.io/ghcr are blocked on ODCN."

**Oordeel: blijft staan** — het comment doet zijn werk en de default is de *neutrale* kant, niet
de ODCN-kant. Wel opnemen in de installatiechecklist (paragraaf 4): als fundament ook geen
docker.io mag, moet de fundament-overlay ze zetten, en dan valt het op als je het vergeet
(de console start niet).

#### l. De odcn-configmap zelf — `bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/configmap.yaml`

Niet ODCN-*lekkage*, maar de complete lijst van wat een fundament-configmap moet beantwoorden.
De ODCN-specifieke regels: `OWN_DOMAIN` (`:28`), `ADDITIONAL_DOMAINS` (`:29`),
`OIDC_DISCOVERY_URL`/`KEYCLOAK_URL` (`:32`,`:34`), `PROMETHEUS_URL`/`PROMETHEUS_EXTERNAL_URL`
(`:36`,`:37`), `GRAFANA_DATASOURCE_UID=mimir-prd` en
`GRAFANA_BILLING_DATASOURCE_UID=mimir-billing` (`:47`,`:48`), `ARGOCD_MANAGER` (`:50`),
`REGISTRY_*` (`:53`-`:56`), `SLEEP_MODE_WAKER_IMAGE` (`:62`), en de
`GIT_PROJECTS_SERVER_*`/`GIT_ARGO_APPLICATIONS_*`-blokken (`:10`-`:23`) — die laatste bepalen of
fundament de git-repo's deelt met ODCN (vraag 3.8).

**Oordeel: geen wijziging.** Een overlay hoort clusterwaarden te bevatten; dat is het doel. Wel
is dit de checklist voor de nieuwe overlay.

#### m. `bootstrap/.../odcn-production/patches/deployment.yaml:12` en `:53`

Regel 12 is de image-pin (`rcr.rijksapps.nl/ghcr-rig/.../operations-manager:...`), regel 53 zet
`cluster-api.apps.prd1.gn2.quattro.rijksapps.nl` als env-waarde. Dezelfde API-host staat ook in
`bootstrap/rig-system/kustomize/overlays/odcn-production/kustomization.yaml:71`,
`infrastructure/.../external-dns/controller/overlays/odcn/kustomization.yaml:33` en
`infrastructure/.../prometheus/controller/overlays/odcn/kustomization.yaml:31`.

**Oordeel: geen wijziging** — allemaal overlaywaarden op de goede plaats. Wel: vier plaatsen
voor dezelfde clusterAPI-host is vier plaatsen om te vergeten bij fundament. Opnemen in de
checklist.

#### n. `ocs-storagecluster-ceph-rbd` in de infrastructuuroverlays — vijf plaatsen

`minio/controller/overlays/odcn/kustomization.yaml:14`,
`pgadmin/controller/overlays/odcn/kustomization.yaml:14`,
`postgresql/database/overlays/odcn/kustomization.yaml:15`,
`backup-destination/controller/overlays/odcn/kustomization.yaml:35`,
`redis/controller/overlays/odcn/kustomization.yaml:15`.

**Oordeel: geen wijziging** — overlaywaarden op de goede plaats. Vijf plaatsen om te vergeten;
checklist.

#### o. `capsule.clastix.io/tenant: rig-prd` — `infrastructure/.../backup-destination/controller/overlays/odcn/network-policies/minio-networkpolicy.yaml:16`

Een NetworkPolicy die op het Capsule-tenantlabel selecteert. Op een cluster zonder Capsule
matcht die selector **niets**, en dan laat de policy de back-uppods niet bij MinIO.

**Oordeel: geen wijziging aan het odcn-bestand**, maar dit is géén stille no-op zoals (f) —
het faalt dicht. De fundament-overlay moet een eigen selector krijgen (namespacelabel, of het
tenantlabel van wat fundament aan multi-tenancy gebruikt). Opnemen als expliciet punt in de
checklist, niet als "kopieer de odcn-overlay".

#### p. `Taskfile.yaml:842`, `:924`, `:974` — de AGE-sleutelkeuze is een tweeweg-ternair

```
KEY_FILE: '{{if eq (default "local" .CLUSTER_TYPE) "sandboxed-local"}}security/sandbox-key.txt{{else}}security/key.txt{{end}}'
```

Alles wat **niet** `sandboxed-local` is, valt terug op `security/key.txt` — de
**productiesleutel van ODCN**. Een nieuw `CLUSTER_TYPE=fundament-production` versleutelt dus
stilzwijgend met de ODCN-sleutel, en is daarmee door iedereen met de ODCN-sleutel te
ontsleutelen. Er komt geen waarschuwing.

**Oordeel: naar configuratie.** De sleutelkeuze hoort uit `.env-taskfile-<cluster>` te komen
(bijvoorbeeld een `AGE_KEY_FILE=`-regel), niet uit een ternair op één clusternaam.
*Losse taak — dit is een beveiligingspunt, niet alleen een netheidspunt.*

De ODCN-recipient in de bestaande sops-bestanden is
`age1efv94gmdq8l60au5gslnxunkqrftcyyfvscfeysv9j44q8g8ages2gl4dd` (o.a.
`infrastructure/.../secrets/config/overlays/odcn/keycloak-admin-secret.yaml.sops.yaml:13`).
Fundament krijgt een eigen recipient; dat is precies het punt van (p).

### 2.3 Samenvatting: wat naar configuratie moet

| # | punt | bestand:regel | prioriteit |
|---|---|---|---|
| 1 | egress-annotatie op namespace | `manifests/namespace.yaml.jinja:10` | **blokkerend** — kan namespace-creatie laten falen |
| 2 | AGE-sleutel per cluster | `Taskfile.yaml:842,924,974` | **blokkerend** — versleutelt anders met de ODCN-sleutel |
| 3 | UID/SCC-keuze op eigenschap i.p.v. clusternaam | `manifests/pod-security-context.yaml.jinja:8` | hoog |
| 4 | snapshotclass uit één bron | `opi/core/config.py:438`, `opi/manager/backup/base.py:467` | hoog |
| 5 | external-dns-image uit de mirror in de base | `infrastructure/.../external-dns/controller/base/deployment.yaml:28` | midden |
| 6 | registry-URL uit settings | `opi/api/router.py:3386` | midden |
| 7 | `select-cluster` niet met hardgecodeerde cases | `Taskfile.yaml:33` (menu `:41-45`, cases `:49-61`) | laag — handwerk, maar zichtbaar |
| 8 | `publish`/`pin-operations-manager-image` clusteronafhankelijk | `Taskfile.yaml:1209`, `:1249` | laag — pas nodig als fundament eigen images pint |
| 9 | `ip_whitelist` zonder nginx-equivalent | `manifests/ingress.yaml.jinja:15` | voorwaardelijk — alleen als fundament op IP wil afschermen |
| 10 | dode verwijzing `docs/knowledge/odcn-ingress-controller.md` | `opi/core/cluster_config.py:627` | opruimen |

Wat **blijft staan**, met reden: de OpenShift-router-annotaties (inert op nginx, en een
per-cluster if in vijf takken kost meer dan het oplevert), het
`openshift-monitoring`-Prometheusfilter (no-op buiten OpenShift), de Capsule-wachtlus (zit al
achter `uses_capsule`), de UID-defaults in `deployment.yaml.jinja` (het projectbestand kan ze
overschrijven), de docker.io-defaults voor de console-images (de default is de neutrale kant en
het comment zegt het), en alle waarden in de odcn-overlays (die horen daar).

---

## 3. De vragenlijst voor het fundament-platformteam

Bedoeld om als één mail of één overleg te kunnen. Vraag 3.1 en 3.2 eerst: hun antwoorden laten
de helft van de rest vervallen.

### 3.1 Wat voor cluster is het

1. Is fundament OpenShift of vanilla Kubernetes? Welke versie?
2. Draait er een policy-engine (Kyverno, Gatekeeper, Kubewarden) die manifesten kan weigeren?
   Zo ja: is er een lijst van verplichte en verboden labels/annotaties op namespaces, pods en
   Ingress-objecten?
3. Gelden er Pod Security Standards / SCC's, en zo ja welke? Krijgen pods een UID toegewezen,
   of moeten wij er zelf een zetten? Is er een per-namespace UID-range?

*(Bepaalt in één klap of de SCC-aanname, de route-annotaties en de namespace-annotatie
overgaan of eruit moeten — punten 2.2c, 2.2d en 2.2e.)*

### 3.2 Wie beheert wat

4. Krijgen wij cluster-admin, of alleen rechten binnen een set namespaces?
5. Mogen wij CRD's en operators installeren — CloudNativePG, cert-manager, VPA, Capsule,
   ArgoCD? Of levert het platform die, en zo ja welke versies en in welke namespaces?
6. Is er multi-tenancy (Capsule of iets anders)? Zo ja: hoe heet het tenantlabel, en welke
   waarde krijgen onze namespaces? *(Wij wachten vandaag op `capsule.clastix.io/tenant`, zie
   2.2g, en selecteren erop in een NetworkPolicy, zie 2.2o.)*
7. Welke naamgeving voor namespaces wordt van ons verwacht? Wij gebruiken op ODCN het prefix
   `rig-prd-`; is een `rig-`-achtig prefix daar ook beschikbaar?
8. Bestaat er een LimitRange of ResourceQuota op onze namespaces? Welke minima en maxima voor
   CPU en geheugen? *(Vult `min/max_memory_*` en `min/max_cpu_*`.)*

*(Dit is de vraag die de meeste vervolgvragen wegneemt. Stel hem eerst.)*

### 3.3 Netwerk en naam

9. Welke ingresscontroller draait er? In welke namespace, en met welk podlabel is precies
   *onze* controller te selecteren? *(Wij moeten hem in een NetworkPolicy kunnen aanwijzen —
   `ingress_controller_selector`.)*
10. Welke DNS-zone krijgen wij als default voor deployments? Krijgen wij daar een wildcard
    op, of moet elke hostnaam apart aangevraagd?
11. Draait er external-dns? Zo ja: op welke API/host praat die, en mogen wij er records in
    zetten via annotaties? *(Vult `nice_url.supported_domains[].external_dns_target`.)*
12. Mag het ZAD-portaal publiek op internet bereikbaar zijn, of moet het achter een
    afscherming? Zo afgeschermd: op IP-niveau, via VPN, of alleen intern? **Zie paragraaf 5 —
    dit antwoord bepaalt de hele hub-en-spoke-opzet.**
13. Is er internet-egress vanuit pods? Zo nee: via welke gateway, en hoe wordt die aangezet —
    een annotatie op de namespace, een label, een aparte resource? *(Op ODCN is dit
    `egress.projectcalico.org/egressGatewayPolicy`, zie `docs/ron-koppeling.md`; dit is het
    soort afspraak dat op fundament gegarandeerd anders heet — punt 2.2c.)*
14. Kunnen wij Let's Encrypt gebruiken (ACME HTTP-01 of DNS-01), of is er een interne CA?

### 3.4 Images

15. Is er een registry-mirror zoals `rcr.rijksapps.nl` op ODCN? Onder welke paden zijn
    ghcr.io, docker.io, quay.io, gcr.io en registry.k8s.io bereikbaar?
16. Zijn ghcr.io en docker.io direct bereikbaar vanuit pods, of geblokkeerd?
17. Welke pull-secrets hebben wij nodig, en hoe komen we eraan?
18. Mogen wij zelf images pushen naar die registry? Onder welk pad?

*(Zonder mirror is de `registry-rewrite`-extensie niet nodig; met een ándere mirror is het één
nieuw yaml-bestand naast `operations-manager/python/extensions/odcn-registry-rewrite.yaml`.)*

### 3.5 Opslag en back-up

19. Welke StorageClasses zijn er? Welke is de default, en welke ondersteunt
    `ReadWriteOnce`? Is er er een die `ReadWriteMany` kan?
20. Is er een VolumeSnapshotClass, en hoe heet die? **De hele back-upketen leunt erop** — zonder
    snapshotclass werkt back-up niet.
21. Waar mag de back-updoelbestemming landen: een eigen namespace met MinIO (zoals
    `rig-prd-backup` op ODCN), of is er een S3-dienst van het platform?
22. Zijn er quota op opslag per namespace?

### 3.6 Identiteit

23. Gebruikt fundament dezelfde Keycloak als ODCN (`keycloak.rijksapp.nl`) of een eigen?

    **Dit is een ontwerpbeslissing, geen detail.** Eén Keycloak betekent één
    gebruikersadministratie over twee clusters: gebruikers en realms bestaan één keer, en een
    project op fundament kan dezelfde realm-naam niet nog eens claimen (de realmnaam bevat
    vandaag het cluster: `<project>-<cluster>`, zie het voorbeeld in `workflow/outline.md`).
    Twee Keycloaks betekent twee administraties, twee sets credentials en gebruikers die twee
    keer moeten inloggen.

24. Is die Keycloak vanaf fundament netwerkbereikbaar (voor pods én voor de OPI die realms
    beheert)?
25. Welke OIDC-provider gebruikt het portaal zelf voor inlog van beheerders?

### 3.7 Toezicht

26. Is er Prometheus, Mimir, of iets anders? Op welke in-cluster URL is het te bevragen?
27. Is er een Grafana, en wat zijn de datasource-uid's? *(Wij zetten vandaag
    `GRAFANA_DATASOURCE_UID=mimir-prd` en `GRAFANA_BILLING_DATASOURCE_UID=mimir-billing` in de
    OPI-configmap — `bootstrap/.../odcn-production/configmap.yaml:47-48`.)*
28. Scrapet het platform onze pods al, of moeten wij een eigen Prometheus meebrengen? *(Als
    beide: zie punt 2.2f — dubbeltelling in de gebruiksweergave.)*
29. Draait er een VPA-recommender? *(Zonder VPA valt de resource-tuner terug —
    `supports_vpa`.)*

### 3.8 De repositories

30. Deelt fundament de drie git-repo's met ODCN (`zad-projects`,
    `zad-argo-user-applications`, `zad-deployments`), of krijgt het eigen repo's?

    Delen kán: projectbestanden hebben een `clusters:`-lijst en deployments een `cluster:`-veld,
    dus één projectbestand kan beide clusters beschrijven. Maar dan schrijven **twee
    OPI-instanties in dezelfde repo**, en wordt serialisatie van pushes een vraag: wat gebeurt
    er bij een gelijktijdige push van beide instanties? Vandaag is er geen mechanisme dat dat
    coördineert.

31. Is de git-server (vandaag GitHub, zie `configmap.yaml:12,20`) bereikbaar vanaf fundament?
32. Kan ArgoCD op fundament bij die repo's, en met welke credentials?

---

## 4. Het stappenplan

### 4.1 Wat nu al kan, zonder toegang tot fundament

| # | stap | resultaat |
|---|---|---|
| 1 | Punt 2.3-1 uitvoeren: egress-annotatie configureerbaar per cluster | een nieuw cluster krijgt geen ODCN-annotatie meer op zijn namespaces |
| 2 | Punt 2.3-2 uitvoeren: AGE-sleutelbestand uit `.env-taskfile-<cluster>` | fundament kan niet per ongeluk met de ODCN-sleutel versleutelen |
| 3 | Punt 2.3-3 uitvoeren: `pod-security-context` op een clustereigenschap i.p.v. een namenlijst | een nieuw cluster krijgt niet stil de OpenShift-tak |
| 4 | Punt 2.3-4 uitvoeren: snapshotclass uit `cluster_config`, de twee andere defaults weg | één bron voor de snapshotclass |
| 5 | Punt 2.3-5 en 2.3-6 uitvoeren: mirror uit de external-dns-base, registry-URL uit settings | geen ODCN-registry meer buiten de odcn-overlays |
| 6 | Punt 2.3-10: de dode `docs/knowledge/`-verwijzing opruimen (of het bestand alsnog schrijven) | geen verwijzing naar een niet-bestaand bestand |
| 7 | Een lege `fundament`-overlayskeletons voorbereiden als kopie van odcn, met elke ODCN-waarde vervangen door een `TODO:`-markering | de installatie wordt invullen, niet zoeken |
| 8 | De vragenlijst uit paragraaf 3 versturen | het pad vrijmaken voor 4.2 |
| 9 | De beslissingen uit 5.6 agenderen | hub-en-spoke-richting vastgelegd vóór de installatie |

Stappen 1 t/m 6 zijn zes losse, kleine PR's. Ze zijn allemaal **nu** te doen en maken de
installatie niet alleen makkelijker maar ook veiliger, ook als fundament nooit komt.

Stap 7 is bewust een skelet met `TODO:`-markeringen en niet een werkende kopie: een kopie van
de odcn-overlay die per ongeluk doorgaat is precies het faalpad dat dit hele document probeert
te voorkomen.

### 4.2 Wat pas kan zodra de antwoorden er zijn

| # | stap | wacht op |
|---|---|---|
| 10 | Clustersleutel + naamgeving vaststellen (`fundament-production`? prefix?) | 3.2-7 |
| 11 | Het `CLUSTER_CONFIG`-blok invullen (`cluster_config.py`) | 3.1 t/m 3.7, vrijwel alles |
| 12 | `.env-taskfile-fundament-*` aanmaken; let op de twee mapnamen (`INFRASTRUCTURE_CLUSTER_FOLDER` vs `BOOTSTRAP_CLUSTER_FOLDER`) | 10 |
| 13 | `select-cluster` uitbreiden (`Taskfile.yaml:33`) | 10 |
| 14 | Eigen AGE-sleutel genereren, `generate-infrastructure-secrets-for-cluster` en `generate-bootstrap-secrets-for-cluster` draaien, het `secrets-overview-*-fundament.yaml` verwerken en verwijderen | 2 en 10 |
| 15 | `bootstrap/rig-system/kustomize/overlays/fundament/` invullen (ArgoCD, namespace, repo-secrets, netwerkbeleid) | 3.2, 3.8 |
| 16 | `bootstrap/.../operations-manager/overlays/fundament/` invullen (configmap-checklist uit 2.2l) | 3.3, 3.4, 3.6, 3.7 |
| 17 | `infrastructure/bootstrap/clusters/fundament/kustomization.yaml` samenstellen — begin bij de 9 componenten die op ODCN echt draaien | 3.2-5 (wat levert het platform zelf) |
| 18 | Per meegaand component een `overlays/fundament/` aanmaken; let op de vijf storageclass-plaatsen (2.2n) en de tenant-selector in de backup-networkpolicy (2.2o) | 3.5, 3.2-6 |
| 19 | Registry-extensie: `extensions/fundament-registry-rewrite.yaml` óf `extensions: []` | 3.4 |
| 20 | `ip_whitelist`-equivalent bouwen (punt 2.3-9) | 3.3-12, alleen als afgeschermd |
| 21 | Publish/pin-taken clusteronafhankelijk maken (punt 2.3-8) | alleen als fundament eigen image-pins krijgt |
| 22 | Hub-en-spoke inrichten, of niet | paragraaf 5, beslissing 5.6 |

### 4.3 Volgorde van installeren

Wanneer de antwoorden binnen zijn, is de volgorde: **10-11-12-13** (namen en code), dan **14**
(sleutels), dan **17-18** (infrastructuur, zodat de platformdiensten er staan), dan **15-16**
(ArgoCD en OPI zelf), dan **19-20** (registry en afscherming), dan **22** (federatie).

Eerst OPI uitrollen en dan pas de infrastructuur werkt niet: OPI's eigen configmap wijst naar
database, MinIO, Redis en Keycloak die er dan nog niet zijn.

---

## 5. Hub en spoke

### 5.1 Wat er al ligt

Meer dan het lijkt. `features/federation-routing.md` beschrijft precies dit, met status
**Implemented**, en het is gebouwd:

| onderdeel | bestand | regels |
|---|---|---|
| instellingen | `opi/core/config.py:426-428` (`FEDERATION_ROLE`, `FEDERATION_PEERS`, `FEDERATION_REQUEST_TIMEOUT`) | — |
| peerregister | `opi/core/federation_config.py` | 86 |
| routeringslaag | `opi/core/federation_service.py` | 228 |
| HTTP-client naar een peer | `opi/connectors/opi.py` | 132 |
| peers/health-endpoints | `opi/api/federation_router.py` | 74 |
| inhaakpunt in de taakketen | `opi/core/task_helpers.py:58-61` (aanmaken), `:110-112` en `opi/api/task_router.py:166-168` (status) | — |

`FEDERATION_ROLE` is `standalone` | `master` | `slave`; default `standalone`, en dan bestaat de
laag niet (`opi/server.py:250-255` bouwt de service alleen als er peers zijn). Een master leest
het `cluster`-veld van een deployment (`federation_service.py:167-189`, met terugval op
`settings.CLUSTER_MANAGER`) en stuurt de taak door naar de OPI van dát cluster, over HTTPS met
een `X-API-Key`. Statusopvragingen en annuleringen worden doorgeproxyd via een **in-memory**
routetabel (`federation_service.py:31-32`). De verbinding is eenrichtingsverkeer: master belt
spoke, nooit andersom.

Dat is het pass-through-idee. De vraag is niet of het bestaat, maar of het klopt met wat we nu
willen. Vier gaten en een sleutelvraag.

### 5.2 Welke kant staat de pijl op

In het bestaande ontwerp is de master de publieke en de spoke de afgeschermde: het
architectuurdiagram in `features/federation-routing.md:29-54` zet "Master OPI (odcn-production
or central)" met "Full frontend" bovenaan, en de slaves als "API + worker".

Als fundament niet publiek bereikbaar mag zijn en ODCN wel, dan is **ODCN de hub en fundament
de spoke**, en past het ontwerp precies. Het omgekeerde (fundament als hub) vraagt een ander
verhaal richting beveiliging, want dan moet fundament juist wél publiek zijn.

**Te bevestigen** vóór de installatie, want de rolverdeling bepaalt welke van de twee clusters
een ingress met portaal krijgt en welke alleen een API-endpoint. Antwoord komt uit vraag
3.3-12.

### 5.3 Wat is er zichtbaar op een spoke — het grootste gat

Federatie routeert **taken**. Wat níét beschreven is als geproxyd, en het ook niet is:

- de webinterface (`opi/web/`), inclusief projectenlijst, detailpagina en wizard;
- het lezen van projectbestanden (die komen uit de git-repo, niet van de peer);
- de logboeken (`logs_router`, `logs_websocket_router` — die praten met `kubectl` op het eigen
  cluster);
- de metriekweergave (`opi/web/router_usage.py` — die praat met de Prometheus van het eigen
  cluster).

Concreet betekent dat: **een gebruiker die op de hub naar de logs of het geheugengebruik van
een fundament-deployment kijkt, krijgt niets.** De hub kent alleen zijn eigen cluster voor die
weergaven. Alleen taken (deploy, back-up, restore, database-acties) reizen mee.

En omgekeerd: `opi/server.py:521-544` registreert **alle** routers onvoorwaardelijk. Er is
**geen modus waarin het portaal uit staat en alleen de API luistert**. Een spoke-OPI serveert
dus een volledig portaal op zijn eigen ingress, ook als niemand daar hoort te komen.

Dat is het gat tussen `features/federation-routing.md` en "niet direct als web te bereiken".
Twee dingen ontbreken:

1. Een `FEDERATION_ROLE=slave`-modus die de web-routers niet registreert (of achter een schakelaar
   zet), zodat een spoke alleen `/api/*` en `/health` aanbiedt. Vandaag is de enige afscherming
   netwerkniveau — geen ingress, of een ingress met een whitelist.
2. Proxy voor de leesbare kant: logs en metrieken van een remote deployment. Dat is nieuw werk,
   geen configuratie.

**Aanbeveling:** los (1) op met netwerk vóór code — geen publieke ingress op de spoke is
eenvoudiger en betrouwbaarder dan een routerschakelaar in de app. Zet (2) op de lijst als
bekende beperking en beslis apart of hij nodig is; het is de eerste klacht die je van
gebruikers gaat horen.

### 5.4 Wat gaat er niet over HTTP

Alles hieronder gaat vandaag over een **directe verbinding** en niet via federatie:

| verbinding | waar | binnen één cluster | cross-cluster |
|---|---|---|---|
| `kubectl` naar de API-server | `opi/connectors/kubectl.py` | werkt (OPI zit in het cluster) | **kan niet** — geen mechanisme |
| chisel-tunnels voor databases en MinIO | `opi/connectors/chisel_connector.py`, `opi/utils/chisel_helper.py` | werkt | zie hieronder |
| Keycloak-beheer (realms, clients) | Keycloak-connector | werkt als Keycloak bereikbaar is | hangt aan vraag 3.6-23 |
| database-kloon tussen omgevingen | `opi/manager/database_manager.py`, `opi/manager/clone_validation.py` | werkt | **moet nog** |
| MinIO-kopie | `opi/manager/minio_manager.py` | werkt | **moet nog** |
| ArgoCD | in-cluster service | werkt | n.v.t. — elk cluster zijn eigen ArgoCD |

Het federatie-ontwerp zegt hierover expliciet
(`features/federation-routing.md:15`): "Chisel tunnels exist for temporary operations (DB
cloning, MinIO access) but are not suitable as permanent infrastructure for a task queue."
Dat is een uitspraak over de **taakketen**, niet over kloonacties zelf.

Binnen fundament blijven al deze verbindingen werken zoals op ODCN, want de spoke-OPI zit in
zijn eigen cluster. Wat **niet** werkt is een actie die twee clusters tegelijk raakt — een
kloon van een ODCN-database naar een fundament-namespace. Die moet expliciet benoemd worden als
**kan-niet-vandaag**, en het is de vraag of hij ooit moet: de eenvoudige route is via de
back-upketen (back-up op cluster A, restore op cluster B), die al bestaat en al over S3 loopt.

**Aanbeveling:** cross-cluster kloon niet bouwen. Documenteren als "gebruik back-up + restore",
en meten of dat volstaat.

### 5.5 Hoe komt de master bij de spoke

Als fundament niet publiek is, is de vraag over welk pad ODCN hem dan bereikt. Opties, in
volgorde van voorkeur:

1. **Interne koppeling tussen de clusters** — als er een netwerkpad is tussen ODCN en fundament
   (bijvoorbeeld via het RON, zie `docs/ron-koppeling.md`), dan is het gewoon een interne
   hostnaam met TLS. Simpelst.
2. **Een gateway of proxy** die de fundament-API op een beperkt pad publiceert
   (`/api/v2/*` en `/api/tasks/*`), met de `X-API-Key` als enige toegang.
3. **Een omgekeerde tunnel** (spoke belt hub, hub gebruikt de tunnel) — dit **past niet op het
   huidige ontwerp**, dat expliciet eenrichtingsverkeer is
   (`features/federation-routing.md:229`). Vergt bouwwerk.

**Dit is een vraag aan het platformteam, geen keuze van ons.** Hij staat als 3.3-12 en moet
gesteld worden voordat 5.2 beantwoord kan worden.

Let ook op de failure modes die het ontwerp al benoemt
(`features/federation-routing.md:236-247`): de routetabel is in-memory, dus **een herstart van
de hub verliest het spoor naar lopende taken op de spoke** (client krijgt 404). Het ontwerp
noemt een DB-tabel als optie B en zegt "start with Option A". Met twee productieclusters en een
hub die vaker herstart dan een sandbox is optie B waarschijnlijk alsnog nodig. Openstaand.

### 5.6 De sleutels

Per peer een eigen API-key (`federation_config.py:16` `api_key` op `PeerConfig`; JSON in
`FEDERATION_PEERS`). Dat is de goede vorm: één gecompromitteerde spoke-sleutel raakt de andere
niet. De spoke valideert hem met zijn eigen `MASTER_API_KEY` (`opi/core/config.py:263`), via
`validate_master_api_key` (`opi/api/federation_router.py:7`).

Wat **niet** beschreven is:

- Waar de sleutel landt. `FEDERATION_PEERS` is een JSON-string in de settings, dus vandaag zou
  hij in de OPI-configmap van de hub staan — **in platte tekst**, want de configmap versleutelt
  alleen via SOPS-velden zoals de git-wachtwoorden dat doen
  (`.../odcn-production/configmap.yaml:14`). Dit moet een `base64+age:`-waarde worden, of een
  aparte secret.
- Hoe hij te roteren is. Er is geen procedure. Een rotatie betekent vandaag: nieuwe waarde in
  de spoke-`MASTER_API_KEY`, nieuwe waarde in de hub-`FEDERATION_PEERS`, en beide OPI's
  herstarten — met een venster waarin de hub de spoke niet bereikt. Een tweede geaccepteerde
  sleutel op de spoke zou dat venster wegnemen; die bestaat niet.

**Aanbeveling:** `FEDERATION_PEERS` versleuteld opslaan zoals de git-wachtwoorden, en de
rotatieprocedure opschrijven (ook als hij "korte onderbreking, buiten kantooruren" is). Beide
vóór de eerste peer live gaat.

### 5.7 Openstaande beslissingen

| # | beslissing | wie | vóór installatie? |
|---|---|---|---|
| A | Wordt ODCN de hub en fundament de spoke, of andersom, of blijven het twee losse standalone-instanties? | wij, na antwoord 3.3-12 | **ja** |
| B | Krijgt de spoke een publieke ingress met portaal, alleen een afgeschermd API-endpoint, of helemaal geen ingress? | wij + platformteam | **ja** |
| C | Over welk netwerkpad bereikt de hub de spoke? | platformteam | **ja** |
| D | Delen de twee clusters de git-repo's, of niet? (vraag 3.8-30) — bepaalt of één projectbestand beide clusters kan beschrijven | wij + platformteam | **ja** |
| E | Eén Keycloak of twee? (vraag 3.6-23) | wij + platformteam | **ja** |
| F | Wordt de routetabel duurzaam (optie B uit het federatieontwerp) of blijft hij in-memory? | wij | nee — maar wel vóór productief gebruik |
| G | Komt er een `slave`-modus die de web-routers uitzet, of doen we het met netwerk? | wij | nee — netwerk volstaat om te starten |
| H | Bouwen we logs- en metriekproxy voor remote deployments? | wij | nee — bekende beperking |
| I | Bouwen we cross-cluster database-kloon, of verwijzen we naar back-up + restore? | wij | nee — aanbeveling: niet bouwen |
| J | Waar landt `FEDERATION_PEERS` en hoe roteren we de sleutels? | wij | **ja**, vóór de eerste peer live gaat |

Beslissingen A t/m E en J moeten genomen zijn voordat de installatie begint. F t/m I mogen
daarna, maar horen wel op de lijst te staan zodat ze niet stilzwijgend "nee" worden.

---

## 6. Wat dit document bewust niet doet

**Geen tweede cluster-abstractielaag.** `opi/core/cluster_config.py` is het inhaakpunt en dat
werkt: 56 bestanden lezen eruit via 48 accessors, en dat is precies waarom een vierde
clustersleutel goedkoop is. De TODO bovenaan dat bestand (`cluster_config.py:10`, "In the
future, read this configuration from YAML file") staat er al lang en niet zonder reden — een
YAML-gedreven clusterregistratie is een grotere verbouwing dan een tweede cluster
rechtvaardigt. De punten in 2.3 gaan allemaal over waarden die *naar* `cluster_config`
verhuizen, niet over een nieuwe laag eromheen.

**Geen namen vastleggen.** Zie de waarschuwing bovenaan.

**Geen federatie aanzetten of testen.** Paragraaf 5 stelt alleen vast wat er is, wat het gat
is, en wat beslist moet worden.

**Geen ODCN-eigenaardigheden meeverhuizen.** `docs/ron-koppeling.md` en
`features/futures/vlam-api-vpn-proxy.md` zijn leerzaam als *voorbeeld van het soort
platformafspraak dat je moet uitvragen* — de egressgateway-annotatie en het
ingresscontroller-label zullen op fundament gegarandeerd anders heten of niet bestaan. Ze zijn
hier gebruikt om de vragen scherp te krijgen, niet als iets dat overgezet moet worden.
