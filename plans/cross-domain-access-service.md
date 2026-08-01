# Cross-domain toegang (service)

Status: PLAN, nog niet gebouwd. Basis-branch: `uniform-declarative-platform-services`.

Doel: een nieuwe platform-service `cross-domain-access` waarmee een project declaratief
vastlegt welke *andere* projecten, deployments of componenten zijn pods mogen bereiken
(inbound) en waar het zelf heen mag (outbound), telkens op expliciet benoemde poorten. Het
effect is een eigen NetworkPolicy per deployment, naast de tenant-baseline. De keuzelijst
toont alleen projecten waar de ingelogde gebruiker rechten op heeft.

Lees eerst `instructions/services.md`. Dit plan volgt dat contract; als je generieke code
moet aanpassen buiten wat in Stap 4 staat, klopt er iets niet.

---


> **Bijgewerkt 1 augustus 2026.** Dit plan is geschreven vóór de afronding van de service-opzet. Wat er sindsdien veranderd is en wat je dus niet opnieuw hoeft te doen: `$defs/deployment-service-config` is opengezet (stap 2.4 vervalt), `validate_service_configs` loopt over alle vier de configlagen, `Service.config_model_for(layer)` bestaat voor een service die per laag een ander model draagt, dertien van de vijftien services hebben nu een configmodel met drift-gelockt fragment, en er staan geen servicenamen meer als contract in het globale schema. Lees `instructions/service-review-checklist.md` voordat je begint, en gebruik die als sluitstuk. Let op de coördinatie met `plans/oom-auto-tune-deployment-scoped.md`: dat plan introduceert een `HookPoint`-enum en dit plan een haak `contribute_deployment_manifests`. Die twee moeten één mechanisme worden, niet twee naast elkaar; stem af met wie dat plan uitvoert.

## 1. Hoe het vandaag werkt

Per deployment schrijft OPI precies een NetworkPolicy:

- Template: `operations-manager/python/manifests/tenant-baseline-network-policy.yaml.jinja`
- Emissie: `opi/manager/project_manager.py`, aan het eind van
  `create_application_manifests()` (zoek op `tenant-baseline`, rond regel 5978), na de
  component-loop, altijd, ongeacht welke services aan staan.
- Tweede call-site: de infrastructure-namespace (CNPG) rond regel 2085, met
  `deployment_selector: None`.

De policy selecteert pods op label `deployment: <naam>` en staat toe:

| Richting | Toegestaan |
|---|---|
| ingress | eigen deployment-pods, ingress-controller, ops-namespace, backup-namespace, optioneel `allowed_ingress_namespaces` |
| egress | DNS, eigen deployment-pods, ops-namespace, backup-namespace, project-infra-namespace, `0.0.0.0/0` op TCP 80 en 443 |

Labels op elke door OPI beheerde pod (`manifests/deployment.yaml.jinja`):

```yaml
app: "<deployment>-<component>"   # generate_unique_name(), utils/naming.py:160
deployment: "<deployment>"
project: "<project>"
component: application            # letterlijke waarde, GEEN componentnaam
```

Die drie eerste labels zijn precies de granulariteit die dit plan nodig heeft, en ze
bestaan al. Er hoeft geen enkel nieuw label bij.

Drie eigenschappen die het ontwerp sturen:

1. **Deployments in dezelfde namespace zien elkaar niet.** Isolatie loopt via het
   `deployment`-label, niet via de namespace.
2. **Egress naar 80/443 staat al open naar alles**, inclusief pod-IP's in de cluster
   (`ipBlock: 0.0.0.0/0` kent geen `except` voor cluster-CIDR). Voor die twee poorten is
   de ontvangende kant dus de enige echte poort. Voor elke andere poort is de
   egress-regel wel nodig.
3. **Handhaving**: ODCN draait Calico, dat handhaaft. De lokale Kind-sandbox draait kindnet
   en handhaaft NetworkPolicies **niet**. In de sandbox verifieer je de gegenereerde YAML
   en het opruimen, niet het werkelijke blokkeren.

---

## 2. Ontwerpbeslissingen

### 2.1 Wie beslist over toegang

De ontvanger. Een inbound-regel in project A is de toestemming; project B kan zichzelf
geen toegang geven. De outbound-lijst van B is (a) noodzakelijk voor poorten buiten
80/443 en (b) expliciete intentie, zodat strengere egress later geen verrassing wordt.

Consequentie voor de UI-tekst: schrijf niet "beide projecten moeten dit instellen anders
werkt het niet". Schrijf: "de ontvanger bepaalt wie binnen mag; zet aan jouw kant ook de
uitgaande regel, anders is alleen verkeer op poort 80 en 443 mogelijk."

### 2.2 Twee lagen met benoemde regels en een partiele patch

Twee lagen (`ConfigLayer.PROJECT` en `ConfigLayer.DEPLOYMENT`), net als env-vars:

- **Project** (`services/cross-domain-access/config`): geldt voor elke deployment van het
  project, ook voor deployments die later ontstaan. Dit is meteen het antwoord op de
  wildcard-vraag (zie 2.6).
- **Deployment** (`deployments[*]/services{cross-domain-access}/config`): past regels aan of
  voegt er toe voor precies die deployment.

Een unie van twee lijsten is hier **niet genoeg**, en dat is het echte modelleerprobleem.
De inbound-kant verandert doorgaans niet per deployment, maar de outbound-kant wel: mijn
`dev` moet naar hun `dev` en mijn `prod` naar hun `prod`. Met een unie kun je alleen een
regel toevoegen of alle projectregels weggooien, niet "dezelfde regel, ander doel".

Daarom krijgt elke regel een verplichte, korte **`name`**, en is de deployment-laag een
partiele patch op sleutel:

- Deployment-regel met een `name` die op rootniveau bestaat: **overschrijft veld voor veld**.
  Wat je niet noemt, erf je.
- Deployment-regel met een nieuwe `name`: komt erbij, volledig ingevuld.
- `disabled: true` op deployment-niveau: zet die ene geerfde regel uit voor deze deployment.

**De deployment bepaalt dus wat er uiteindelijk geldt**, en verwijst per `name` naar de
rootdefinitie. De root is de gedeelde basis en het enige wat de deployment daarvan in de
praktijk aanpast is de deployment-naam van de peer (2.3). Dat is ook precies wat de UI moet
laten zien, zie Stap 6.

De sleutel moet expliciet zijn en kan niet impliciet uit de inhoud volgen: een impliciete
sleutel zou de peer moeten bevatten, en juist de peer is het veld dat je wilt overschrijven.
Twee projectregels naar hetzelfde peer-project (bijvoorbeeld naar `api` en naar `worker`)
zouden bovendien op dezelfde impliciete sleutel botsen.

Elk veld van een regel is een scalair (zie 2.3 en 2.5), dus een patch is altijd
veldvervanging en nooit een halve samenvoeging van een lijst. Dat is bewust: het maakt de
uitkomst van een override voorspelbaar zonder dat je de merge-regels hoeft te kennen.

Er is bewust geen `inherit-project: false`. Per-regel `disabled` is fijner en strikt
expressiever dan die botte bijl.

### 2.3 De vorm van een regel: from en to

Een regel is opgebouwd uit precies de begrippen die een gebruiker kent: **project,
deployment, component, poort**, en heeft altijd twee kanten:

```yaml
inbound:
  - name: van-regelrecht-api
    from:                    # de peer
      project: regelrecht
      deployment: prod
      component: api
    to:                      # mijn kant
      component: web
      port: 8080

outbound:
  - name: naar-regelrecht-api
    from:                    # mijn kant
      component: web
    to:                      # de peer
      project: regelrecht
      deployment: prod
      component: api
      port: 8080
```

Drie regels die hieruit volgen en die het model dragen:

1. **De peer is de kant met `project`.** Welke kant dat is volgt uit de lijst waarin de regel
   staat: bij `inbound` is dat `from`, bij `outbound` is dat `to`. Valideer het ook zo:
   `project` op de eigen kant is een fout, niet iets dat je stilzwijgend negeert.
2. **De eigen kant heeft alleen een component.** Geen `project` (dat ben je zelf) en geen
   `deployment` (die volgt uit waar de regel geldt, zie 2.4).
3. **De poort staat altijd op de `to`-kant.** Dat is geen conventie maar precies de
   Kubernetes-semantiek: een `ingress`-regel filtert op de poort van de ontvangende pod, een
   `egress`-regel op de poort van de bestemming. Door de poort structureel bij `to` te zetten
   kun je hem niet aan de verkeerde kant invullen. Zie 2.5 voor welke poort dat is.

Alle velden zijn verplicht: `project`, `deployment` en `component` op de peer-kant,
`component` op de eigen kant, en `port` op de `to`-kant.

**Met een uitzondering: de `deployment` van de peer mag op rootniveau open blijven.** Dat is
precies het veld waarvoor de tweede laag bestaat (2.2), en soms weet de root het antwoord
niet: "wij koppelen met regelrecht/api op 8080, welke omgeving dat is bepaalt de deployment".
Elk ander veld moet op rootniveau wel ingevuld zijn.

Blijft de peer-deployment na de merge alsnog leeg, dan is die regel voor die deployment niet
compleet: WARNING loggen, regel overslaan, en het in de UI tonen als "nog niet ingesteld voor
deze deployment". Niet hard falen, want een half ingevulde rootregel mag niet elke deployment
van dat project blokkeren.

De peer levert altijd dezelfde selector op: `namespaceSelector` op de namespace van die
deployment, plus in dezelfde peer-entry `podSelector` met **twee** labels:

```yaml
podSelector:
  matchLabels:
    app: "prod-api"          # generate_unique_name(deployment, component)
    project: "regelrecht"    # sluit een gedeelde namespace uit
```

Het `project`-label kost niets en sluit het laatste gat: `namespace` is een vrij veld per
deployment, dus twee projecten kunnen dezelfde namespace kiezen. Met beide labels is de
selectie eenduidig.

Geen bredere vormen. Er is bewust geen "heel project" of "hele deployment" variant:

- **Strikt is het punt.** Een grant op projectniveau opent ook alles wat er morgen bij komt,
  inclusief workloads die niets met de koppeling te maken hebben. Dat is precies het
  bleeding tussen deployments en projecten dat deze service moet voorkomen.
- **Het is ook eerlijker.** Een brede grant ziet er in de UI onschuldig uit en is in de
  cluster de grootste opening die je kunt maken.
- **Prijs, expliciet:** een nieuwe peer-deployment (bijvoorbeeld een PR-omgeving) krijgt geen
  toegang tot je een regel toevoegt. Dat is de bedoeling, en het maakt de
  `{deployment}`-placeholder uit sectie 7 een serieuzere kandidaat voor de volgende ronde.

Aparte velden, geen samengestelde string, want dit is precies wat de laag-patch uit 2.2
bruikbaar maakt: een deployment overschrijft dan letterlijk alleen `deployment:` en erft
project, component en poort. Dat is de meest voorkomende override ("mijn dev praat met hun
dev").

Voor de UI heeft dat wel een gevolg, zie Stap 5: het formulier kan geen per-rij afhankelijke
selects (`_build_provider_context` in `opi/forms/renderer.py` bouwt context per formulier,
niet per rij), dus de drie peer-velden worden in het formulier gepresenteerd als **een** select
met vooraf uitgerekende opties, die bij het opslaan wordt gesplitst. Opslagvorm en
formuliervorm mogen verschillen; de opslagvorm is leidend.

### 2.4 De eigen deployment staat er niet bij

De eigen kant noemt alleen een component. Welke deployment dat betreft volgt uit de plek waar
de regel geldt: een rootregel geldt voor elke deployment die dat component heeft, een
deployment-regel voor die ene. Heeft een deployment het genoemde component niet, dan levert
de regel daar niets op (WARNING, overslaan, zie 2.7).

Sta `deployment` op de eigen kant dus ook niet toe. Wie een rootregel maar voor een van zijn
deployments wil, zet hem op deployment-niveau; dat is waar de tweede laag voor is. Een tweede
manier om hetzelfde te zeggen levert alleen verwarring op over welke wint.

Gevolg voor de emissie: elke gegenereerde policy is component-scoped, en er komt er een per
eigen component dat regels heeft. Zie 2.8.

### 2.5 Welke poort

Een regel heeft **een** poort, op de `to`-kant (2.3). Twee poorten naar dezelfde peer zijn
twee regels met een eigen `name`. Dat houdt de laag-patch triviaal (elk veld is een scalair,
er is geen lijst die je bij een override half zou moeten samenvoegen) en het sluit aan bij hoe
een gebruiker het benoemt. De emitter groepeert regels met dezelfde eigen scope en dezelfde
peer weer samen tot een `ports`-lijst in de policy, dus de gegenereerde YAML blijft compact.

Dat de poort bij de ontvanger hoort is met deze vorm structureel geregeld. Wat overblijft is
een valkuil die je moet documenteren en in de UI moet afvangen:

**Het is de containerpoort, niet de Service-poort.** NetworkPolicy werkt op pod-niveau, na de
DNAT van de Service. In de praktijk zijn ze bij ons gelijk (`project_manager.py` geeft
`service_port = application_port` mee), met een belangrijke uitzondering: staat er een
**authorization-wall** voor een component, dan zet die service `service_port` op **4180** (de
oauth2-proxy) en luistert de app zelf niet meer direct. Voor zo'n component is 4180 de
bereikbare poort.

De poortkeuze in de UI is daarom een select, gevuld uit `ports.inbound` van het component op
de `to`-kant (bij inbound dus uit het eigen projectbestand, bij outbound uit dat van de peer),
met 4180 erbij als dat component een authorization-wall heeft.

### 2.6 Geen wildcards

Overwogen: `deployment: pr-*` zodat elke PR-deployment van een ander project onder de regel
valt. Niet doen:

- **Kubernetes kan het niet.** `matchLabels` is exact; `matchExpressions` kent In, NotIn,
  Exists en DoesNotExist, geen prefix-glob. Je zou de wildcard bij het genereren moeten
  uitrollen naar de deployments die op dat moment bestaan. Dan mist elke PR-deployment die
  daarna ontstaat de regel, tot het verwijzende project toevallig opnieuw verwerkt wordt.
  Dat is een stille faalmodus in precies het meest vluchtige scenario.
- **Het strijdt met 2.3.** Een glob is een brede grant met een nette jas aan. Als we bewust
  geen "heel project" toestaan, is `pr-*` dat ook niet.

Als er ooit toch behoefte is aan "alle pr-deployments maar niet prod", is het juiste antwoord
een expliciet label op de peer-pods waar je op selecteert, geen glob in ons config-bestand.
Dan is de peer-kant het die het label zet, en dat is meteen de juiste plek voor die
beslissing.

### 2.7 Hoe een peer wordt opgelost naar YAML

Via `get_project_store().get(<project>).data` (in-memory, geen I/O) naar de genoemde
deployment, gecontroleerd op `deployment["cluster"] == <onze cluster>`, dan
`get_prefixed_namespace(cluster, deployment["namespace"])`.

Namespace-selectie op `kubernetes.io/metadata.name`, het label dat de apiserver zelf op elke
namespace zet, precies zoals de bestaande regels in de baseline. Pod-selectie op `app` +
`project` volgens 2.3.

Omdat alle drie de peer-velden verplicht zijn, is elk ervan ook controleerbaar. Randgevallen,
allemaal WARNING loggen met de `name` van de regel erbij en de regel overslaan, nooit de
manifest-generatie laten falen:

- doelproject bestaat niet (meer)
- de genoemde deployment bestaat niet in dat project
- die deployment draait op een andere cluster (cross-cluster NetworkPolicy bestaat niet; elke
  OPI beheert alleen zijn eigen cluster)
- het genoemde component zit niet in die deployment
- het eigen component uit de regel zit niet in de deployment waarvoor we nu genereren (normaal bij een
  rootregel die niet op elke deployment van toepassing is)
- verwijzing naar het eigen project (zinloos, filter weg)

Helm- en helmfile-workloads dragen geen `app`/`project`-label en zijn dus nooit een peer.
Noem dat in de documentatie; het is een bewuste consequentie van strikt targeten.

Bekende beperking, documenteren: de verwijzing wordt opgelost op het moment dat project A
verwerkt wordt. Hernoemt project B daarna zijn namespace of component, dan is A's policy
stil verouderd tot A opnieuw verwerkt wordt. Verwijderen is onschadelijk (de regel wijst
naar iets dat niet bestaat, dus hij doet niets). Automatisch her-verwerken van verwijzende
projecten is bewust buiten scope.

### 2.8 Waar de regels landen in de YAML

**In eigen, service-gebonden NetworkPolicies**, niet in de tenant-baseline. Een policy per
eigen component dat regels heeft (2.4):

- `<deployment>-cross-domain-access-<component>-network-policy.yaml`

Dat mag, want NetworkPolicies zijn additief: selecteren meerdere policies dezelfde pods, dan
is het toegestane verkeer de unie van hun regels. Winst: de baseline blijft een
platformgarantie waar geen service in schrijft, en `kubectl get netpol` laat zien wie wat
heeft opengezet. Het past ook bij het servicecontract: een service levert een declaratieve
bijdrage, generieke code schrijft die weg.

Wat je daarvoor moet oplossen, en wat dit plan dus doet: **opruimen**.
`_select_obsolete_component_manifests` (project_manager.py:224) ruimt alleen bestanden op met
een component-prefix. Een service-eigen deployment-manifest valt daarbuiten en zou blijven
staan nadat de service is uitgezet, en dan blijft de opening bestaan. Daarom komt er in
Stap 4 een tweede, symmetrische opruimregel op service-prefix. Die is meteen bruikbaar voor
elke volgende service die een deployment-breed manifest wil schrijven.

Verwijderen uit git is genoeg om de resource ook uit de cluster te krijgen: de ArgoCD
Application staat op `prune: true` en `selfHeal: true`
(`manifests/argocd-application.yaml.jinja`).

Twee regels voor het template:

- **Schrijf het bestand niet als er geen enkele peer is.** Laat de opruimstap het dan
  verwijderen.
- **Neem een `policyType` alleen op als die richting peers heeft.** Een policy met
  `policyTypes: [Egress]` en een lege `egress`-lijst isoleert de pod voor egress zonder iets
  toe te staan. Naast de baseline verandert dat vandaag niets, maar het is een valstrik die
  je niet wilt laten liggen.

### 2.9 Naamgeving

`cross-domain-access` botst semantisch met "domain" in de betekenis DNS-domein, dat overal in
deze codebase gebruikt wordt (publish-on-web, domain-approvals, `base-domain`). De term komt
van de gebruiker en dekt de security-betekenis. Houd hem aan, maar zet in de docstring van
het package en in de UI-omschrijving expliciet: dit gaat over netwerktoegang tussen
projecten, niet over DNS-domeinen.

---

## 3. Config-vorm

### Rootniveau: de basis voor elke deployment

```yaml
name: mijnproject
services:
  - name: cross-domain-access
    schema-version: "1.0"
    config:

      inbound:
        # Hun api-component mag bij MIJN web-component binnen, op MIJN poort 8080.
        # 'from' draagt een project, dus dat is de peer; 'to' is mijn kant.
        - name: van-regelrecht-api
          from:
            project: regelrecht
            deployment: prod
            component: api
          to:
            component: web
            port: 8080

        # Een tweede bron, ook volledig uitgeschreven. Er is geen
        # "heel project" of "hele deployment" vorm.
        - name: van-dp-bn7-worker
          from:
            project: dp-bn7
            deployment: prod
            component: worker
          to:
            component: api
            port: 8080

      outbound:
        # Spiegelbeeld: 'from' is nu mijn kant, 'to' de peer, en de poort staat
        # weer bij 'to' omdat dat de ontvanger is. Dus HUN poort 8080.
        - name: naar-regelrecht-api
          from:
            component: web
          to:
            project: regelrecht
            deployment: prod
            component: api
            port: 8080

        # Peer-deployment bewust opengelaten: welke omgeving van hun we aanspreken
        # bepaalt elke deployment zelf. Een deployment die hem niet invult krijgt
        # deze regel niet (met een melding in de UI).
        - name: naar-regelrecht-events
          from:
            component: worker
          to:
            project: regelrecht
            component: events
            port: 9090
```

### Deploymentniveau: de partiele patch

```yaml
deployments:

  - name: prod
    cluster: odcn-production
    namespace: mijnproject
    # Geen cross-domain-access blok: prod erft de rootregels ongewijzigd.
    # naar-regelrecht-api wijst dus naar regelrecht/prod/api.

  - name: dev
    cluster: odcn-production
    namespace: mijnproject
    services:
      - reference: cross-domain-access
        config:

          outbound:
            # Zelfde name, dus een patch op de rootregel. Alleen de doeldeployment
            # wijzigt; project, component, port en de hele 'from' worden geerfd.
            # Resultaat: mijn web praat met regelrecht/dev/api op 8080.
            - name: naar-regelrecht-api
              to:
                deployment: dev

            # Rootregel die zijn peer-deployment openliet: hier ingevuld.
            - name: naar-regelrecht-events
              to:
                deployment: dev

            # Nieuwe name: eigen regel van deze deployment, dus alle velden.
            - name: naar-sandbox
              from:
                component: web
              to:
                project: sandbox-project
                deployment: dev
                component: api
                port: 9000

          inbound:
            # Spiegelbeeld: hun dev mag bij mijn dev, in plaats van hun prod.
            - name: van-regelrecht-api
              from:
                deployment: dev

            # Geerfde regel uit voor deze deployment.
            - name: van-dp-bn7-worker
              disabled: true
```

Effectieve regels voor deployment D = de rootregels, per `name` gepatcht met D's regels,
plus D's eigen regels, minus de op `disabled` gezette, minus de regels die na de merge nog
geen peer-deployment hebben. De patch gaat een niveau diep: velden van de regel, en velden
binnen `from`/`to`. Dieper hoeft niet, want dat is het hele schema. Daarna dedupliceren en
sorteren zodat de render stabiel is.

Wat de twee deployments hierboven opleveren:

| Deployment | Richting | Regel | Peer | Mijn kant | Poort |
|---|---|---|---|---|---|
| prod | inbound | van-regelrecht-api | `regelrecht` / `prod` / `api` | `web` | 8080 |
| prod | inbound | van-dp-bn7-worker | `dp-bn7` / `prod` / `worker` | `api` | 8080 |
| prod | outbound | naar-regelrecht-api | `regelrecht` / `prod` / `api` | `web` | 8080 |
| prod | outbound | naar-regelrecht-events | geen peer-deployment, dus overgeslagen | | |
| dev | inbound | van-regelrecht-api | `regelrecht` / `dev` / `api` | `web` | 8080 |
| dev | inbound | van-dp-bn7-worker | uitgezet | | |
| dev | outbound | naar-regelrecht-api | `regelrecht` / `dev` / `api` | `web` | 8080 |
| dev | outbound | naar-regelrecht-events | `regelrecht` / `dev` / `events` | `worker` | 9090 |
| dev | outbound | naar-sandbox | `sandbox-project` / `dev` / `api` | `web` | 9000 |

Merk op dat `prod` geen enkel configblok heeft en toch drie regels krijgt, en dat `dev` met
vier kleine ingrepen een compleet eigen set heeft. Dat is de verhouding die het model moet
opleveren: de root draagt de betekenis, de deployment alleen het verschil.

---

## 4. Implementatiestappen

Werk in deze volgorde: de service bestaat eerst als data, dan als effect, dan als UI.

### Stap 0. Vertrekpunt groen

```bash
cd operations-manager/python
uv run pytest tests/test_service_providers.py tests/test_service_config_schema.py \
              tests/test_golden_manifests.py tests/test_flow_registry_snapshot.py -q
uv run pytest tests/test_tenant_baseline_netpol.py -q --noconftest
```

Verify: alles groen voordat je iets aanraakt. Noteer de uitkomst.

### Stap 1. Service-identiteit

1. `opi/services/services_enums.py`: `CROSS_DOMAIN_ACCESS = "cross-domain-access"`.
2. `opi/services/services.py`: `ServiceDefinition` in `SERVICE_DEFINITIONS`:
   `scope="deployment"`, geen `variables`, geen `secret_class`, geen `backup_label`,
   `cleanup_strategy="none"` (geen server-side resources; het effect zit in gegenereerde
   manifests, die door Stap 4d verdwijnen). Kies icoon en kleur in lijn met de rest.
3. `opi/services/catalog/cross_domain_access/__init__.py` met een `Service`-subclass.
4. Een regel in `opi/services/registry.py` `SERVICES`.

Verify: `uv run pytest tests/test_service_providers.py -q` groen; service verschijnt op de
`/services`-pagina.

### Stap 2. Config-model en schema

1. `catalog/cross_domain_access/config_model.py`:
   - `CrossDomainAccessConfig`: `inbound: list[InboundRule]`, `outbound: list[OutboundRule]`,
     `model_config = ConfigDict(extra="forbid")`.
   - Vier kleine modellen, want de vier hoeken van een regel verschillen echt:
     `PeerRef` (`project`, `deployment: str | None`, `component`), `PeerTarget` (idem plus `port`),
     `LocalRef` (`component`) en `LocalTarget` (`component`, `port`). Elk veld tegen het
     DNS-1123-patroon uit `project_v2.json`; `port` tussen 1 en 65535. Dit is vormvalidatie;
     of het project bestaat is een runtime-vraag, geen schemavraag (het doelproject kan op een
     andere cluster leven).
   - `InboundRule`: `name` (verplicht, DNS-1123, max ~40), `from_: PeerRef` (alias `from`),
     `to: LocalTarget`, `disabled: bool = False`.
     `OutboundRule`: `name`, `from_: LocalRef`, `to: PeerTarget`, `disabled`.
   - De typering doet het validatiewerk uit 2.3 vanzelf: `project` op de eigen kant en `port`
     op de `from`-kant bestaan simpelweg niet in het model en worden door `extra="forbid"`
     afgewezen. Controleer dat de foutmelding leesbaar is en de `name` noemt; dat is wat een
     gebruiker van de API terugkrijgt.
   - **Twee validatieniveaus, want de deployment-laag is een patch.** Bovenstaande gelden voor
     een volledige regel, dus na de merge in Stap 3. In de opgeslagen laag is alles behalve
     `name` optioneel, anders kun je geen patch schrijven die alleen `to.deployment` zet.
     Modelleer dat als een patch-variant per model (`InboundRulePatch`, `PeerRefPatch`, ...)
     met dezelfde velden maar alles optioneel. Een vlag op een gedeeld model is korter maar
     leest slechter en laat pyright niets zien.
   - Belangrijk gevolg: het **rootniveau** slaat op als patch-model, want een rootregel mag
     onvolledig zijn zolang geen enkele deployment hem gebruikt. De volledigheidseis geldt bij
     de merge, met een melding die de `name` en het ontbrekende veld noemt. Dat is ook de plek
     waar de foutmelding het bruikbaarst is.
   - `name` uniek binnen een richting binnen een laag; een dubbele `name` is een fout, geen
     stille laatste-wint.
2. Zet `config_model` + `config_schema_version = "1.0"` op de service.
3. `uv run python -m opi.services.config_schema` en commit
   `catalog/cross_domain_access/cross-domain-access.v1.0.json`.
4. **`$defs/deployment-service-config` is al verruimd, deze stap vervalt.** Op 1 augustus is dat
   object opengezet en is `$defs/deployment-service` uitgebreid met `name` en `schema-version`,
   zodat elke service deployment-level config kan dragen. Tegelijk loopt `validate_service_configs`
   sinds die datum over alle vier de configlagen, dus de controle op de inhoud is verhuisd naar
   het servicemodel. Doe hier niets; controleer alleen dat je model die laag daadwerkelijk dekt.

Verify:
- `uv run pytest tests/test_service_config_schema.py -q`
- Een test die een projectdict met project- en deployment-level cross-domain-config door
  `validate_project_schema` haalt, en die faalt op een onbekend veld en op een lege
  poortlijst. Draai `migrate_to_latest()` voor validatie, niet op de ruwe file.

### Stap 3. Regels samenvoegen en oplossen als pure functies

**3a. Merge.** `catalog/cross_domain_access/merge.py`:

```python
def merge_rules(project_rules: list[dict], deployment_rules: list[dict]) -> list[dict]
```

Per `name`: rootregel als basis, deployment-velden eroverheen. **Een niveau diep**: velden
van de regel, en velden binnen `from`/`to`. Alleen aanwezige sleutels overschrijven, dus geen
`None`-velden uit een gedeserialiseerd model die een geerfde waarde wissen. `disabled: true`
laat de regel vallen, onbekende `name` op deployment-niveau komt erbij. Volgorde van het
resultaat: rootregels in hun eigen volgorde, daarna de nieuwe deployment-regels. Valideer het
resultaat tegen het volledige model uit Stap 2. Twee soorten onvolledigheid, bewust
verschillend behandeld:

- **peer-deployment ontbreekt**: geen fout, maar overslaan met een WARNING en een markering
  die de UI kan tonen (2.3). De root mag dat veld openlaten.
- **elk ander veld ontbreekt**: fout, met een leesbare melding die de `name` en het veld
  noemt. Zo'n regel had op rootniveau al niet mogen bestaan.

Deze functie is het hart van de patch-vraag en verdient de meeste tests: patch van alleen
`to.deployment` (moet project, component, port en de hele `from` erven), patch die niets
overschrijft, nieuwe regel, disabled, dubbele name, een rootregel zonder peer-deployment die
wel en niet wordt ingevuld, en een regel die na de merge een ander veld mist.

**3b. Oplossen.** `catalog/cross_domain_access/resolve.py`:

```python
@dataclass(frozen=True)
class PeerSelector:
    namespace: str
    pod_labels: dict[str, str]          # altijd {"app": f"{d}-{c}", "project": p}

@dataclass(frozen=True)
class ResolvedRule:
    local_component: str                # het component aan mijn kant
    peer: PeerSelector
    port: int

def resolve_rules(
    rules: list[dict],
    *,
    cluster: str,
    self_project: str,
    lookup_project: Callable[[str], dict | None],
) -> list[ResolvedRule]
```

`lookup_project` wordt geinjecteerd (in productie `get_project_store().get(name).data`), zodat
deze module zonder store, git of FastAPI te testen is. Houd hem vrij van zware imports.

Gedrag: eigen project wegfilteren, op cluster filteren, dedupliceren, sorteren op
`(local_component, namespace, sorted(pod_labels.items()), port)`, en per overgeslagen
regel een WARNING met reden.

Verify: unit tests voor de selectorvorm uit 2.3 (beide labels aanwezig), alle randgevallen uit
2.7, en de dedup/sortering.

### Stap 4. Effect: eigen NetworkPolicies per deployment

De enige stap die generieke code raakt. De baseline blijft ongemoeid; wat je bouwt is een
generiek mechanisme "een service mag deployment-brede manifests bijdragen", waarvan
cross-domain-access de eerste gebruiker is.

**4a. Hook in `opi/services/catalog/base.py`.** Naast `contribute_manifest_context`, want die
draait per component en dat is de verkeerde granulariteit.

```python
@dataclass
class DeploymentManifestContext:
    project_name: str
    project_data: dict[str, Any]
    deployment: dict[str, Any]
    cluster: str
    namespace: str

@dataclass
class DeploymentManifestSpec:
    #: Basename zonder .yaml. MOET beginnen met f"{deployment_name}-{service_type.value}-",
    #: want daar hangt de opruimregel in 4d aan.
    filename: str
    template_path: str
    values: dict[str, Any]

class Service:
    def contribute_deployment_manifests(self, ctx) -> list[DeploymentManifestSpec]:
        return []
```

`opi/services/registry.py` krijgt `deployment_manifest_services()`, gesorteerd op
`manifest_order`, net als `manifest_secret_services()`.

**4b. Nieuw template `manifests/service-network-policy.yaml.jinja`.** Generiek: `name`,
`namespace`, `pod_selector` (labels-dict), `ingress` en `egress` als lijsten van
`{peer: {namespace, pod_labels}, ports: [...]}`. Per peer een `namespaceSelector` op
`kubernetes.io/metadata.name` en, bij gevulde `pod_labels`, een `podSelector` in **dezelfde
lijst-entry** (AND, niet OR). Kopieer de indentatie van het `ingress_controller_selector`-blok
in de baseline, dat is exact dezelfde vorm. `policyTypes` bevat alleen richtingen die regels
hebben.

**4c. Emissie in `opi/manager/project_manager.py`**, in `create_application_manifests()` bij
het bestaande tenant-baseline-blok aan het eind (zoek op `tenant-baseline`, rond regel 5978).
Loop over `deployment_manifest_services()`, roep de hook aan, schrijf elke spec met
`self._manifest_generator.create_manifest_file(..., use_sops=False)` en voeg de basename toe
aan `created_files`. `deployment`, `project_data`, `cluster` en `namespace` zijn daar al in
scope. De kustomization wordt uit een disk-scan gebouwd, dus de bestanden komen er vanzelf in.

De service zelf groepeert zijn `ResolvedRule`s op `local_component` en geeft per groep een
spec terug met `pod_selector = {"app": f"{deployment}-{local_component}"}`. Binnen een groep
worden regels met dezelfde peer samengevoegd tot een entry met een `ports`-lijst, zodat twee
regels naar hetzelfde doel geen twee bijna identieke blokken opleveren.

**4d. Opruimen, symmetrisch aan de component-prune.** In `_process_deployment_manifests` staat
al `self._prune_obsolete_component_manifests(deployment, target_path, created_files)` (rond
regel 3244). Zet daar `_prune_obsolete_service_manifests(...)` naast: verwijder elk bestand
`f"{deployment_name}-{service_type.value}-*"` dat deze run niet gegenereerd is, voor elke
`ServiceType`. Dat dekt service uitgezet, laatste regel weggehaald, component-scope
verdwenen, en doelproject verdwenen. `_select_obsolete_component_manifests` raakt deze
bestanden niet, dus de twee regels bijten elkaar niet.

Verify:
- `uv run pytest tests/test_tenant_baseline_netpol.py -q --noconftest` blijft groen en de
  baseline-render is byte-identiek (je raakt dat template niet aan, dat is het punt).
- Nieuwe tests op het nieuwe template: alleen inbound, alleen outbound, beide; en twee regels
  naar dezelfde peer die tot een entry met twee poorten samensmelten. Assert dat de peer
  altijd beide podlabels draagt (`app` en `project`), dat namespaceSelector en podSelector in
  dezelfde peer-entry zitten, dat `ports` bij de bijbehorende peer-entry staat en niet
  policy-breed, dat `policyTypes` alleen gevulde
  richtingen bevat, en hergebruik `_has_empty_allow_all_rule` uit
  `test_tenant_baseline_netpol.py` om te bewijzen dat er nooit een lege allow-all peer ontstaat.
- Een test op de groepering: twee regels met een verschillend eigen component leveren twee
  bestanden met een verschillende `podSelector`.
- Een test op de opruimregel: gegeven een map met
  `myapp-cross-domain-access-web-network-policy.yaml` en een `created_files` waar die niet in
  zit, wordt het bestand verwijderd en blijven baseline en componentbestanden staan.
- `uv run pytest tests/test_golden_manifests.py -q` onveranderd.
- Sandbox, end to end: service aanzetten met een regel, kijken dat het bestand in
  `zad-deployments` verschijnt; service weer uitzetten, kijken dat het bestand weg is en dat
  ArgoCD de NetworkPolicy uit de cluster prunet. Dat pad is de kern van deze keuze, dus
  verifieer het echt, ook al handhaaft kindnet zelf niets.

Na deze stap werkt de feature volledig via het YAML-bestand. De UI is aankleding.

### Stap 5. UI op projectniveau

1. `catalog/cross_domain_access/editables.py`: een sequence-editable voor `inbound` en een
   voor `outbound`, met per rij vier kinderen: `name`, de peer-select, de
   eigen-component-select (gevuld uit de eigen `components`) en de poort-select. Alles met
   `virtualize=("services", "_services-config")`. Volg
   `catalog/persistent_storage/editables.py` als vorm; paden bouwen met `config_path(...)`,
   nooit hardcoden. De richting bepaalt welk van `from`/`to` de peer-select en welk de
   eigen-component-select voedt, en waar de poort landt (altijd `to`).

   **De peer-select is een transient veld dat bij het opslaan wordt gesplitst.** De opslagvorm
   is het object met drie velden (2.3), de formuliervorm is een enkele keuze
   `regelrecht / prod / api`. Doe dit niet met drie afhankelijke selects: het framework kan
   geen per-rij afhankelijke opties, en dat namaken met eigen JavaScript is een
   maintenance-last die dit ene formulier niet waard is.

   Gebruik hiervoor een transient (`_peer`) plus een `FormState.PRE_SAVE`-hook die per regel
   de drie velden zet, zoals `SubdomainRequestHook` het `_request-subdomain`-transient
   verwerkt; `StripTransientsHook` ruimt het transient daarna op. Niet met een converter op het
   hele `from`/`to`-object, want bij een outbound-regel draagt `to` ook de `port` en die zou
   een objectvervangende converter wissen. De hook krijgt de volledige projectdict en kan
   veilig alleen de drie sleutels zetten.

   Let op bij het schrijven: laat velden weg die de gebruiker niet koos (geen
   `deployment: None` opslaan), anders wist een deployment-patch stilzwijgend een geerfde
   waarde.
2. `catalog/cross_domain_access/visualizers.py`: `WidgetType.SEQUENCE` met select-kinderen,
   Nederlandse labels en helptekst. Zet de twee valkuilen uit 2.5 in de helptekst: het is de
   poort van de ontvangende kant, en bij een authorization-wall is dat 4180.
3. `opi/forms/visualizers/providers.py`: drie providers die hun opties uit `yaml_data` lezen
   (exact het patroon van `BackupDeploymentOptionsProvider` en `AttachmentOptionsProvider`),
   plus registratie in `PROVIDER_REGISTRY`:
   - `CrossDomainTargetOptionsProvider` leest `_cross_domain_targets`
   - `CrossDomainLocalComponentOptionsProvider` leest de eigen `components`
   - `CrossDomainPortOptionsProvider` leest `_cross_domain_ports`
   Bij een lege lijst een uitleg-optie tonen in plaats van een lege select.
4. `opi/web/router_detail_edit.py`, in `modal_wizard_init`: vul `_cross_domain_targets` en
   `_cross_domain_ports` in `state.template_data` wanneer de flow van deze service is.
   Targets bouw je uit `get_project_store().get_all()`, gefilterd met
   `is_user_authorized_for_project(p.name, user_email)`, zonder het eigen project, en per
   project een optie voor het project, per deployment op onze cluster een optie, en per
   component van die deployment een optie. Poorten haal je uit `components[].ports.inbound`
   van hetzelfde projectbestand, plus 4180 als dat component een authorization-wall heeft.
   Let op: `template_data`-sleutels die geen enkele stap produceert worden bij opslaan
   automatisch weggegooid (`template_only_keys` in `router_detail_edit.py` rond regel 1206),
   dus deze context lekt niet het projectbestand in. Verifieer dat wel met een test, want
   `apply_form_data_to_project` doet een kale `{**existing, **submitted}`.
5. `config_form_section(ConfigLayer.PROJECT)` op de service + `config_section_id` +
   `modal_flow_id = "modal-edit-cross-domain-config"`, `post_save_action="process_project"`.
   `visible` = alleen als de service in `services` gekozen is (zie
   `KeycloakService._config_selected`).
6. `opi/forms/visualizers/flows.py`: `FormFlow` toevoegen en registreren in `FLOW_REGISTRY`.
   De knop op de detailpagina komt er vanzelf bij via `SERVICE_CONFIG_MODAL_FLOWS`.

Verify: `uv run pytest tests/test_flow_registry_snapshot.py -q` (snapshot bewust bijwerken),
plus handmatig in de sandbox: service aanzetten, modal openen, regel toevoegen, opslaan, en in
`zad-deployments` controleren dat de NetworkPolicy klopt.

### Stap 6. UI per deployment: de override zichtbaar maken

Dit is de stap waar de patch-semantiek uit 2.2 staat of valt. Een lijst met alleen de
deployment-regels is onbruikbaar: je ziet dan niet wat je erft en overschrijft per ongeluk
niets of alles.

Bouw een modal-flow per deployment, naar het model van `build_domain_cert_section`
(`opi/forms/visualizers/wizard_sections.py` rond regel 670): neem de projectvisualizers en
vervang het segment `deployments[*]` door `deployments[N]` met `replace_segment_visualizer`.
Flow-id `modal-edit-cross-domain-{index}`, gebouwd door een `build_*`-functie en aangeroepen
vanuit `get_flow` net als de andere index-flows.

Het scherm heeft twee blokken.

**Blok 1: de rootregels, met precies een bewerkbaar veld.** Toon elke rootregel volledig
uitgeschreven, met alle door de root bepaalde velden als **read-only**: `name`, de peer
`project` en `component`, het eigen `component` en de `port`. Het enige invulbare veld is de
**deployment van de peer**. Leeglaten betekent: erven wat de root zegt (of, als de root het
open liet, "nog niet ingesteld", zie 2.3). Daarnaast per regel een schakelaar **uitzetten**
(`disabled: true`).

Dat is geen cosmetiek maar de kern van het model: de deployment bepaalt wat er geldt, maar de
enige zinvolle aanpassing aan een gedeelde regel is welke omgeving van de peer je aanspreekt.
Alles anders bewerkbaar maken nodigt uit tot regels die per deployment uit elkaar lopen zonder
dat iemand dat nog overziet.

Bouwstenen die hiervoor al bestaan: `readonly=True` op een `EditableVisualizer`
(`opi/forms/visualizers/visualizer.py:29`, gebruikt in `fields/config_display.py` en
`fields/approval.py`) en `transient=True` op een `Editable` (`editables/editable.py:195`) voor
velden die je toont maar nooit opslaat.

**De read-only velden mogen niet meegeschreven worden.** Maak ze transient (of puur display),
zodat het opslaan uitsluitend de peer-deployment en `disabled` wegschrijft. Zou de UI de hele
regel terugschrijven, dan staat er een volledige kopie in de deployment-laag en doet een
latere wijziging op rootniveau niets meer voor die deployment. Dit is de belangrijkste val in
deze stap; test hem expliciet met een assert op de opgeslagen YAML, niet alleen op het
zichtbare resultaat.

**Blok 2: eigen regels van deze deployment.** Een gewone sequence met het volledige formulier
uit Stap 5 (`from` project/deployment/component, `to` component/port), plus toevoegen en
verwijderen. Deze regels bestaan alleen hier en erven niets.

`name` is bij toevoegen voorgevuld met iets afgeleids (bijvoorbeeld `naar-<project>`) en wordt
gevalideerd op uniciteit binnen de richting over beide blokken heen: een eigen regel mag niet
dezelfde `name` krijgen als een rootregel, want dan is het per ongeluk een patch.

Verify:
- Sandbox, project met `dev` en `prod`, een rootregel `naar-regelrecht-api`, en op `dev`
  alleen de peer-deployment op `dev` gezet. Controleer dat de `dev`-policy naar hun
  dev-namespace wijst en de `prod`-policy naar hun prod, en dat het eigen component, hun
  `component` en de `port` in beide policies de rootwaarden hebben.
- Wijzig daarna de `port` op rootniveau en controleer dat **beide** deployments meebewegen.
  Dat is de test die aantoont dat de UI geen volledige kopie heeft weggeschreven.
- Zet een rootregel uit op `dev` en controleer dat hij op `prod` blijft staan.
- Een rootregel zonder peer-deployment: `dev` vult hem in en krijgt de policy, `prod` laat
  hem leeg, krijgt geen regel, en ziet in de UI dat hij nog niet is ingesteld.

### Stap 7. API

Vandaag exposeert **geen enkele service in `catalog/` een eigen endpoint**. De bestaande
routers (`opi/api/restore_router.py`, `logs_router.py`, `image_router.py`, `admin_router.py`,
`federation_router.py`) horen bij subsystemen, niet bij services, en het enige
service-endpoint is `POST /api/projects/{name}/services` (`opi/api/router.py:1660`), dat
alleen een kale servicenaam accepteert zonder config.

Bouw daarom geen router die specifiek van deze service is. De juiste vorm is dezelfde als de
rest van RC-5: **registry-gedreven, generiek, werkt meteen voor elke service met een
`config_model`**:

```
GET  /api/projects/{project}/services/{service}/config
PUT  /api/projects/{project}/services/{service}/config
GET  /api/projects/{project}/deployments/{deployment}/services/{service}/config
PUT  /api/projects/{project}/deployments/{deployment}/services/{service}/config
```

Implementatie:

1. Service opzoeken via `get_service(ServiceType(service))`; onbekend of zonder
   `config_model` is een 404 respectievelijk 400.
2. Body valideren met `provider.validate_config(body, from_version=...)`. Dat is het
   bestaande chokepoint; niet zelf valideren.
3. Schrijven via `get_project_store().mutate(name, change_fn, ...)` met een
   change-functie die de service-entry opzoekt met `service_entry_name` en de config zet.
   Niet lezen-in-de-handler-en-dan-schrijven: `mutate` herleest en herapplyt bij
   gelijktijdige schrijvers, en dat is precies waarvoor de store bestaat. De store valideert
   de eindtoestand en commit; dat is het enige toegestane opslagpad (de CI-guard bewaakt dat).
4. Auth met `@validate_api_token` (project-API-key), zoals de bestaande endpoints.
5. Antwoord async volgens het v2-patroon (202 met task-id, `Location: /api/tasks/{id}`), want
   de wijziging trekt een `process_project` na zich aan. `upsert_deployment_v2`
   (`opi/api/v2/router.py:466`) is het model om te kopieren.

PUT vervangt de hele config van die laag. Een client die een regel wil toevoegen doet GET,
lijst aanvullen, PUT. Geen PATCH-semantiek op regelniveau; de patch die dit ontwerp kent is
die tussen de lagen (2.2), niet die tussen client en server.

**Als een service ooit meer nodig heeft dan config-CRUD**, voeg dan een hook
`api_router(self) -> APIRouter | None` toe aan `Service` (fastapi lazy importeren in de
methode, anders breek je de import-lichtheid van de catalog) en verzamel die routers bij het
opbouwen van de API-router. Voor cross-domain-access is dat niet nodig: de config is de hele
service.

Verify: een test per endpoint (lezen, geldige PUT, ongeldige PUT geeft 400 met de
veldnamen uit `config_api_fields`, onbekende service geeft 404, verkeerde API-key geeft 401),
en een test dat een geweigerde PUT **geen** commit produceert. Dat laatste is de regressie
die eerder in het self-service-pad zat.

### Stap 8. Documentatie

- `features/cross-domain-access.md`: wat het is, hoe je het gebruikt (beide kanten), de
  YAML-vorm inclusief de laag-patch met `name`, de API-endpoints, dat de ontvanger beslist,
  de poortsemantiek uit 2.4, waarom er geen wildcards zijn (2.6), de beperkingen uit 2.7, en
  dat kindnet in de sandbox niets handhaaft.
- Een verwijzing vanuit `features/restrictive-network-policies.md`, dat de baseline beschrijft
  waar dit naast komt te staan.
- `instructions/services.md`: een regel bij het manifest-hoofdstuk over de nieuwe
  deployment-manifest-hook, en een regel over de generieke config-API uit Stap 7. Dat is het
  contract dat de volgende bouwer leest.

### Stap 9. Afsluiten

```bash
cd operations-manager/python
uv run ruff check . --fix && uv run ruff format . && uv run pyright
uv run pytest tests/test_service_providers.py tests/test_service_config_schema.py \
              tests/test_golden_manifests.py tests/test_flow_registry_snapshot.py -q
uv run pytest tests/test_tenant_baseline_netpol.py -q --noconftest
```

Plus de nieuwe eigen tests. Draai niet de hele suite; die heeft pre-existing collectiefouten.

---

## 5. Bestanden

| Bestand | Wat |
|---|---|
| `opi/services/services_enums.py` | enum-lid |
| `opi/services/services.py` | `ServiceDefinition` |
| `opi/services/registry.py` | registratie + `deployment_manifest_services()` |
| `opi/services/catalog/cross_domain_access/__init__.py` | service-klasse, hooks, groepering per eigen scope |
| `.../config_model.py` | pydantic-modellen (patch + volledig), peer-parsing, poortvalidatie |
| `.../merge.py` | laag-patch op `name` (pure functie) |
| `.../resolve.py` | regels oplossen naar selectors (pure functie) |
| `.../editables.py`, `.../visualizers.py` | formulier |
| `.../cross-domain-access.v1.0.json` | gegenereerd fragment |
| `opi/services/catalog/base.py` | `contribute_deployment_manifests` + context/spec |
| `opi/schemas/project_v2.json` | `deployment-service-config` verruimen |
| `manifests/service-network-policy.yaml.jinja` | nieuw, generiek |
| `opi/manager/project_manager.py` | emissie (4c) + `_prune_obsolete_service_manifests` (4d) |
| `opi/forms/visualizers/providers.py` | drie options providers + registry |
| `opi/forms/visualizers/flows.py` | modal-flows |
| `opi/forms/visualizers/wizard_sections.py` | per-deployment sectie-builder |
| `opi/web/router_detail_edit.py` | `_cross_domain_targets` en `_cross_domain_ports` |
| `opi/api/v2/router.py` (of een nieuw `service_config_router.py`) | generieke config-endpoints uit Stap 7 |
| `features/cross-domain-access.md` | documentatie |

---

## 6. Valkuilen

- **Identiteit altijd via `service_entry_name(entry)`**, nooit via de dict-keys. Een record met
  config heeft keys `name`/`config`; key-lezen laat de service verdwijnen. Config lezen met
  `service_entry_config(entry)`.
- **`deployment-service-config` is inmiddels open** (1 augustus), dus die val bestaat niet meer.
  Wat er wel voor in de plaats komt: omdat het globale schema die laag niet meer bewaakt, is jouw
  configmodel de enige controle. Een laag die je model niet dekt wordt stil doorgelaten.
- **Zaai geen defaults.** Een editable met een default op een `{K}`-pad materialiseert de
  service in de lijst; dan staat de service ineens aan bij projecten die hem nooit kozen.
- **Nooit een lege peer of lege regel renderen.** `- from: [{}]` betekent allow-all in
  Kubernetes. Geen peers betekent: geen bestand.
- **namespaceSelector en podSelector horen in dezelfde lijst-entry** voor AND. Los van elkaar
  is het OR en open je de hele namespace.
- **`component: application` is een letterlijke waarde**, geen componentnaam. Component-selectie
  gaat via `app: <deployment>-<component>`.
- **Poorten zijn die van de ontvangende kant en het zijn containerpoorten.** Bij een
  authorization-wall is dat 4180, niet de app-poort.
- **Genereren mag nooit falen op een kapotte verwijzing.** Loggen en overslaan.
- **Een patch mag niets wissen dat je niet noemt.** Alleen aanwezige sleutels overschrijven,
  geen `None`-velden uit het gedeserialiseerde model doorschuiven. Anders wist een
  deployment-regel die alleen `to.deployment` zet stilletjes het geerfde `component` of de
  geerfde `port`. Dit geldt op drie plekken: de converter (`write`), de merge-functie en de
  UI die een override wegschrijft.
- **Schrijf alleen via `ProjectStore.mutate`** in de API. Lezen-in-de-handler en dan
  schrijven heropent het verloren-update-gat waar de store voor gebouwd is.
- **De sandbox handhaaft geen NetworkPolicies** (kindnet). Verifieer daar de YAML en het
  opruimen; daadwerkelijk blokkeren stel je alleen op ODCN vast.
- Houd `catalog/`-modules import-licht: importeer forms en managers binnen de methode, niet op
  module-niveau, anders krijg je importcycles (forms importeert de catalog).

---

## 7. Buiten scope, expliciet

- **Goedkeuring door een platformbeheerder.** Het `ApprovalSpec`-mechanisme
  (`catalog/approval.py`) zou een inbound-regel kunnen laten wachten op akkoord; de
  approver-UI heeft daar geen wijziging voor nodig. Bewust niet nu, maar het overwegen waard
  voordat dit in productie gaat, want dit is de enige service die tenant-isolatie versoepelt.
- **Verwijzen naar projecten waar je geen rechten op hebt.** Nu filtert de dropdown op toegang.
  Later moet dat misschien anders (bijvoorbeeld: elk project mag genoemd worden, maar de
  ontvanger accordeert). Dat is dezelfde discussie als het punt hierboven.
- **Wildcards in peer-verwijzingen.** Zie 2.6, met de reden.
- **Automatisch her-verwerken van projecten die naar een gewijzigd project verwijzen.** Zie de
  staleness-beperking in 2.7.
- **UDP en protocolkeuze.** Alle regels zijn TCP, net als de rest van de gegenereerde policies.
- **`{deployment}`-placeholder als waarde van `to.deployment`.** Met
  `to: {project: regelrecht, deployment: "{deployment}", component: api}` op rootniveau zou
  mijn `dev` vanzelf naar hun `dev` wijzen en had je in het parity-geval helemaal geen
  override nodig. Aantrekkelijk, maar het is een tweede mechanisme naast de laag-patch uit
  2.2, en die patch heb je sowieso nodig (namen lopen niet altijd gelijk, en soms wil je een
  heel ander doel). Eerst de patch bouwen; als blijkt dat vrijwel elke override alleen de
  deployment-naam spiegelt, is dit de volgende stap. Houd hem dan strikt tot dit ene token in
  dit ene veld, geen template-taal.
- **`POST /api/projects/{name}/services` moderniseren.** Dat endpoint accepteert nog het oude
  platte stringformaat (zie `features/futures/update-add-service-api-for-v2-schema.md`). De
  nieuwe config-endpoints uit Stap 7 staan daarnaast: aanzetten doe je met het bestaande
  endpoint, configureren met het nieuwe.
- **Cross-cluster.** Onmogelijk met NetworkPolicies en met het gedistribueerde OPI-model.
