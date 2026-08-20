# Cross-domain toegang (service)

**Status**: Geïmplementeerd (RC-15)

Een platform-service waarmee een project declaratief vastlegt welke projecten, deployments
of componenten zijn pods mogen bereiken (**inbound**) en waar het zelf heen mag
(**outbound**), telkens op een expliciet benoemde poort. Het effect is een eigen
NetworkPolicy per deployment, naast de tenant-baseline.

De tegenpartij mag ook het eigen project zijn. De tenant-baseline scheidt namelijk op
**deployment**, niet op project: twee deployments van hetzelfde project kunnen elkaar net zo
min bereiken als twee losse projecten. Zie *Binnen je eigen project* hieronder.

> **"Domain" gaat hier over netwerktoegang tussen projecten, niet over DNS-domeinen.** De
> DNS-kant (hostnames, certificaten, subdomein-goedkeuringen) is van `publish-on-web`. De term
> komt van de gebruiker en dekt de security-betekenis: het beveiligingsperimeter van een ander
> project.

## Wat het doet

Per deployment schrijft OPI standaard één tenant-baseline NetworkPolicy die cross-tenant
verkeer blokkeert. Deze service voegt daar, additief, een tweede policy per eigen component
aan toe die precies de door jou benoemde peers openzet. NetworkPolicies zijn additief: als
meerdere policies dezelfde pods selecteren, is het toegestane verkeer de unie van hun regels.

- Bestandsnaam: `<deployment>-cross-domain-access-<component>-network-policy.yaml` in
  `zad-deployments`.
- De policy selecteert jouw pods op `app: <deployment>-<component>` en zet per peer een
  `namespaceSelector` (op de namespace van de peer) én een `podSelector`
  (`app: <peer-deployment>-<peer-component>` + `project: <peer-project>`) in dezelfde
  peer-entry (AND).

## De ontvanger beslist

Een inbound-regel in project A is de toestemming; project B kan zichzelf geen toegang geven.
De outbound-lijst van B is (a) nodig voor poorten buiten 80/443 (egress naar 80/443 staat in
de baseline al open) en (b) expliciete intentie, zodat strengere egress later geen verrassing
wordt.

In de UI: *"de ontvanger bepaalt wie binnen mag; zet aan jouw kant ook de uitgaande regel,
anders is alleen verkeer op poort 80 en 443 mogelijk."*

## Binnen je eigen project

De tenant-baseline selecteert op het pod-label `deployment: <naam>` en laat alleen pods met
datzelfde label bij elkaar binnen. Deployments van één project delen wel een namespace, maar
staan daarmee net zo goed los van elkaar als deployments van twee verschillende projecten.
Verkeer van deployment `test` naar deployment `acceptatie` binnen één project heeft dus een
regel nodig, en het symptoom zonder die regel is een timeout (het pakket wordt gedropt), niet
een geweigerde verbinding.

Kies daarvoor gewoon je eigen project als tegenpartij. Er is geen apart mechanisme en geen
uitzondering in de resolutie: de peer-namespace is dan je eigen namespace en de pod-labels
zijn `app: <peer-deployment>-<peer-component>` + `project: <eigen project>`, precies zoals bij
een vreemde peer.

Wat wél anders voelt: je bent nu zelf ook de ontvanger, dus je schrijft beide regels in
hetzelfde projectbestand. Een outbound-regel bij het bellende component en een inbound-regel
bij het gebelde component. Zet je er maar één, dan blijft het verkeer hangen. Staan beide
regels op de projectlaag, dan komt elke regel vanzelf terecht bij de deployment die het
genoemde eigen component bevat.

Componenten binnen één deployment hebben hier niets voor nodig: die dragen hetzelfde
`deployment`-label en mogen elkaar op elke poort bereiken.

## De YAML-vorm

Elke regel heeft een verplichte, korte **`name`** en twee kanten, `from` en `to`. De **peer**
is de kant met `project`; welke kant dat is volgt uit de lijst: bij `inbound` is dat `from`,
bij `outbound` is dat `to`. De **poort staat altijd op `to`** (de ontvangende kant), wat de
Kubernetes-semantiek is.

### Rootniveau (basis voor elke deployment)

```yaml
services:
  - name: cross-domain-access
    schema-version: "1.0"
    config:
      inbound:
        # Hun api-component mag bij MIJN web-component binnen, op MIJN poort 8080.
        - name: van-regelrecht-api
          from: { project: regelrecht, deployment: prod, component: api }
          to:   { component: web, port: 8080 }
      outbound:
        # Spiegelbeeld: 'to' is de peer, de poort is HUN poort.
        - name: naar-regelrecht-api
          from: { component: web }
          to:   { project: regelrecht, deployment: prod, component: api, port: 8080 }
        # De peer-deployment mag op rootniveau open blijven; elke deployment vult hem zelf in.
        - name: naar-regelrecht-events
          from: { component: worker }
          to:   { project: regelrecht, component: events, port: 9090 }
```

### Deploymentniveau (partiële patch, gesleuteld op `name`)

De deployment-laag is een patch op de rootregels, gesleuteld op `name`:

- Zelfde `name`: **overschrijft veld voor veld** (wat je niet noemt, erf je) — meestal alleen
  `to.deployment` / `from.deployment` van de peer.
- Nieuwe `name`: komt erbij, volledig ingevuld.
- `disabled: true`: zet die ene geërfde regel uit voor deze deployment.

```yaml
deployments:
  - name: prod
    # Geen blok: erft de rootregels ongewijzigd (naar-regelrecht-api -> regelrecht/prod/api).
  - name: dev
    services:
      - reference: cross-domain-access
        config:
          outbound:
            - name: naar-regelrecht-api        # alleen de doeldeployment wijzigt
              to: { deployment: dev }
            - name: naar-regelrecht-events      # rootregel zonder peer-deployment: hier ingevuld
              to: { deployment: dev }
          inbound:
            - name: van-regelrecht-api
              from: { deployment: dev }
            - name: van-dp-bn7-worker
              disabled: true                    # geërfde regel uit voor deze deployment
```

Effectieve regels voor deployment D = de rootregels, per `name` gepatcht met D's regels, plus
D's eigen regels, minus de op `disabled` gezette, minus de regels die na de merge nog geen
peer-deployment hebben.

## Poortsemantiek (valkuil)

De poort is die van de **ontvangende kant** en het is de **containerpoort**, niet de
Service-poort — NetworkPolicy werkt op pod-niveau, na de DNAT van de Service. Bij ons zijn ze
meestal gelijk, met één belangrijke uitzondering: staat er een **authorization-wall** voor een
component, dan luistert de app zelf niet meer direct en is **4180** (de oauth2-proxy) de
bereikbare poort.

## Geen wildcards, geen brede grants

- **Geen `deployment: pr-*`.** Kubernetes `matchLabels` is exact en kent geen prefix-glob;
  uitrollen bij het genereren zou elke later ontstane PR-deployment stil missen. Wil je "alle
  pr-deployments", dan is het juiste antwoord een expliciet label op de peer-pods, geen glob.
- **Geen "heel project" of "hele deployment" variant.** Strikt targeten is het punt: een brede
  grant opent ook alles wat er morgen bijkomt. Prijs, expliciet: een nieuwe peer-deployment
  krijgt geen toegang tot je een regel toevoegt.

## Beperkingen

- **Kapotte verwijzingen falen nooit de generatie.** Bestaat het doeldeployment of -component
  niet (meer) in een project dat deze cluster wél kent, of draait het op een andere cluster,
  dan wordt die regel met een WARNING overgeslagen — de manifest-generatie gaat door.
- **Een peer-project dat deze cluster niet kent, is geen kapotte verwijzing.** Cross-*domain*
  betekent dat de andere kant elders beheerd kan worden of nog niet bestaat (project A wordt
  ingericht vóór project B). Zo'n regel levert wél een policy op, met een WARNING; de namespace
  volgt dan de conventie *namespace = projectnaam* (plus het clusterprefix), want er is geen
  projectbestand om hem uit te lezen. Dat geeft niemand toegang die hij anders niet had: een
  NetworkPolicy-peer zegt alleen wie er mág praten, en de **ontvanger** bepaalt met zijn eigen
  policy of hij binnenlaat. De pod-labels blijven bovendien `project: <peer>` eisen, dus een
  namespace die toevallig zo heet maar van iemand anders is, matcht niets.
- **Staleness.** De verwijzing wordt opgelost op het moment dat project A verwerkt wordt.
  Hernoemt project B daarna zijn namespace of component, dan is A's policy stil verouderd tot A
  opnieuw verwerkt wordt. Verwijderen is onschadelijk (de regel wijst naar iets dat niet
  bestaat). Automatisch her-verwerken van verwijzende projecten is buiten scope.
- **Helm/helmfile-workloads dragen geen `app`/`project`-label** en zijn dus nooit een peer.
- **Cross-cluster is onmogelijk** met NetworkPolicies en met het gedistribueerde OPI-model;
  elke OPI beheert alleen zijn eigen cluster.
- **De sandbox handhaaft NetworkPolicies WEL.** Hier stond het tegendeel ("kindnet doet niets
  met NetworkPolicies"), en dat is niet waar: gemeten op 2026-08-20 (RC-144) haalt
  een afnemer-pod `/v1/models` op van een stub in een andere namespace zolang de inbound-regel
  er staat, en loopt diezelfde aanroep in een time-out zodra die regel is weggehaald. Zie
  `tests/e2e/test_sandbox_vlam.py`, dat die handhaving eerst MEET en de negatieve meting
  overslaat als een cluster niets afdwingt. Een blokkade in de sandbox is dus een echte
  uitspraak; alleen de vorm van de policy blijft ook zonder cluster te toetsen.

## API

De service is via de generieke, registry-gedreven config-API configureerbaar (geen
service-eigen endpoint):

```
GET  /api/v2/projects/{project}/services/cross-domain-access/config
PUT  /api/v2/projects/{project}/services/cross-domain-access/config/project
PUT  /api/v2/projects/{project}/services/cross-domain-access/config/deployment/{deployment}
```

De request-body is het getypeerde `CrossDomainAccessConfig`-model, dus de OpenAPI-spec
documenteert de velden per service. PUT vervangt de hele config van die laag; een client die
een regel wil toevoegen doet GET, lijst aanvullen, PUT.

## Configuratie

Aanzetten doe je met de service-selectie (wizard-kaart of `POST /api/projects/{name}/services`);
configureren met de UI-modal of de config-API hierboven. De service provisioneert niets
server-side: het effect zit volledig in de gegenereerde manifests, en de generieke
service-manifest-prune ruimt de policy-bestanden op zodra de service wordt uitgezet.

## Bewijs (waar de keten getest wordt)

- `tests/test_cross_domain_access.py` — merge, resolve, template en het formulier per onderdeel.
- `tests/test_cross_domain_chain.py` — de keten vanaf de formulier-processor: submissie →
  projectbestand → manifest, inclusief de per-deployment patch.
- `tests/e2e/test_wizard_cross_domain_policy.py` — dezelfde keten, maar begonnen in de
  **browser**: de create-wizard wordt echt doorlopen en ingevuld (de trapsgewijze selects
  inbegrepen), en de YAML die de wizard indient levert een NetworkPolicy op. Draai met
  `-m "e2e and not sandbox"`.

## Afhankelijkheden

- Tenant-baseline NetworkPolicy (zie [restrictive-network-policies.md](restrictive-network-policies.md)),
  waar deze service additief naast komt te staan.
- De peer-pod-labels `app` en `project` uit `manifests/deployment.yaml.jinja` (bestonden al).
- ArgoCD `prune: true` + `selfHeal: true`, zodat een uit git verwijderde policy ook uit de
  cluster verdwijnt.
