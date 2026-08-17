# Wat een tweede cluster van ons vraagt

Status: plan, 12 augustus 2026. Aanleiding: ZAD draait vandaag op ODCN. Er komt een tweede productiecluster bij, werknaam **fundament**. We weten nog niet wat daar aan platformdiensten staat, wie er beheert en of het portaal daar publiek bereikbaar mag zijn. De installatie moet daarom van "wat wij op ODCN deden" naar "wat een willekeurig cluster van ons vraagt", en dat begint met weten waar ODCN vandaag in de code en de manifesten zit vastgeschroefd.

**Deze taak levert een document op, geen code.** De opdracht is een uitzoekplan uitvoeren en het resultaat vastleggen als stappenplan. Wijzig in deze taak geen enkel bestand onder `operations-manager/python/opi/`, `bootstrap/` of `infrastructure/`. Wat er aan code moet gebeuren komt als lijst in het stappenplan te staan, met per punt een bestand en een regelnummer, zodat het daarna als losse taken te shippen is.

## Het op te leveren document

`docs/een-nieuw-cluster-installeren.md`, in het Nederlands, met deze indeling:

1. **De drie lagen** waar een cluster in zit (code, kustomize, taakketen), met per laag wat er voor een nieuw cluster bij moet.
2. **De inventaris**: elk punt waar ODCN vandaag hard in staat, met bestand en regel, en per punt of het naar configuratie moet of dat het mag blijven staan.
3. **De vragenlijst voor het fundament-platformteam**, gegroepeerd zodat hij als één mail of één overleg kan.
4. **Het stappenplan zelf**: wat we nu al kunnen voorbereiden zonder toegang tot fundament, en wat pas kan zodra de antwoorden er zijn.
5. **Hub en spoke**: wat er al ligt, welke beslissingen open staan, en welke daarvan vóór de installatie genomen moet zijn.

De inventaris in dit plan is het startpunt, niet het eindresultaat. Loop hem na, vul aan wat ontbreekt, en corrigeer wat niet klopt.

## Wat er nu is, gemeten

### Laag 1: de code

`opi/core/cluster_config.py` is 1133 regels met drie clusters (`local`, `sandboxed-local`, `odcn-production`) en 48 functies eromheen. **56 bestanden onder `opi/` importeren eruit.** Dit is het bedoelde inhaakpunt: een vierde sleutel in `CLUSTER_CONFIG` erbij en het meeste volgt vanzelf.

Wat daar per cluster in staat, en dus voor fundament beantwoord moet worden:

| sleutel | ODCN-waarde | wat het van het cluster vraagt |
|---|---|---|
| `ingress_postfix` | `.rig.prd1.gn2.quattro.rijksapps.nl` | een default-zone die de ingresscontroller serveert |
| `namespace_prefix` / `namespace` / `argo_namespace` | `rig-prd-` / `rig-prd-operations` | naamgevingsafspraak met het platformteam |
| `storage` | `ocs-storagecluster-ceph-rbd` plus snapshotclass | een RWO-storageclass en een VolumeSnapshotClass |
| `ingress_controller_selector` | namespace `openshift-ingress`, podlabel `...deployment-ingresscontroller: rig` | de NetworkPolicy moet de juiste router kunnen aanwijzen |
| `uses_capsule` | `true` | multi-tenancy waar OPI op moet wachten bij namespace-creatie |
| `supports_vpa` | `true` | een draaiende VPA-recommender, waar de resource-tuner op leunt |
| `nice_url.supported_domains` | drie domeinen met `external_dns_target` | eigen DNS-zones plus een werkende external-dns |
| `extensions` | `["odcn-registry-rewrite"]` | een registry-mirror, want ghcr is er geblokkeerd |
| `keycloak_discovery_url`, `database_server`, `minio_host`, `redis_server`, `backup_namespace`, `database_operator_namespace` | rig-prd-adressen | de bijbehorende platformdiensten in dat cluster |
| `min/max_memory_*`, `min/max_cpu_*` | 25 tot 4096 Mi | quota- en LimitRange-grenzen van het cluster |

**Buiten `cluster_config` staan ODCN-waarden als default in de code.** Dat is het gevaarlijke deel, want een nieuw cluster erft ze stilzwijgend:

- `opi/core/config.py:438` `BACKUP_SNAPSHOT_CLASS: str = "ocs-storagecluster-rbdplugin-snapclass"`
- `opi/manager/backup/base.py:467` `snapshot_class: str = "ocs-storagecluster-rbdplugin-snapclass"`
- `manifests/namespace.yaml.jinja:10` zet `egress.projectcalico.org/egressGatewayPolicy: "internet"` op elke namespace, met er letterlijk boven de notitie `NOTE: we need manifests per cluster`. Op ODCN is dit een Kyverno-gecontroleerde annotatie met een beperkte waardenlijst (zie `docs/ron-koppeling.md`); op een cluster zonder Calico-egressgateway is hij zinloos, en bij een cluster met een ándere policy-engine kan hij de namespace weigeren.
- `manifests/ingress.yaml.jinja` zet `haproxy.router.openshift.io/*`-annotaties en `route.openshift.io/termination` op elke Ingress. Nginx negeert ze, dus het breekt niets, maar het is wel de OpenShift-router als aanname.
- `manifests/deployment.yaml.jinja` en `pod-security-context.yaml.jinja` laten de UID/GID los zodra het cluster SCC's heeft.
- `opi/web/router_usage.py:57` filtert op `prometheus!="openshift-monitoring/k8s"`.
- `opi/connectors/kubectl.py:631` wacht op het Capsule-tenantlabel, aangestuurd door `uses_capsule`.

Uitzoeken: welke van deze horen alsnog in `cluster_config`, en welke zijn onschadelijk genoeg om te laten staan. Niet alles hoeft configureerbaar; wél moet in het document staan wélke keuze per punt gemaakt is en waarom.

### Laag 2: kustomize

Twee bomen, allebei met een map per cluster.

- `bootstrap/rig-system/kustomize/overlays/{local,sandboxed-local,odcn-production}/` voor ArgoCD, namespace, repo-secrets en netwerkbeleid.
- `bootstrap/rig-system/kustomize/operations-manager/overlays/{...}/` voor OPI zelf: ingress, configmap, networkpolicy, letsencrypt-issuer, billing-prometheusrule, image-pin.
- `infrastructure/bootstrap/clusters/{local,odcn,sandboxed-local}/` als lijst van infracomponenten, die verwijst naar `infrastructure/bootstrap/infrastructure/<component>/{controller,config,database}/overlays/<cluster>/`. Er staan **80 overlaymappen** onder infrastructure.

Voor fundament betekent dat: één nieuwe cluster-map in elk van de drie bomen, plus een overlay per infracomponent dat meegaat. Meet in de taak welke componenten dat zijn: `infrastructure/bootstrap/clusters/odcn/kustomization.yaml` heeft vault en pgadmin uitgecommentarieerd, dus de odcn-lijst is korter dan de mappenlijst suggereert.

De hardcoded ODCN-waarden in die overlays: `ocs-storagecluster-ceph-rbd` op vijf plaatsen, `cluster-api.apps.prd1.gn2.quattro.rijksapps.nl` als external-dns- en prometheus-target, hostnames op `*.rig.prd1.gn2.quattro.rijksapps.nl`, en `capsule.clastix.io/tenant: rig-prd` in de backup-networkpolicy.

### Laag 3: de taakketen

- `.env-taskfile-<cluster>` per cluster, met `CLUSTER_TYPE`, `RIG_NAMESPACE`, `INFRASTRUCTURE_CLUSTER_FOLDER` en `BOOTSTRAP_CLUSTER_FOLDER`. Let op dat ODCN twee verschillende mapnamen gebruikt (`odcn` voor infrastructure, `odcn-production` voor bootstrap); dat is een valkuil bij het aanmaken van een nieuwe.
- `Taskfile.yaml:33` `select-cluster` heeft de drie clusters als hardgecodeerde `case`. Een vierde erbij is handwerk.
- De AGE-sleutels: `security/key.txt` voor productie, `security/sandbox-key.txt` voor de sandbox. Fundament heeft een eigen sleutel nodig, en daarmee een eigen ronde van `generate-infrastructure-secrets-for-cluster` en `generate-bootstrap-secrets-for-cluster`, plus een `secrets-overview-*-fundament.yaml` naast de zes die er nu staan.
- `task publish-operations-manager` en `pin-operations-manager-image` kennen alleen het `odcn-production`-pad (`Taskfile.yaml:1250`).

## Wat we willen uitzoeken

Dit is de kern van de taak: de vragenlijst opstellen en zoveel mogelijk zelf al beantwoorden uit wat we hebben. Wat niet uit de code te halen is, gaat naar het fundament-platformteam. Groepeer zo:

**Wat voor cluster is het.** OpenShift of vanilla Kubernetes, welke versie, en draait er een policy-engine (Kyverno, Gatekeeper) die onze manifesten kan weigeren. Dit bepaalt in één klap of de SCC-aannames, de route-annotaties en de namespace-annotatie overgaan of dat ze uit moeten.

**Wie beheert wat.** Krijgen wij cluster-admin of alleen namespaces. Mogen wij CRD's installeren (CloudNativePG, cert-manager, VPA, Capsule, ArgoCD-operator) of levert het platform die. Dit is de vraag die de meeste vervolgvragen wegneemt, dus stel hem eerst.

**Netwerk en naam.** Welke ingresscontroller, welke DNS-zone krijgen wij, is er external-dns en op welke API praat die. Mag het portaal publiek bereikbaar zijn, of niet (zie hub en spoke). Is er internet-egress vanuit pods, en zo nee, via welke gateway.

**Images.** Is er een registry-mirror zoals `rcr.rijksapps.nl` op ODCN, en zo ja onder welke paden en met welke pull-secrets. Zonder mirror is de `registry-rewrite`-extensie niet nodig; met een andere mirror is het één nieuw yaml-bestand in `operations-manager/python/extensions/`.

**Opslag en back-up.** Welke storageclasses, is er een VolumeSnapshotClass (de back-upketen leunt erop), en waar landt de back-updoelbestemming.

**Identiteit.** Gebruikt fundament dezelfde Keycloak als ODCN of een eigen. Dit is een echte ontwerpbeslissing en geen detail: één Keycloak betekent één gebruikersadministratie over twee clusters, twee betekent twee.

**Toezicht.** Is er Prometheus of Mimir, en met welke datasource-uid, want `GRAFANA_DATASOURCE_UID` en de billing-uid staan nu in de OPI-configmap.

**De repositories.** Deelt fundament de drie git-repos met ODCN of krijgt het eigen. De projectbestanden hebben een `clusters:`-lijst, dus delen kan, maar dan schrijven twee OPI-instanties in dezelfde repo en wordt serialisatie van pushes een vraag.

## Hub en spoke

Wat er al ligt, en dat is meer dan het lijkt: `features/federation-routing.md` beschrijft precies dit, en het is gebouwd. `FEDERATION_ROLE` (`standalone` | `master` | `slave`) en `FEDERATION_PEERS` staan in `opi/core/config.py:426`, met `opi/core/federation_config.py`, `opi/core/federation_service.py`, `opi/connectors/opi.py` en `opi/api/federation_router.py` eromheen. Een master-OPI leest het `cluster`-veld van een deployment en stuurt de taak door naar de OPI van dat cluster, over HTTPS met een `X-API-Key`. Statusopvragingen worden doorgeproxyd. De verbinding is eenrichtingsverkeer: master belt spoke, nooit andersom.

Dat is het pass-through-idee waar de vraag naar verwijst. Wat de taak moet uitzoeken is niet of het bestaat, maar of het klopt met wat we nu willen:

1. **Welke kant staat de pijl op.** In het bestaande ontwerp is de master de publieke en de spoke de afgeschermde. Als fundament niet publiek bereikbaar mag zijn en ODCN wel, dan is ODCN de hub en fundament de spoke. Bevestig dat dat de bedoelde richting is, want het omgekeerde (fundament als hub) vraagt een ander verhaal richting beveiliging.
2. **Wat is er zichtbaar op een spoke.** Federatie routeert taken. De webinterface, het aanmaken van projecten, de detailpagina, de logboeken en de metriekweergave zijn niet beschreven als geproxyd. Zoek uit wat een spoke-OPI nog wél nodig heeft aan eigen ingress, en of er een modus moet komen waarin het portaal zelf uit staat en alleen de API luistert. Dat is vermoedelijk het grootste gat tussen het ontwerp en "niet direct als web te bereiken".
3. **Wat gaat er niet over HTTP.** Kijk expliciet naar de dingen die vandaag een directe verbinding zijn: `kubectl` naar het cluster, de chisel-tunnels voor databases en MinIO (`opi/connectors/chisel.py`), Keycloak, en het klonen van databases tussen clusters. Elk daarvan is een aanname dat de OPI en het cluster op elkaars netwerk zitten. Op een spoke geldt dat nog steeds binnen dat cluster, maar cross-cluster acties (kloon van ODCN naar fundament) moeten expliciet worden benoemd als kan-niet of moet-nog.
4. **Hoe komt de master bij de spoke.** Als fundament niet publiek is, is de vraag over welk pad de master hem dan bereikt: een interne koppeling, een gateway, of een omgekeerde tunnel. Dit is een vraag aan het platformteam, geen keuze van ons, maar hij moet wel gesteld.
5. **De sleutels.** Per peer een eigen API-key, en die moet ergens beheerd. Beschrijf waar hij landt en hoe hij te roteren is.

Wat níét in deze taak hoort: federatie aanzetten of testen. Alleen vaststellen wat het is, wat het gat is, en wat er beslist moet worden.

## De toets

- `docs/een-nieuw-cluster-installeren.md` bestaat, is Nederlands, en heeft de vijf beschreven onderdelen.
- De inventaris noemt per punt een bestand en een regelnummer, en die kloppen (steekproef van tien).
- Elk hardcoded ODCN-punt uit de inventaris heeft een expliciet oordeel: naar configuratie, of blijft staan met reden.
- De vragenlijst is zo geschreven dat hij zonder toelichting naar een ander team kan.
- Het stappenplan scheidt zichtbaar wat nu al kan van wat op antwoorden wacht.
- De hub-spoke-paragraaf benoemt het gat tussen `features/federation-routing.md` en een niet-publieke spoke, en eindigt met een lijst openstaande beslissingen.
- Er staat geen enkele codewijziging in de PR.

## Waar op te letten

**Verzin geen namen.** De clustersleutel voor fundament is nog niet gekozen. `odcn-production` draagt de omgeving in de naam, dus iets als `fundament-production` ligt voor de hand, maar zet dat in het document neer als voorstel en gebruik het nergens alsof het vastligt. Hetzelfde geldt voor namespaceprefixen en zonenamen.

**De inventaris is geen greplijst.** Zoeken op `odcn` vindt de makkelijke gevallen. De lastige zijn de waarden die ODCN-specifiek zijn zonder dat het woord erin staat: `ocs-storagecluster-*`, `quattro`, `capsule`, `haproxy.router.openshift.io`, `rcr.`, `mimir-prd`, `145.21.227.*`. Loop die apart langs.

**Geen tweede cluster-abstractielaag ontwerpen.** `cluster_config.py` is het inhaakpunt en dat werkt. De verleiding om er een yaml-gedreven clusterregistratie omheen te bedenken is groot en valt buiten deze taak; de TODO bovenaan dat bestand zegt het al, en die staat er niet voor niets al lang.

**`docs/ron-koppeling.md` en `features/futures/vlam-api-vpn-proxy.md` zijn ODCN-specifiek maar wel leerzaam.** De egressgateway-annotatie en het ingresslabel daarin zijn precies het soort platformafspraak dat op fundament anders zal heten. Gebruik ze als voorbeeld van wat je moet uitvragen, niet als iets dat overgezet moet worden.

**Er is geen `docs/knowledge/`.** `opi/core/cluster_config.py:627` verwijst naar `docs/knowledge/odcn-ingress-controller.md` en dat bestand bestaat niet. Noem dat in het document als los op te ruimen punt; repareer het niet in deze taak.
