# Cross-domain toegang (service)

**Status**: Geïmplementeerd (RC-15)

Een platform-service waarmee een project declaratief vastlegt welke *andere* projecten,
deployments of componenten zijn pods mogen bereiken (**inbound**) en waar het zelf heen mag
(**outbound**), telkens op een expliciet benoemde poort. Het effect is een eigen
NetworkPolicy per deployment, naast de tenant-baseline.

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

- **Kapotte verwijzingen falen nooit de generatie.** Bestaat het doelproject/-deployment/
  -component niet (meer), draait het op een andere cluster, of verwijst een regel naar het
  eigen project, dan wordt die regel met een WARNING overgeslagen — de manifest-generatie gaat
  door.
- **Staleness.** De verwijzing wordt opgelost op het moment dat project A verwerkt wordt.
  Hernoemt project B daarna zijn namespace of component, dan is A's policy stil verouderd tot A
  opnieuw verwerkt wordt. Verwijderen is onschadelijk (de regel wijst naar iets dat niet
  bestaat). Automatisch her-verwerken van verwijzende projecten is buiten scope.
- **Helm/helmfile-workloads dragen geen `app`/`project`-label** en zijn dus nooit een peer.
- **Cross-cluster is onmogelijk** met NetworkPolicies en met het gedistribueerde OPI-model;
  elke OPI beheert alleen zijn eigen cluster.
- **De sandbox (kindnet) handhaaft NetworkPolicies niet.** Daar verifieer je de gegenereerde
  YAML en het opruimen; daadwerkelijk blokkeren stel je alleen op ODCN (Calico) vast.

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

## Afhankelijkheden

- Tenant-baseline NetworkPolicy (zie [restrictive-network-policies.md](restrictive-network-policies.md)),
  waar deze service additief naast komt te staan.
- De peer-pod-labels `app` en `project` uit `manifests/deployment.yaml.jinja` (bestonden al).
- ArgoCD `prune: true` + `selfHeal: true`, zodat een uit git verwijderde policy ook uit de
  cluster verdwijnt.
