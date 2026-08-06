# Sleep-mode (slaapstand met wekken op verzoek)

Zet inactieve preview-/PR-deployments na een deadline in **slaapstand** (`replicas: 0`) en
wekt ze weer op zodra iemand de URL bezoekt, met een "applicatie wordt gestart"-pagina
zolang dat duurt. Zo geven previews die niemand bekijkt geen geheugen en CPU meer uit op
ODCN, zonder dat een ontwikkelaar iets hoeft te doen om ze weer aan te zetten.

## Belangrijk: het is slaapstand, geen sluimerstand

De applicatie **start koud op**. Er blijft niets bewaard: pods verdwijnen, en bij het
wekken start alles opnieuw. Sessies, caches en geheugen overleven de slaapstand niet.
Schrijf in Nederlandse teksten dus altijd **slaapstand** (sleep) en nooit sluimerstand
(dat is hibernate, met bewaarde staat).

## Wat het doet

| `sleep.state` | app-Deployment | wekker-Deployment |
|---|---|---|
| `awake` | `replicas: 1` | niet gegenereerd |
| `sleeping` | `replicas: 0` | `replicas: 1` (alleen als er een wekker hoort te zijn) |
| `waking` | `replicas: 1` | `replicas: 1` (alleen als er een wekker hoort te zijn) |

De tussentoestand `waking` voorkomt een gat: de app start op terwijl de wekker de
laadpagina nog serveert. Zodra de app terug is, trekt de wekker zich uit het verkeer
(zijn readinessprobe gaat 503 geven) en neemt de app het over.

## De slaapstand werkt overal; de wekker is een aanvulling

De slaapstand werkt voor **elke** deployment, ook zonder webendpoint. De **wekker** (de
"applicatie wordt gestart"-pagina) bestaat alleen waar een component `publish-on-web`
heeft met standaard-TLS.

| Situatie | Wekkerpod | Wat de bezoeker ziet | Wie kan wekken |
|---|---|---|---|
| Geen web-gepubliceerd component | nee | n.v.t. | knop in de UI, API |
| Wel web, `waker: false` | nee | 503 van de router | knop in de UI, API |
| `wake-mode: manual` | ja | uitlegpagina zonder knop | knop in de UI, API |
| `wake-mode: confirm` | ja | pagina met knop | bezoeker na een klik, plus bovenstaande |
| `wake-mode: auto` | ja | laadpagina, wekt direct | bezoeker bij het eerste verzoek, plus bovenstaande |

In `manual` en `confirm` kan geen crawler, uptime-check of linkpreview per ongeluk een
applicatie starten.

## Gebruik

Sleep-mode is een selecteerbare wizard-service met een eigen configuratiestap
("Slaapstand configuratie"). Kies hem in de wizard en vul de sectie in; bijna alle velden
zijn selectielijsten (wektype, de twee slaap-deadlines, wekker-component en de Ja/Nee-
schakelaars), alleen `match` is vrije tekst (glob-patronen, een per regel). De service
declareert die velden zelf (`catalog/sleep_mode/editables.py` + `visualizers.py`), zoals de
andere services. Onder water schrijft dat exact hetzelfde config-blok als hieronder.

Handmatig in het projectbestand kan ook, onder `services:`.

```yaml
services:
  - name: sleep-mode
    config:
      enabled: true
      match: ["PR-*"]           # glob op deploymentnaam; alleen deze slapen
      sleep-after-deploy: 48h   # deadline bij aanmaak en bij elke image-update
      sleep-after-wake: 1h      # nieuwe deadline na een wek-call
      waker: true               # een wekkerpod genereren
      waker-component: frontend # optioneel; verplicht bij meerdere web-componenten
      wake-mode: auto           # auto | confirm | manual
      title: "{deployment}"     # optioneel; {project} {deployment} {component}
      description: ""           # optioneel
```

- `match` is het enige selectiemechanisme: zo laat je `main` met rust en slapen alleen de
  `PR-*` previews. Een lege `match` laat niets slapen.
- Duur-notatie: `48h`, `90m`, `30s`, `2d`. Een typefout faalt luid bij het laden.

### Clusterbrede default

De service bezit zelf een clusterbrede default (in het servicepackage, niet in
`core/cluster_config.py`). Standaard staat sleep-mode **overal uit**; op `sandboxed-local`
staat hij aan zodat de sandbox het gedrag laat zien. Een project overschrijft de default
per sleutel. Zet hem pas op `odcn-production` aan als de werking daar bevestigd is: een
clusterbrede regel raakt iedereen tegelijk.

### Welk component krijgt de wekker

Er komt **één wekker per deployment**. Volgorde:

1. Staat `waker-component` gezet en heeft dat component `publish-on-web`, dan die.
2. Anders, heeft precies één component `publish-on-web`, dan die.
3. Anders (nul, of twee of meer web-componenten zonder `waker-component`): **geen wekker**,
   met een waarschuwing in de logs die de kandidaten opsomt. De deployment slaapt gewoon en
   is te wekken via de knop of de API.

Verwijst `waker-component` naar een niet-bestaand component, dan faalt de config luid.

## Wekken en handmatig slapen (toggle)

Twee transities, elk één implementatie (`sleep_mode.flow.wake` / `sleep_mode.flow.sleep`):

```
POST /api/sleep-mode/{project}/{deployment}/wake       # wektoken, voor de wekkerpod
GET  /api/sleep-mode/{project}/{deployment}/status     # wektoken, voor de wekkerpod
POST /projects/{project}/deployments/{deployment}/wake  # sessie + CSRF, voor de UI-knop
POST /projects/{project}/deployments/{deployment}/sleep # sessie + CSRF, voor de UI-knop
```

- De UI toont precies **één** knop per deployment, afhankelijk van de status (alleen voor
  `admin`/`owner`, alleen voor deployments die onder `match` vallen):
  - `awake` → **"Deployment slapen"** (handmatig in slaapstand, `awake -> sleeping`),
  - `sleeping`/`waking` → **"Applicatie wekken"** (`sleeping -> waking`).

  De twee vormen samen een toggle: er staat er altijd maar één. Handmatig slapen mint —
  net als de sweeper — een wektoken als er een wekker komt, zodat de deployment daarna weer
  gewekt kan worden. Wakken zet de volgende deadline op `sleep-after-wake`, dus na een
  handmatige wake valt de deployment vanzelf weer in slaapstand.
- De API-endpoints (wekkerpod) gebruiken een **wektoken per deployment** (`X-Wake-Token`),
  bewust niet de project-API-key: een gelekt wektoken kan één deployment wekken en verder
  niets. Het endpoint accepteert alleen `sleeping -> waking`; al het andere is een no-op.

## Beveiliging van de wek-call

Per deployment wordt een wektoken gemunt, AGE-versleuteld op
`deployments[].sleep.wake-token` gezet (zodat OPI hem kan teruglezen) en als SOPS-secret in
de namespace gerenderd voor de wekkerpod (`envFrom`). Blast radius bij lekken: één
deployment wekken. Tweede laag: het endpoint accepteert alleen de overgang
`sleeping -> waking` voor een matchende deployment; al het andere is een no-op.

## Onder de motorkap

- **Twee Deployments achter één Service.** De wekker draagt hetzelfde `app`-label en
  `component: application` als de app, plus `zad-role: waker`. Hij landt daardoor
  automatisch achter dezelfde Service en Ingress; er verandert niets aan de Route,
  cert-manager of de ArgoCD Application.
- **Waarom geen sidecar?** `replicas: 0` betekent nul pods, dus een sidecar (een container
  ín de pod) is óók weg. Wie het verzoek wil opvangen moet buiten de geschaalde workload
  leven — een aparte, altijd draaiende pod.
- **Het projectbestand is de bron van waarheid.** Geen `ignoreDifferences`, geen
  `kubectl scale` buiten ArgoCD om, `selfHeal` blijft aan. Elke overgang schrijft eerst
  naar het projectbestand en commit dat; pas daarna worden manifesten hergenereerd (alleen
  die deployment, `argocd_resources_changed=False`).
- **Commit-storm voorkomen.** De wekker doet single-flight (één wek-call per pod-leven), het
  wek-endpoint is idempotent, en de sweeper paced tussen projecten.

De sweeper (`SLEEP_MODE_SWEEP_MINUTES`, standaard 30 min) doet per ronde: een matchende
`awake` deployment zonder deadline krijgt er een (`sleep-after-deploy`); een `awake` met
verlopen deadline gaat `sleeping`; een deployment die te lang (`SLEEP_MODE_WAKING_TIMEOUT_MINUTES`,
standaard 10) in `waking` staat gaat terug naar `awake` (kapot image dat nooit terugkwam).

## Het wekkerimage

`images/zad-waker/` (Go, distroless, non-root UID 1001). Gepubliceerd naar
`ghcr.io/minbzk/base-images/zad-waker`; OPI's ghcr->RCR-rewrite regelt de pull op ODCN.

- **Publiceren naar ghcr:** `task publish-waker` (multi-platform build + push, net als de
  andere base-images).
- **Sandbox (kind):** `task build-waker-image` bouwt lokaal en `kind load`t de tag
  `ghcr.io/minbzk/base-images/zad-waker:sandbox`. Die pinned tag matcht
  `SLEEP_MODE_WAKER_IMAGE` in de sandboxed-local overlay, dus het `IfNotPresent`-beleid
  gebruikt de geladen image zonder te pullen.

De pagina hergebruikt bewust de vormgeving van de authorization sign-in card, zodat de twee
pagina's consistent zijn.

## Configuratie (settings)

Operationele toggles (env-overschrijfbaar), in `opi/core/config.py`:

| Setting | Standaard | Betekenis |
|---|---|---|
| `SLEEP_MODE_SCHEDULER_ENABLED` | `True` | Start de sweeper |
| `SLEEP_MODE_SWEEP_MINUTES` | `30` | Interval van de sweeper |
| `SLEEP_MODE_PACE_SECONDS` | `15` | Pauze tussen gewijzigde projecten |
| `SLEEP_MODE_WAKING_TIMEOUT_MINUTES` | `10` | Terug naar `awake` als `waking` vastloopt |
| `SLEEP_MODE_WAKER_IMAGE` | `ghcr.io/minbzk/base-images/zad-waker:latest` | Wekkerimage |

Deze settings horen in de standaard OPI-configmap, niet ad-hoc als env. Ze staan in de
overlays `bootstrap/rig-system/kustomize/operations-manager/overlays/*/configmap.yaml`:
de sandbox zet de pinned `:sandbox`-tag + `SLEEP_MODE_SWEEP_MINUTES=1` (snelle cyclus voor
de E2E), productie zet de RCR-mirror-ref (sleep-mode zelf blijft daar uit tot gevalideerd).

## Afhankelijkheden

- [Diensten die elkaars toestand kennen](deployment-state-and-health.md) - sleep-mode
  meldt via `deployment_state()` dat een deployment slaapt, zodat de gezondheidscheck nul
  pods niet als storing leest en de deploymentweergave het uitlegt. Sinds RC-35 draagt dat
  feit ook de badge "Slaapstand", die op de deploymentkaart in de plaats komt van het
  groene `Healthy` -- een slapende deployment werd anders "gezond" genoemd met een blok
  eronder dat zei dat hij sliep. Een deployment die *gewekt* wordt krijgt geen badge: zijn
  pods horen terug te komen, dus juist daar moet de gezondheidsuitspraak zichtbaar blijven.
- `publish-on-web` (standaard-TLS) op het te wekken component, voor de wekker.
- De tenant-netpol staat egress naar de ops-namespace toe, dus de wekker bereikt de
  OPI-API over cluster-interne DNS.

## Bewuste beperkingen

- **Eén wekker per deployment.** Heeft een deployment meerdere web-gepubliceerde
  componenten, dan krijgen de overige hostnames tijdens de slaapstand een 503 van de
  router. Wie de wekker-hostname bezoekt wekt wél de hele deployment (alle componenten
  schalen samen omhoog). Zet `waker-component` op het component dat mensen als eerste
  bezoeken, meestal de frontend.
- De wekker werkt alleen voor HTTP-componenten met `publish-on-web`. Passthrough en eigen
  TLS vallen af, want de wekker heeft dat certificaat niet. De slaapstand werkt daar wél.
- Niet-browserclients krijgen de HTML-pagina in plaats van een `503` met `Retry-After`.
  Voor previews acceptabel, later te verfijnen.
- **Geen inactiviteitsdetectie**, en dat kan ook niet: de tenant-Prometheus ziet geen
  node-/routerjobs. Een preview die de hele dag gebruikt wordt gaat na de deadline toch in
  slaapstand en moet één keer opnieuw gewekt worden.
- Kort venster waarin app en wekker allebei achter de Service zitten. De pagina vangt dat
  op door te herladen zodra het antwoord geen geldige JSON is.
- Databases en PVC's slapen niet mee. Een preview met een eigen CNPG-cluster houdt zijn
  Postgres-pod, dus de besparing is gedeeltelijk.
- De applicatie start **koud** op. Sessies, caches en geheugen overleven de slaapstand niet.
- Wektijd is de git-round-trip plus de opstarttijd van de app — de prijs van het
  projectbestand als enige bron van waarheid, bewust gekozen.

## Architectuur (het servicepackage)

Alles zit in `opi/services/catalog/sleep_mode/` (de service bezit al zijn eigen config,
manifests, state, token, router en sweeper):

| Bestand | Inhoud |
|---|---|
| `__init__.py` | `SleepModeService` (declaratiehub) + bindt de wekknop op de ServiceDefinition |
| `config_model.py` | Pydantic-configmodel + duur-parsing (drift-locked schemafragment) |
| `config.py` | Service-eigen clusterdefault + merge + `load()` |
| `state.py` | Runtime-state `deployments[].sleep` lezen/schrijven |
| `token.py` | Wektoken munten, AGE-versleutelen, teruglezen, vergelijken |
| `secret.py` | `WakeTokenSecret` (SOPS-secret in de wekkerpod) |
| `service.py` | De pure, idempotente toestandsovergangen |
| `manifests.py` | Wekker-componentselectie + values-dicts |
| `actions.py` | De wekknop (`DeploymentAction`) |
| `flow.py` | Wake/status-orchestratie (git) die beide ingangen delen |
| `router.py` | De API-router |
| `scheduler.py` | De sweeper |
