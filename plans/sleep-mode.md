# Implementatieplan: sleep-mode (slaapstand met wekken op verzoek)

**Eindproduct van deze sessie:** dit document als `plans/sleep-mode.md` in de repo.
**Eindproduct van de implementatie:** `features/sleep-mode.md` volgens de projectconventie.

Doelgroep: de sessie die dit bouwt. Bevat de context, de ontwerpbeslissingen met hun redenen,
de concrete wijzigingen per bestand, en de volgorde met verificatie per stap.

---

## 1. Context

Veel PR-/previewdeployments in ZAD draaien permanent zonder dat er iemand naar kijkt. Ze
reserveren geheugen en CPU op ODCN zonder waarde te leveren. Doel: zulke deployments na een
harde deadline in slaapstand zetten (`replicas: 0`), en waar dat kan ze weer laten opstarten
zodra iemand de URL bezoekt, met een "applicatie wordt gestart"-pagina zolang dat duurt.

Landschapsonderzoek (2026) wees uit dat niets bestaands hier past. KEDA HTTP add-on is beta,
kost zeven pods en zijn cross-namespace Route-koppeling is onbevestigd. Sablier is de beste
OSS-match maar heeft geen HAProxy/OpenShift Route-integratie. Knative vereist dat alle
workloads herschreven worden naar `Service.serving.knative.dev`. OpenShifts eigen `oc idle` kan
geen laadpagina tonen en is broos op OVN-Kubernetes. Alleen Kedify (commercieel) doet het
compleet.

### Waarom geen sidecar

Eerste vraag die iedereen stelt, dus neem dit ook op in het feature-document. `replicas: 0`
betekent nul pods. Een sidecar is een container ín een pod, dus die is óók weg en de Service
heeft een lege EndpointSlice. Wie het verzoek wil opvangen moet buiten de geschaalde workload
leven. Knative en Osiris hebben ook een sidecar, maar puur voor metrics: het wekken doet daar
altijd een aparte, altijd draaiende component.

### Waarom deze naam

Leg dit vast, want vroeg of laat wil iemand dit alsnog "idle" noemen.

- **Niet `idle`.** OpenShift heeft `oc idle` en `oc unidle` al, en daar wordt het wekken
  getriggerd door netwerkverkeer via de HAProxy-router. Onze trigger is een deadline, niet
  verkeer. Een service die `idle` heet zou frontaal botsen met begrippen die het cluster zelf
  al gebruikt.
- **Niet `hibernation`, `suspend` of `pause`.** Die beloven bewaarde staat. Bij ons verdwijnen
  de pods en start het koud op.
- **Niet `standby`.** Stand-by betekent: nog gevoed, direct klaar. Wij hebben dertig tot zestig
  seconden koude start.
- **Wel `sleep-mode`.** Dat is de term die Okteto, vCluster/Loft, Heroku en Railway gebruiken
  voor precies dit gedrag, inclusief het feit dat er niets bewaard blijft. Het leest als een
  capaciteit, net als `persistent-storage` en `authorization-wall`.

**Nederlandse copy, belangrijk:** `slaapstand` is sleep, `sluimerstand` is hibernate. Dat is
contra-intuïtief omdat "sluimeren" lichter klinkt, maar Windows gebruikt het voor de diepere
stand. Schrijf dus altijd **slaapstand** en nooit sluimerstand, en zet er expliciet bij dat de
applicatie koud opstart, zodat niemand verwacht dat sessies of geheugen blijven bestaan.

---

## 2. Wat een service moet kunnen

Dit is één service, maar hij vraagt zeven dingen van het servicemechanisme en daarvan
ondersteunt de code er vandaag één. Formuleer ze als eisen aan de servicestructuur, want de
services-herschrijving loopt en deze service is de eerste die ze allemaal nodig heeft.

| # | Een service moet kunnen | Bestaat vandaag |
|---|---|---|
| 1 | Config bijdragen in het projectbrede `services`-blok | Deels: `$defs.service-entry` accepteert een single-key dict met vrije waarde |
| 2 | Een clusterbrede default hebben die het project mag overschrijven | Nee |
| 3 | Runtime-toestand per deployment lezen en schrijven in het projectbestand | Nee, wel een model: `disabled` / `disabled-reason` |
| 4 | Manifesten bijdragen aan een deployment, als waarden voor bestaande templates | Nee, wel een model: de sidecar-lus in `project_manager.py:5275` en `5335-5357` |
| 5 | Secrets bijdragen | Ja: `secret_class` op `ServiceDefinition`, `BaseSecret` met `SERVICE_TYPE` |
| 6 | Een API-router en een achtergrondtaak bijdragen | Nee, routers liggen plat in `opi/api/`, schedulers in `opi/core/` |
| 7 | **Actieknoppen bijdragen op deployment-niveau in de UI** | Nee, en dat is nu al een gat |

Punt 7 heeft nu al een tweede afnemer: `section-deployment-actions.html.j2:4-5` leidt zelf
`has_database` af uit de serviceslijst om de knop "Databaseconsole" te tonen. Die conditie hoort
bij de databaseservice, niet in de template.

**Aanpak: eis vastleggen, minimum bouwen, niet generaliseren op de rug van één feature.** Alles
van deze service komt in één package, en `server.py` bindt het met twee expliciete regels. Voor
punt 7 komt er wél een klein generiek mechanisme, want anders is de knop niet te bouwen zonder
de template opnieuw te vervuilen. Zodra de services-herschrijving landt kan de registry dezelfde
objecten binden en hoeft er niets te verhuizen.

Geen generiek delegatie-endpoint (`POST /api/services/{service}/{action}`): dat levert
ongetypeerde payloads op, geen OpenAPI-schema per actie en één auth-regel voor alle acties. Een
`APIRouter` is al de compositie-eenheid van FastAPI.

---

## 3. Twee helften: de slaapstand is de kern, de wekker is optioneel

De slaapstand werkt voor **elke** deployment, ook zonder webendpoint. De wekker is een
aanvulling die alleen bestaat waar een component `publish-on-web` heeft. Dat geeft deze matrix:

| Situatie | Wekkerpod | Wat de bezoeker ziet | Wie kan wekken |
|---|---|---|---|
| Geen web-gepubliceerd component | nee | n.v.t. | knop in de UI, API, image-update |
| Wel web, `waker: false` | nee | 503 van de router | knop in de UI, API, image-update |
| `wake-mode: manual` | ja | uitlegpagina zonder knop | knop in de UI, API, image-update |
| `wake-mode: confirm` | ja | pagina met knop | bezoeker na een klik, plus bovenstaande |
| `wake-mode: auto` | ja | laadpagina, wekt direct | bezoeker bij het eerste verzoek, plus bovenstaande |

`manual` is de modus waarin je een bezoeker netjes wil informeren ("deze applicatie staat in
slaapstand en moet door een beheerder worden gestart") zonder dat hij hem zelf kan wekken.
`confirm` is prettig tijdens ontwikkeling. In `manual` en `confirm` kan geen crawler,
uptime-check of linkpreview per ongeluk een applicatie starten.

Omdat een deployment zonder wekker alleen via een knop of de API te wekken is, is de actieknop
uit eis 7 geen extraatje maar een voorwaarde.

---

## 4. Architectuur van de wekker

### Twee Deployments achter één Service

`manifests/service.yaml.jinja` selecteert op `app: <naam>` + `component: application`. Een
tweede Deployment met diezelfde podlabels landt daardoor automatisch achter dezelfde Service en
dezelfde Ingress. Er verandert dus niets aan de Ingress, de Route, cert-manager of de ArgoCD
Application.

De selector van het app-Deployment matcht de wekkerpods ook. Dat is onschadelijk: een
ReplicaSet adopteert nooit pods die al een `controllerRef` naar een andere controller hebben.
Het wekker-Deployment krijgt `zad-role: waker` in zijn eigen selector om te onderscheiden. De
selector van bestaande app-Deployments blijft ongewijzigd, dus geen fleet-brede herstart.

### Drie toestanden

| `sleep.state` | app-Deployment | wekker-Deployment |
|---|---|---|
| `awake` | `replicas: 1` | niet gegenereerd |
| `sleeping` | `replicas: 0` | `replicas: 1` (alleen als er een wekker is) |
| `waking` | `replicas: 1` | `replicas: 1` (alleen als er een wekker is) |

De tussentoestand `waking` voorkomt een gat. Zou één commit tegelijk de app op 1 zetten en de
wekker weghalen, dan is er tijdens het opstarten niemand die de laadpagina serveert.

### Waarom de tweede commit niet in het wekpad zit

1. **Een pod die niet `Ready` is, staat niet in de EndpointSlice.** Tijdens het opstarten is de
   wekker daardoor vanzelf het enige endpoint. Gratis.
2. **De wekker haalt zichzelf uit het verkeer.** Zijn readinessprobe wijst naar zijn eigen
   `/__zad/ready`, die 503 gaat geven zodra zijn poller ziet dat de app terug is. Dat is geen
   afwijking van git: git zegt "wekker-Deployment, replicas 1" en dat draait ook. Readiness is
   runtime-toestand die Kubernetes sowieso bezit, net als pod-fases.

De overgang `waking` naar `awake` is dus opruimwerk, ná het moment dat de bezoeker de app al
gebruikt.

### Uitgangspunt: het projectbestand is de bron van waarheid

Geen `ignoreDifferences`, geen `kubectl scale` buiten ArgoCD om, `selfHeal` blijft overal aan.
Elke overgang schrijft eerst naar het projectbestand en commit dat; pas daarna worden manifesten
hergenereerd. Er is geen moment waarop de gegenereerde manifesten iets beweren wat het
projectbestand niet zegt. Dit kost wektijd en dat is bewust gekozen. Beperk het met:

- `trigger_reprocessing(..., deployment_name=<dep>, argocd_resources_changed=False)`, zodat
  alleen die deployment hergenereerd wordt en Application/AppProject worden overgeslagen. Dit is
  de knop die de resource-tuner ook gebruikt.
- Direct `argo.refresh_application()` plus `argo.sync_application()` na de push, zodat ArgoCD
  niet op polling wacht. De Application heeft al `argocd.argoproj.io/manifest-generate-paths: .`.

Loopt het opstarten vast (kapot image), dan zet de sweeper `waking` na
`SLEEP_MODE_WAKING_TIMEOUT_MINUTES` (standaard 10) terug naar `awake` en ruimt de wekker op. De
bezoeker ziet dan de echte fout en de bestaande sanitize-route pakt een structureel kapotte
deployment op zoals nu.

### Commit-storm voorkomen

Belangrijkste faalmodus nu alles via git loopt.

- De wekker doet single-flight: één wek-call per pod-leven, daarna alleen nog pollen.
- Het wek-endpoint is idempotent. Al `waking` of `awake` betekent geen commit.
- De sweeper paced (`SLEEP_MODE_PACE_SECONDS`), zodat twintig verlopen previews niet twintig
  commits tegelijk afvuren.
- Gelijktijdige commits op hetzelfde projectbestand vangt de bestaande conflict-retry in
  `save_and_commit_project` al af.

---

## 5. Datamodel

### Clusterbrede default

In `opi/core/cluster_config.py`, naast bestaande sleutels als `min_memory_limit_mi` en
`supports_vpa`:

```python
"sleep_mode": {
    "enabled": False,          # standaard UIT, zie waarschuwing
    "match": [],
    "sleep_after_deploy": "48h",
    "sleep_after_wake": "1h",
    "waker": True,
    "wake_mode": "auto",
}
```

**Ship dit uitgeschakeld.** Een clusterbrede regel die deployments in slaapstand zet raakt
iedereen tegelijk. Zet hem eerst alleen aan op `sandboxed-local`, en pas op `odcn-production`
als de werking bevestigd is. Een project moet altijd kunnen uitstappen met `enabled: false`.

Precedentie: clusterdefault, daarboven de projectconfig, per sleutel.

### Projectconfig

```yaml
services:
  - sleep-mode:
      enabled: true
      match: ["PR-*"]           # glob op deploymentnaam
      sleep-after-deploy: 48h   # bij aanmaak en bij elke image-update
      sleep-after-wake: 1h      # na een wek-call
      waker: true               # wekkerpod genereren, zie "welk component"
      waker-component: frontend # optioneel, verplicht bij meerdere web-componenten
      wake-mode: auto           # auto | confirm | manual
      title: "{deployment}"     # optioneel, placeholders {project} {deployment} {component}
      description: ""           # optioneel
```

Bewust alleen projectbreed, met `match` als enige selectiemechanisme. Overschrijven per
deployment of per pod bouwen we niet: `match` dekt het onderscheid tussen `main` en `PR-*` af.

### Welk component krijgt de wekker

Er komt **één wekker per deployment**, niet één per endpoint. De wekker zit achter de Service
van een component, en een component kan zelf al meerdere hostnames en paden hebben (zie de
per-pad-lus in `project_manager.py:5406-5420` en `get_component_ingress_map` in
`opi/utils/naming.py:1785`). Die dekt hij dus allemaal automatisch. De onduidelijkheid speelt
alleen tússen componenten.

Regels, in deze volgorde:

1. Staat `waker-component` gezet en heeft dat component `publish-on-web`, dan die.
2. Anders, heeft precies één component in de deployment `publish-on-web`, dan die.
3. Anders (nul, of twee of meer web-componenten zonder `waker-component`): **geen wekker**, plus
   een `logger.warning` die het probleem benoemt en de kandidaten opsomt.

Geval 3 is bewust geen willekeurige keuze en ook geen wekker op alles. Een willekeurig gekozen
URL is verrassend gedrag, en een wekker per endpoint kost een pod per hostname terwijl er maar
één nodig is om de hele deployment te wekken. Niets doen is hier eerlijker: de deployment gaat
gewoon in slaapstand en is te wekken via de knop in de UI of de API, precies zoals een
deployment zonder endpoint.

Verwijst `waker-component` naar een component dat niet bestaat of geen `publish-on-web` heeft,
faal dan luid bij het laden van de config. Dat is een configuratiefout, geen randgeval.

### Runtime-toestand per deployment

```yaml
deployments:
  - name: PR-123
    sleep:
      state: sleeping                        # awake | sleeping | waking
      expires-at: "2026-07-28T14:03:00+02:00"
      wake-token: |                          # AGE-versleuteld, zoals config.api-key
        -----BEGIN AGE ENCRYPTED FILE-----
```

### Schemawijziging

Aan `$defs.deployment.properties` in `opi/schemas/project_v2.json`:

```json
"sleep": {
  "type": "object",
  "additionalProperties": false,
  "properties": {
    "state": { "enum": ["awake", "sleeping", "waking"] },
    "expires-at": { "type": "string", "format": "date-time" },
    "wake-token": { "$ref": "#/$defs/age-encrypted" }
  }
}
```

De serviceconfig zelf hoeft niet: `$defs.service-entry` accepteert al een single-key dict met
vrije waarde. Valideer die in code en faal luid met een begrijpelijke melding, want een stille
configfout blokkeert hier deployments.

**Let op bij het valideren van bestaande projecten:** `process_project_from_git` migreert
in-memory vóór het valideren. Test dus tegen `migrate_to_latest()`-uitvoer, niet tegen het ruwe
bestand. Een eerdere schemagap (dp-bn7) blokkeerde hierdoor stilzwijgend alle deploys.

---

## 6. Generieke template-uitbreidingen

Vier toevoegingen aan `manifests/deployment.yaml.jinja` plus één nieuwe generieke template.
Alle met een default, zodat bestaande rendering byte-identiek blijft. Anders herstart de hele
vloot bij de eerstvolgende reprocess en loopt de deployments-repo vol ruis.

| Variabele | Waarom nodig | Default |
|---|---|---|
| `object_name` | `metadata.name` en het `app`-label komen nu allebei uit `{{ name }}`. De wekker heet `<naam>-waker` maar moet het `app`-label van de app dragen, anders landt hij niet achter dezelfde Service. | `name` |
| `extra_selector_labels` | `selector.matchLabels` staat hardcoded. De wekker zet er `zad-role: waker` bij, in zowel de selector als de podlabels. | `{}` |
| `probe_readiness_failure_threshold` | Staat hardcoded op 3; met `periodSeconds: 5` duurt uitvallen dan 15s in plaats van 5s. | `3` |
| `env_from_configmaps` | Spiegelbeeld van het bestaande `env_from_secrets`, voor de presentatiewaarden van de wekker. | `[]` |

```jinja
{# regel 4 #}
  name: "{{ object_name | default(name) }}"

{# selector, regel ~19, en dezelfde lus bij de podlabels rond regel ~36 #}
  selector:
    matchLabels:
      app: "{{ name }}"
      component: application
{% for k, v in (extra_selector_labels or {}).items() %}
      {{ k }}: {{ v | tojson }}
{% endfor %}

{# readinessProbe #}
            failureThreshold: {{ probe_readiness_failure_threshold | default(3) }}

{# envFrom: de conditie wordt {% if env_from_secrets or env_from_configmaps %},
   met de envFrom-sleutel één keer erboven #}
          {% for cm_name in env_from_configmaps | default([]) %}
            - configMapRef:
                name: {{ cm_name }}
          {% endfor %}
```

**Nieuw `manifests/configmap.yaml.jinja`**, generiek gehouden zodat er geen wekker-specifieke
variant ontstaat:

```jinja
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ name | tojson }}
  namespace: {{ namespace | tojson }}
  labels:
    app: {{ app_label | default(name) | tojson }}
    project: {{ project.name | tojson }}
data:
{% for k, v in (data or {}).items() %}
  {{ k }}: {{ v | tojson }}
{% endfor %}
```

Voor het emitteren van een standalone manifest is er al een patroon: de sidecar-lus in
`project_manager.py:5335-5357` rendert met `section="configmap"` en voegt het resultaat toe aan
`created_files`, zodat de obsolete-manifest prune het opruimt. Volg diezelfde afhandeling.

---

## 7. De wekkerpod

Geen eigen Service, geen eigen Ingress: hij deelt die van de app. Dat is de kern. Per slapend,
web-gepubliceerd component ontstaan drie objecten:

| Object | Template | Naam |
|---|---|---|
| Deployment | `deployment.yaml.jinja` | `<unique_name>-waker` |
| ConfigMap | `configmap.yaml.jinja` | `<unique_name>-waker-config` |
| Secret | bestaande secret-pijplijn | `<unique_name>-waker-token` |

`unique_name` uit `generate_unique_name(deployment_name, component_reference)` in
`opi/utils/naming.py`. Bestandsnamen via `generate_manifest_name(component_name, ...)`.

### Values-dict voor `deployment.yaml.jinja`

Gebouwd door de service. Alleen de velden die afwijken van wat `project_manager` al doorgeeft:

```python
{
    "object_name": f"{unique_name}-waker",
    "name": unique_name,                    # levert app-label + Service-match
    "extra_selector_labels": {"zad-role": "waker"},
    "imageURL": settings.SLEEP_MODE_WAKER_IMAGE,
    "replicas": 1,
    "inbound_ports": [8080],
    "application_port": 8080,
    "probe_scheme": "http",
    "probe_liveness_path": "/__zad/healthz",
    "probe_readiness_path": "/__zad/ready",
    "probe_readiness_failure_threshold": 1,
    "env_from_configmaps": [f"{unique_name}-waker-config"],
    "env_from_secrets": [f"{unique_name}-waker-token"],
    "resources_requests_cpu": "10m",
    "resources_requests_memory": "25Mi",     # clusterondergrens, cluster_config.py:40
    "resources_limits_cpu": "100m",
    "resources_limits_memory": "64Mi",
    "storage_configs": [], "sidecars": [], "attachment_secret_mounts": [],
    "ca_config": None, "env_vars": {}, "command": None,
}
```

De labels `app`, `component: application`, `deployment` en `project` zet de template al, en dat
is precies wat de Service en `tenant-baseline-network-policy.yaml.jinja` nodig hebben. Die
netpol staat egress naar `ops_namespace` toe, dus de wekker bereikt de OPI-API over
cluster-interne DNS zonder internetpad.

### ConfigMap-inhoud

```
ZAD_API_URL             http://operations-manager.<ops-namespace>.svc.cluster.local:<poort>
ZAD_PROJECT             <projectnaam>
ZAD_DEPLOYMENT          <deploymentnaam>
ZAD_APP_TITLE           uit config.title met placeholders ingevuld, anders de deploymentnaam
ZAD_APP_DESCRIPTION     uit config.description, anders leeg
ZAD_WAKE_MODE           auto | confirm | manual
ZAD_POLL_INTERVAL_SEC   3
```

Het wektoken staat bewust níét in de ConfigMap maar in het Secret.

---

## 8. Beveiliging van de wek-call

De project-API-key is hiervoor niet bruikbaar en mag dat ook niet worden. Hij wordt gemunt door
`opi/utils/api_keys.py:generate_api_key`, AGE-versleuteld in
`opi/forms/editables/generators.py:83` en opgeslagen op `config.api-key` in het projectbestand
(`project_v2.json:162`, type `age-encrypted`). Hij staat nergens als Secret in het cluster en
dat moet zo blijven: die key mag deployments verwijderen en images wijzigen.

In plaats daarvan een **wektoken per deployment**, via dezelfde drie bestaande mechanismen:
munten en AGE-versleuteld wegschrijven op `deployments[].sleep.wake-token` zodat OPI hem kan
teruglezen; SOPS-versleuteld als Secret in de namespace renderen via de bestaande
secret-pijplijn, zoals de authorization-wall zijn cookie-secret krijgt; en als
`envFrom.secretRef` in de wekkerpod.

Blast radius bij lekken: één deployment wekken, verder niets. Bewust geen clusterbrede token in
de stijl van `MetricsAuthSecret` (`opi/utils/secrets.py`, expliciet clusterbreed), want dan wekt
één lek uit een publiek bereikbare wekkerpod alles.

Tweede laag die niets kost: het endpoint accepteert alleen de overgang `sleeping` naar `waking`,
en alleen voor een deployment die daadwerkelijk matcht. Al het andere is een no-op. Een gelekt
token kan dus niet afschalen, verwijderen of config wijzigen.

---

## 9. Het wekkerimage

`images/zad-waker/` (Dockerfile plus `main.go`), naar het model van `images/haproxy-redirect`.
Distroless of scratch, draait als UID 1001 non-root, `GOMEMLIMIT` en `GOMAXPROCS` expliciet
gezet (anders sized de Go-runtime zich op de node-CPU's en groeit de heap nodeloos in een
container met 100m CPU).

| Route | Gedrag |
|---|---|
| `GET /__zad/healthz` | Altijd 200. Voedt liveness en startup. |
| `GET /__zad/ready` | 200 zolang de app níét terug is, 503 zodra wel. Bewust omgekeerd: zo haalt de wekker zichzelf uit de EndpointSlice. |
| `GET /__zad/status` | JSON: `{"state":"idle\|waking\|ready\|error","title":...,"description":...,"mode":...,"elapsed":42}` |
| `POST /__zad/wake` | Start het wekken. Alleen in `auto` en `confirm`; in `manual` altijd 403. Idempotent. |
| `GET /robots.txt` | `User-agent: *` / `Disallow: /` |
| al het overige | 200 met de pagina. In `auto` triggert dit ook het wekken. |

Gedrag:

- **Single-flight**: `sync.Once` rond de wek-call. Honderd gelijktijdige bezoekers geven één
  POST naar ZAD, dus één commit.
- **Poller**: na het wekken elke `ZAD_POLL_INTERVAL_SEC` een `GET` naar ZAD's statusendpoint.
  Stopt zodra de app `Available` is; vanaf dat moment geeft `/__zad/ready` 503.
- In `manual` polst de wekker óók, zonder ooit zelf te wekken, zodat hij zich netjes terugtrekt
  wanneer een beheerder via de UI-knop wekt.
- **Foutafhandeling**: faalt de wek-call, dan maximaal drie pogingen met backoff, daarna
  `state: error` met een begrijpelijke melding. Nooit stilletjes blijven draaien.
- **Pagina**: server-rendered `html/template` met titel en beschrijving uit de ConfigMap. De
  JavaScript pollt elke 2s `/__zad/status` en herlaadt zodra `state: ready`, of zodra het
  antwoord geen geldige JSON is, want dan raakte het verzoek de echte app. Meld eerlijk dat
  opstarten ongeveer een minuut kan duren.
  In `manual`: geen knop, alleen de tekst dat de applicatie in slaapstand staat en door een
  beheerder gestart moet worden.
- In `auto` alleen wekken op `GET`, en niet op `/favicon.ico` of `/__zad/*`. Crawlers vang je af
  met `robots.txt` en `X-Robots-Tag: noindex`. Blijkt dat niet genoeg, dan is `confirm` of
  `manual` het antwoord, niet meer heuristiek.

Publiceren naar `ghcr.io/minbzk/base-images/zad-waker`. OPI's bestaande ghcr-naar-RCR-rewrite
regelt de pull op ODCN. Neem het image mee in de Trivy-scanflow.

---

## 10. Actieknoppen vanuit een service

`section-deployment-actions.html.j2` heeft nu vier hardcoded knoppen, en leidt op regel 4-5 zelf
`has_database` af om er een van te tonen. Voeg een klein generiek mechanisme toe:

- `DeploymentAction` dataclass: `label`, `icon`, `kind`, `confirm_message | None`, `endpoint`
  (webroute-pad), `visible: bool`.
- Optioneel veld `actions_provider` op `ServiceDefinition`:
  `Callable[[dict, str], list[DeploymentAction]]`, met `(project_data, deployment_name)`.
- De template lust ná de bestaande knoppen over de acties van de services die het project heeft.
  De vier bestaande knoppen blijven staan; migreren mag later.

Voor deze service levert de provider één knop op, alleen zichtbaar als `state != "awake"`:
label "Applicatie wekken", icoon `uitvoering`, met bevestiging.

Twee valkuilen die hier al eerder toesloegen:

- **ROOS `c-button` verhaspelt JSON en aanhalingstekens in attributen.** Gebruik padparameters
  plus `hx-include`, of enkel-aangehaalde strings. Zet geen JSON in `hx-vals` of `hx-headers`.
- **Elke htmx-POST heeft het CSRF-token nodig.** Zie de bestaande knoppen in dezelfde template.

---

## 11. Servicepackage

Alles bij elkaar in `opi/services/sleep_mode/`.

| Bestand | Inhoud |
|---|---|
| `config.py` | `SleepModeConfig` dataclass, `load(project_data, cluster) -> SleepModeConfig \| None` inclusief de clusterdefault-samenvoeging, `matches(deployment_name)` via `fnmatch`, duur-parsing (`48h`, `90m`) |
| `state.py` | `read(project_data, deployment_name) -> SleepState`, `write(...)`. Model: `extract_deployment_component_disabled` (`project_file_handler.py:1461`) en `set_deployment_component_disabled` (regel 1526) |
| `token.py` | Wektoken munten, AGE-versleuteld wegschrijven, teruglezen, vergelijken met `secrets.compare_digest` |
| `service.py` | `sleep(project, deployment)`, `wake(project, deployment, actor)`, `finish_wake(...)`. Het enige pad dat toestand wijzigt; de webroute en de API-route roepen allebei dit aan |
| `manifests.py` | Bouwt de values-dicts uit paragraaf 7, geeft `None` terug wanneer er geen wekker hoort te zijn |
| `actions.py` | De `actions_provider` uit paragraaf 10 |
| `router.py` | De `APIRouter` uit paragraaf 12 |
| `scheduler.py` | De sweeper uit paragraaf 13 |
| `secret.py` | `WakeTokenSecret(BaseSecret)` met `SERVICE_TYPE = ServiceType.SLEEP_MODE`, model `MetricsAuthSecret` in `opi/utils/secrets.py` |

Registratie: `ServiceType.SLEEP_MODE = "sleep-mode"` in `opi/services/services_enums.py`, plus
een `ServiceDefinition` in de `_SERVICES`-map van `opi/services/services.py` (model:
`ServiceType.METRICS_SCRAPER`, regel ~500) met `secret_class` en `actions_provider`.

---

## 12. Twee ingangen, één implementatie

```
POST /api/sleep-mode/{project_name}/{deployment_name}/wake      wektoken, voor de wekkerpod
GET  /api/sleep-mode/{project_name}/{deployment_name}/status    wektoken, voor de wekkerpod
POST /projects/{project_name}/deployments/{deployment_name}/wake  sessie + CSRF, voor de UI-knop
```

De API-route krijgt een eigen decorator naast de bestaande in `opi/api/endpoint_util.py`, die
het wektoken uit de header vergelijkt met `secrets.compare_digest`. Níét `validate_api_token`,
want dat is de project-API-key. De webroute gebruikt de bestaande sessie-auth, CSRF en rolcheck
(`admin` of `owner`, zoals de rest van `section-deployment-actions.html.j2`).

Beide roepen `sleep_mode.service.wake(...)` aan. Statuscodes op de API: 401 zonder of met
verkeerd token, 404 bij onbekend project of deployment, 200 met `{"state": ...}` bij een no-op,
202 als er echt een overgang start.

De implementatie volgt exact `_run_sanitize` in `opi/api/resource_router.py`:

```python
project_manager = ProjectManager(project_file_relative_path=f"projects/{filename}")
try:
    project_data = await project_manager.get_contents()     # verse, gemigreerde data
    ...  # state.write(...)
    await project_manager.save_and_commit_project(project_data, commit_msg)
    await trigger_reprocessing(project_name, filename, deployment_name,
                               argocd_resources_changed=False)
    await argo.sync_application(app_name)
finally:
    await project_manager.close()
```

`get_contents()` is bewust de mutatiepad-load: vers uit git, niet uit de cache, zodat een
verouderde cache geen gelijktijdige wijziging overschrijft.

Het statusendpoint leest `KubectlConnector.get_deployment_status(namespace, unique_name)` en
vertaalt dat naar `starting` of `ready`.

---

## 13. Sweeper

`sleep_mode/scheduler.py`, interval-gebaseerd naar het model van
`opi/core/db_console_reaper.py` (die doet al expiry-parsing). Itereert
`get_project_store().get_all()` gefilterd op `settings.CLUSTER_MANAGER`, zoals
`opi/core/resource_tuning_scheduler.py:_sweep`.

Per ronde twee taken:

1. Matchende deployments met `state: awake` en `expires-at < now`: naar `sleeping`.
2. Deployments die langer dan `SLEEP_MODE_WAKING_TIMEOUT_MINUTES` in `waking` staan: naar
   `awake`, met een `logger.warning`.

Pacen tussen gewijzigde projecten, per project loggen wat er gebeurde. Een sweep die niets doet
mag stil zijn.

---

## 14. Integratiepunten in bestaande code

| Plaats | Wijziging |
|---|---|
| `project_manager.py:4875-4882` | `replicas = 0 if (is_disabled or state == "sleeping") else 1`. `disabled` wint altijd. Geldt voor élk component, ook zonder endpoint. |
| `project_manager.py:5360-5410` | Extra renders wanneer `sleep_mode.manifests` iets teruggeeft. De voorwaarden liggen in de service: `state` in (`sleeping`, `waking`), deployment matcht, `waker: true`, het component is gekozen volgens de regels in paragraaf 5, en géén passthrough of eigen TLS (de wekker heeft dat certificaat niet). Maximaal één wekker per deployment. |
| `project_manager.py:5335-5357` | Model volgen voor het standalone ConfigMap-manifest, inclusief `created_files` zodat de prune werkt. |
| `server.py:434-448` | `app.include_router(sleep_mode_router, include_in_schema=True)` |
| `server.py:173-182` | Scheduler starten achter `SLEEP_MODE_SCHEDULER_ENABLED`, plus `stop()` in de shutdown rond regel 253. |
| `templates/project-details/section-deployment-actions.html.j2` | Lus over service-acties ná de bestaande knoppen. |
| `opi/api/router.py:1812`, het v2-equivalent `PUT /api/v2/projects/{p}/deployments/{d}/image`, en het upsert-pad | Deadline resetten: `expires-at = now + sleep-after-deploy`, `state: awake`. |
| `opi/core/cluster_config.py` | `sleep_mode`-defaults per cluster, plus een `get_sleep_mode_defaults(cluster)`-helper naast `get_min_memory_limit_mi` (regel 654). |

### Settings in `opi/core/config.py`

```python
SLEEP_MODE_SCHEDULER_ENABLED: bool = True
SLEEP_MODE_SWEEP_MINUTES: int = 30
SLEEP_MODE_PACE_SECONDS: int = 15
SLEEP_MODE_WAKING_TIMEOUT_MINUTES: int = 10
SLEEP_MODE_WAKER_IMAGE: str = "ghcr.io/minbzk/base-images/zad-waker:latest"
```

---

## 15. Bewuste beperkingen

Neem deze letterlijk over in het feature-document.

- **Eén wekker per deployment.** Heeft een deployment meerdere web-gepubliceerde componenten,
  dan krijgen de overige hostnames tijdens de slaapstand een 503 van de router. Wie de
  wekker-hostname bezoekt wekt wél de hele deployment, want alle componenten schalen samen terug
  omhoog. Zet `waker-component` op het component dat mensen als eerste bezoeken, meestal de
  frontend.
- De wekker werkt alleen voor HTTP-componenten met `publish-on-web`. Passthrough en eigen TLS
  vallen af, want de wekker heeft dat certificaat niet. De slaapstand werkt daar wél.
- Niet-browserclients krijgen de HTML-pagina in plaats van een `503` met `Retry-After`. Voor
  previews acceptabel, later te verfijnen.
- Geen detectie van inactiviteit, en dat kan ook niet: de Prometheus in
  `infrastructure/bootstrap/infrastructure/prometheus/` draait tenant-scoped via Capsule Proxy
  (zie `overlays/odcn/configmap-patch.yaml`: geen node-jobs) en de HAProxy-routermetrics zitten
  in `openshift-ingress`, buiten bereik van de tenant. Een preview die de hele dag gebruikt wordt
  gaat na de deadline toch in slaapstand en moet één keer opnieuw gewekt worden.
- Kort venster waarin app en wekker allebei achter de Service zitten. De pagina vangt dat op door
  te herladen zodra het antwoord geen geldige JSON is.
- Databases en PVC's slapen niet mee. Een preview met een eigen CNPG-cluster houdt zijn
  Postgres-pod, dus de besparing is gedeeltelijk. Meeslapende databases is een apart traject.
- De applicatie start **koud** op. Sessies, caches en geheugen overleven de slaapstand niet.
  Zet dit expliciet in de documentatie.
- Wektijd is de git-round-trip plus de opstarttijd van de app. Dat is de prijs van het
  projectbestand als enige bron, bewust gekozen.

---

## 16. Implementatievolgorde

Elke stap is los te verifiëren. Draai tests gericht, niet de hele suite:
`uv run pytest tests/<pad> -x -q --tb=short`.

**Stap 1: template-uitbreidingen.** De vier variabelen plus `configmap.yaml.jinja`.
*Verificatie:* render een bestaand component vóór en ná, diff moet leeg zijn.

**Stap 2: schema, config en state.** `$defs.deployment.sleep`, `services_enums`,
`ServiceDefinition`, clusterdefaults, `config.py`, `state.py`.
*Verificatie:* units op de matcher (`PR-123` matcht `PR-*`, `main` niet), de
clusterdefault-precedentie, duur-parsing, en de overgangen. Valideer een bestaand project ná
`migrate_to_latest()`, niet ruw.

**Stap 3: slaapstand compleet.** `service.py`, `replicas`-koppeling, sweeper, binding in
`server.py`. Nog geen wekker: slapen geeft een 503 en wekken kan alleen via de API. Hiermee is
de besparing al binnen, ook voor deployments zonder endpoint.
*Verificatie:* sandbox met `sleep-after-deploy: 2m`, app gaat naar 0 na de sweep, een
niet-matchende deployment niet.

**Stap 4: actieknop.** `DeploymentAction`, `actions_provider`, de webroute, de templatelus.
*Verificatie:* knop verschijnt alleen bij een slapende deployment, wekt hem, en verdwijnt daarna.
Dit is meteen het enige wekmechanisme voor deployments zonder endpoint.

**Stap 5: token en API-route.** `token.py`, `secret.py`, `router.py`.
*Verificatie:* wek-call zonder token geeft 401, met het token van een ándere deployment ook, een
tweede call levert geen tweede commit op. Controleer met `git log` op de projects-repo dat er
precies één commit per echte overgang staat.

**Stap 6: wekkerimage.** `images/zad-waker/`, bouwen, publiceren.
*Verificatie:* lokaal `docker run` met de env uit paragraaf 7, alle routes en alle drie de modi
langslopen.

**Stap 7: wekkermanifesten.** `manifests.py` plus de integratie in `project_manager`.
*Verificatie:* de eind-tot-eind-test uit paragraaf 17.

**Stap 8: deadline-reset op image-update en upsert.**

**Stap 9: `features/sleep-mode.md`.**

---

## 17. Eind-tot-eind-verificatie

Sandbox (`task sandbox:setup`), testproject met `match: ["PR-*"]` en `sleep-after-deploy: 2m`,
en een tweede deployment zónder web-gepubliceerd component.

1. Na de sweep: `kubectl get deploy` toont beide apps op 0, en `<naam>-waker` op 1 bij de
   deployment mét endpoint.
2. `kubectl get endpointslice` toont alleen de wekker.
3. `curl https://<host>` geeft de pagina met de juiste titel uit de ConfigMap.
4. In `kubectl logs -n rig-system deployment/operations-manager -f` verschijnt precies één
   wek-call, ook bij tien gelijktijdige requests.
5. **Meet de round-trip** (commit, hergenereren, sync, podstart) en leg het getal vast in het
   feature-document. Dat is wat we gebruikers beloven en waaraan we een regressie zien.
6. Tijdens `waking`: de app start, de wekker is nog steeds het enige endpoint.
7. Binnen enkele seconden na `Available`: alleen de app is nog endpoint, de wekker staat op
   `0/1 READY`.
8. Na de opruimcommit zijn wekker-Deployment, ConfigMap en Secret gepruned.
9. `wake-mode: confirm`: pagina met knop, er gebeurt niets tot je klikt.
10. `wake-mode: manual`: pagina zonder knop, `POST /__zad/wake` geeft 403, en de UI-knop wekt hem
    wel.
11. De deployment zónder endpoint: geen wekkerpod, en de UI-knop is het enige wat hem wekt.
12. Een deployment met twee web-componenten en zonder `waker-component`: geen wekkerpod, en een
    waarschuwing in de logs die beide kandidaten noemt. Daarna `waker-component` zetten en
    controleren dat er precies één wekker verschijnt, op het juiste component.
13. Een `disabled` component in een matchende deployment blijft op 0.
14. Controleer de Nederlandse teksten: overal "slaapstand", nergens "sluimerstand".
15. `uv run ruff check . --fix && uv run ruff format . && uv run pyright`.
