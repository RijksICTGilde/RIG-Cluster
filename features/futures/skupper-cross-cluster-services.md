# Services delen tussen clusters met Skupper

**Status**: Ontwerp, proof of concept nog te bouwen
**Datum**: 2026-08-20
**Scope**: Een service (bijvoorbeeld een mailserver of database) die in één cluster draait bereikbaar maken vanuit andere clusters, terwijl de clusters elkaar uitsluitend via ingress kunnen bereiken.

## Waar dit over gaat

Onze clusters hebben geen onderling netwerkpad op pod- of node-niveau; het enige dat van buiten bereikbaar is, is de ingress-laag. Toch willen we services kunnen delen: één mailserver die alle clusters gebruiken, of een database die in één cluster staat maar door workloads elders benaderd moet worden. Dat zijn ruwe TCP-protocollen (SMTP, PostgreSQL), dus een gewone HTTP-ingress volstaat niet.

Skupper legt een layer-7 "Virtual Application Network" tussen de clusters. Per cluster draait een router; de routers verbinden onderling over één ingress-endpoint met mTLS, en daaroverheen worden individuele services geëxposeerd. Een gedeelde service verschijnt in de afnemende cluster als een gewone ClusterIP-service, en de applicatie daar merkt niet dat de backend elders draait.

## Waarom Skupper, kort

De afweging tegen de alternatieven staat in het gesprek dat aan dit document voorafging; de kern:

- Submariner en Cilium ClusterMesh vereisen directe pod/node-connectiviteit en vallen af.
- Per service een passthrough Route met TLS-wrappers (stunnel/ghostunnel) werkt, maar schaalt slecht: elke gedeelde service is een nieuw wrapper-paar, een hostname en certbeheer. Voor één service is dat prima; dit document is voor het moment dat het patroon groeit.
- Een headscale/Tailscale-overlay (bewezen in de VLAM-PoC, zie `features/futures/vlam-api-vpn-proxy.md`) is een netwerkoplossing: autorisatie zit op netwerkniveau en discovery regel je zelf. Skupper autoriseert per service.
- FSC is de stelselstandaard voor delen óver organisatiegrenzen, met PKIoverheid-certs en contracten per afnemer. Voor clusters die allemaal van ons zijn is dat overkill; zodra een externe partij afneemt is FSC de route, niet Skupper.

Skupper is precies gebouwd voor "clusters die elkaar alleen via ingress zien": geen CNI-wijziging, geen cluster-admin netwerktoegang op ODCN nodig (wel eenmalig CRD-installatie), mTLS standaard, en elke volgende gedeelde service is één extra resource in plaats van een nieuw bouwwerk.

## Hoe Skupper v2 werkt

We gebruiken Skupper v2 (actueel: 2.2.2, augustus 2026), niet v1. V2 is declaratief via CRD's, wat naadloos in onze GitOps-flow past. De resources:

| Resource | Rol |
|---|---|
| `Site` | Eén per cluster/namespace: "hier draait een router". |
| `RouterAccess` | Maakt een site benaderbaar voor inkomende links, bij ons via een passthrough Route/ingress met SNI. |
| `AccessGrant` / `AccessToken` | Bootstrap: de hub geeft een kortlevend token uit, de afnemende site lost het in en krijgt de mTLS-certs voor de link. |
| `Link` | De permanente mTLS-verbinding tussen twee sites (ontstaat uit het ingeloste token). |
| `Connector` | In de cluster mét de service: bindt een lokale workload (selector + poort) aan een routing key. |
| `Listener` | In de cluster zónder de service: maakt onder dezelfde routing key een lokale ClusterIP-service aan. |

Connector en Listener zijn losgekoppeld via de routing key: de afnemende cluster hoeft niet te weten wáár de service draait, alleen hoe hij heet. Verkeer loopt altijd router-naar-router over de bestaande link, ongeacht de richting waarin de link ooit is opgezet.

## Topologie

Hub-spoke, met de hub in de cluster die de gedeelde services host (of, als dat er meer worden, de best bereikbare cluster):

```
 cluster A: hub, host van de gedeelde services         cluster B: afnemer
+---------------------------------------------+      +-----------------------------------+
| namespace: rig-interconnect (naamvoorstel)  |      | namespace: rig-interconnect       |
|                                             |      |                                   |
|  +----------------+   +------------------+  |      |  +----------------+               |
|  | skupper-router |<==| RouterAccess:    |<=|======|==| skupper-router |               |
|  +---+--------+---+   | passthrough      |  | mTLS |  +---+-------+----+               |
|      |        |       | Route, SNI, 443  |  | Link |      |       |                    |
|      |        |       +------------------+  |      |      |       |                    |
|  Connector  Connector                       |      |  Listener   Listener              |
|  smtp:465   pg:5432                         |      |  smtp:465   pg:5432               |
|      |        |                             |      |      = ClusterIP-services         |
|  +---v---+ +--v-------+                     |      |      ^       ^                    |
|  | mail  | | postgres |                     |      |  +---+-------+---+                |
|  +-------+ +----------+                     |      |  | app-workloads |                |
+---------------------------------------------+      +-----------------------------------+
```

Belangrijk voor onze situatie: **alleen de hub hoeft inkomend bereikbaar te zijn**. De spokes leggen de link uitgaand aan (uitgaand 443 naar een publieke hostname kan overal), dus een spoke-cluster heeft geen eigen RouterAccess, geen open poort en geen ip_whitelist-aanvraag nodig. Dat maakt een nieuwe afnemende cluster goedkoop.

De namespace-naam `rig-interconnect` is een voorstel, geen bestaande naam; definitief te kiezen bij de bouw.

## ODCN-specifiek

- **Bereikbaarheid van de hub**: de RouterAccess wordt een TLS-passthrough Route. Dat patroon is op ODCN al bewezen (het pod-eigen cert wordt geserveerd zodra de twee ODCN-kanten openstaan: de ip_whitelist op de Route en een NetworkPolicy die de IngressController naar de router-pods toelaat). Zie de eerdere passthrough-bevindingen in het FSC/mTLS-traject.
- **Images**: Skupper-images komen van quay.io. ODCN pullt via de RCR-proxy (`rcr.rijksapps.nl/<x>-rig`); te verifiëren dat er een quay-proxyproject bestaat en dat de Skupper-images door de signature policy komen. Dit is een go/no-go-check die vroeg in het stappenplan zit.
- **CRD's en operator**: CRD-installatie vraagt cluster-admin. Op ODCN betekent dat een aanvraag bij het platformteam, tenzij wij die rechten al hebben via de bestaande bootstrap-flow. De controller zelf draait daarna gewoon in onze eigen namespace.
- **GitOps**: alle Skupper-resources (Site, Connector, Listener, RouterAccess) zijn kale CRD-manifests en gaan via de normale route: Kustomize onder `infrastructure/bootstrap/infrastructure/skupper/` met `base/` + `overlays/{local,sandboxed-local,odcn-production}/`, uitgerold door ArgoCD. Alleen de token-inwisseling (AccessToken) is een eenmalige, kortlevende handeling per link; die hoort in een Taskfile-task, niet in git.

## Beveiliging

- **mTLS overal**: de controller geeft per netwerk een eigen CA uit; router-naar-router-verkeer is altijd mTLS. Er is geen "open" modus.
- **De bootstrap is het gevoelige moment**: wie een geldig AccessToken bemachtigt, hangt een site aan ons netwerk. AccessGrants daarom altijd met korte expiry en `redemptionsAllowed: 1` uitgeven, en het token via een bestaand vertrouwd kanaal transporteren (SOPS-versleuteld in git of direct via een Taskfile-task, nooit plain in een chat of ticket).
- **Blast radius van de router**: de router kan bij alles waarvoor een Connector bestaat, en niets anders. Er is dus geen flat network; de exposure-lijst is exact de lijst Connectors in git, en die is reviewbaar per PR. Aanvullend zetten we NetworkPolicies om de router-namespace: ingress alleen vanaf de IngressController (hub) en de app-namespaces die de Listeners gebruiken, egress alleen naar de services met een Connector.
- **Wie mag wat**: Skupper autoriseert op site-niveau (welke cluster hangt aan het netwerk), niet per afnemer per service. Alles wat een Connector heeft is zichtbaar voor alle gelinkte sites. Zolang alle clusters van ons zijn is dat acceptabel; fijnmaziger autorisatie per afnemer is het punt waarop FSC in beeld komt.
- **De service zelf blijft zichzelf beveiligen**: Skupper vervangt geen databaseauthenticatie of SMTP-auth. De tunnel is transport, geen identiteit.

## De database-kanttekening

Voor de mailserver is delen een natuurlijk patroon: SMTP is een nette servicegrens en latency is irrelevant. Voor een database geldt: elk query-roundtrip krijgt de inter-cluster-latency erbij, en de link wordt beschikbaarheidskritisch voor de afnemende applicatie. Waar mogelijk heeft een read-replica in de afnemende cluster de voorkeur; het replicatieverkeer (streaming replication) loopt prima en latency-tolerant over precies dezelfde Skupper-link. Eén gedeelde primary is legitiem als de data één waarheid moet zijn, maar dan is de link infrastructuur die redundant en gemonitord moet zijn, en hoort dat expliciet in het ontwerp van dat project, niet impliciet in deze feature.

## Inpassing in ZAD

Twee fasen, bewust gescheiden:

**Fase 1, platform-infra (dit document)**: Skupper is een platformvoorziening zoals ArgoCD of MinIO. De router-namespace, Sites en Links worden beheerd via de bootstrap-Kustomize; Connectors en Listeners voor gedeelde platformservices (de mailserver) schrijven wij met de hand in de overlays. Geen schema-wijziging, geen OPI-code.

**Fase 2, pas als er vraag is (YAGNI)**: een ZAD-serviceoptie waarmee een projectbestand kan zeggen "expose dit component cross-cluster" respectievelijk "gebruik service X uit cluster Y", waarbij OPI de Connector/Listener genereert. Werknaam `shared-service` (naamvoorstel, niet vastgelegd). Dat is een echte nieuwe schrijfweg met eigen autorisatievragen (wie mag een service clusterbreed exposen?) en verdient dan een eigen plan met die poort expliciet benoemd. Niet nu bouwen.

## Stappenplan

De PoC draait volledig lokaal met twee Kind-clusters, dus zonder aanvragen bij anderen. Skupper v2 CLI (`skupper`) alleen voor debuggen; de bron van waarheid zijn de CRD-manifests.

1. **Go/no-go-checks vooraf.**
   → verify: quay.io-images zijn via de RCR-proxy pulbaar (of het proxyproject is aangevraagd), en het is bekend of wij op ODCN CRD's mogen installeren of dat het platformteam dat doet.
2. **Twee Kind-clusters, Skupper-controller + CRD's in beide, Site in `rig-interconnect` (naamvoorstel).**
   → verify: `kubectl get site` toont beide sites Ready.
3. **Hub bereikbaar maken: RouterAccess achter de ingress van cluster A met TLS-passthrough op SNI.**
   → verify: vanaf cluster B is de hub-hostname op 443 bereikbaar en serveert de router zijn cert.
4. **Link: AccessGrant op de hub (expiry kort, 1 redemption), AccessToken inlossen op cluster B.**
   → verify: `kubectl get link` toont de link Ready; een tweede inlossing van hetzelfde token wordt geweigerd (negatieve test).
5. **Mailserver delen: Connector (poort 465/587) op de hub bij de bestaande Mailpit/mailserver, Listener in cluster B.**
   → verify: een pod in cluster B levert via de ClusterIP van de Listener een mail af die in de mailbox op cluster A verschijnt.
6. **Database delen: Connector op 5432, Listener in cluster B.**
   → verify: `psql` vanuit cluster B werkt; meet de latency per roundtrip en leg die vast in dit document als input voor de replica-afweging.
7. **Kwaadweer-tests.**
   → verify: link verbreken (Route dicht) en herstellen: de Listener-verbindingen herstellen zonder handmatig ingrijpen; router-pod killen: idem; een service zónder Connector is vanuit cluster B aantoonbaar onbereikbaar.
8. **NetworkPolicies om de router-namespace, zoals onder Beveiliging.**
   → verify: het legitieme pad werkt nog, al het andere niet.
9. **Kustomize-structuur**: alles uit stap 2 t/m 8 vastleggen onder `infrastructure/bootstrap/infrastructure/skupper/` met de standaard overlays, plus een Taskfile-task voor de token-uitgifte/inwisseling.
   → verify: kale `kustomize build` slaagt op alle overlays en een verse Kind-opbouw komt volledig uit git.
10. **Naar sandbox en daarna ODCN**: sandboxed-local als spoke aan een hub hangen, daarna de ODCN-aanvragen (ip_whitelist op de Route, NetworkPolicy naar de IngressController) en de productie-hub.
    → verify: de mailserver-flow uit stap 5, maar dan cross-omgeving.

## Openstaande vragen

- **Waar staat de hub in productie?** De cluster met de gedeelde services is de logische kandidaat, maar de hub is het enige inkomend bereikbare punt; als de gedeelde services later verhuizen, verhuist de RouterAccess mee of kiezen we een vaste "best bereikbare" cluster.
- **HA van de router**: v2 ondersteunt meerdere router-replica's per site; uitzoeken wat dat betekent voor lopende verbindingen bij een pod-restart, en of één replica voor onze eerste services volstaat.
- **Doorstaan Skupper-images de ODCN signature policy?** Eerdere ervaring leert dat image-policies op ODCN verrassen; dit zit daarom in stap 1.
- **Observability**: Skupper levert een network-observer/console; bepalen of we die uitrollen of volstaan met de bestaande Prometheus/Loki-lijn. Sluit aan bij de bredere service-monitoring-gap.
- **Latencybudget voor de database-usecase**: de meting uit stap 6 bepaalt of direct query-verkeer acceptabel is of dat we standaard op replica's sturen.
- **Vervalt de headscale-route hiermee?** Nee: headscale blijft het antwoord voor mens-naar-cluster (VLAM); Skupper is cluster-naar-cluster. Beide documenteren zodat ze niet als concurrenten gelezen worden.
