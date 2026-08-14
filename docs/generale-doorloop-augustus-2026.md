# De generale: de hele suite tegen een nieuwe versie van ZAD

Laatste meting van `release-augustus-2026` voor de merge naar `main`.

- **Gemeten commit**: `e187015e` (`fix(services): een standaardmaat die je ook echt kunt kiezen`)
- **Cluster**: `kind-rig-sandbox`, `sandboxed-local`
- **Datum**: 14 augustus 2026

> Deze doorloop begon op `418533e5`. Halverwege kwam de opdracht binnen om op
> `e187015e` te meten, omdat daar de standaardmaten in zitten die deze release
> juist moet aantonen. Alles hieronder is opnieuw gemeten op `e187015e`; wat nog
> van de eerste ronde stamt staat er expliciet bij.

## Oordeel

(volgt aan het eind van de doorloop)

## Taak 1 - Verse sandbox met een verse build

### Wat er niet gedaan kon worden, en waarom

Het plan vraagt `task sandbox:destroy` gevolgd door `task sandbox:setup`. **Dat is
in deze sessie niet uitgevoerd, en dat is een bewuste keuze.**

`sandbox:setup` begint met `task requirements-check`, en die faalt hier hard:

```
task: sops is not installed! Install with 'brew install sops' ...
task: precondition not met
```

Naast `sops` ontbreken ook `yq` en `pwgen`. Zwaarder weegt dat `sandbox:setup` als
tweede stap `sandbox:decrypt-wildcard-cert` draait, en die heeft
`security/developer-key.txt` nodig. Die sleutel wordt buiten de repo om verstrekt en
staat hier niet:

```
/workspace/security/
  readme.md
  tls/sandbox-wildcard/{fullchain.pem.age, privkey.pem.age}
```

Er is dus geen weg terug: `sandbox:destroy` zou het gedeelde cluster slopen zonder
dat deze sessie het opnieuw kan opbouwen. Dat is precies waar
`workflow/sandbox.md` voor waarschuwt ("Do NOT run `task sandbox:update-operations-manager`
(or `sandbox:setup`) in a session"). Daarom is het cluster blijven staan en is de
versie onder toets erop gezet met `sandbox-deploy`, de weg die voor sessies bedoeld is.

**Gevolg voor de meting**: het "vanaf nul"-argument uit het plan - dat de helft van
de vorige bevindingen uit blijven staande toestand kwam - geldt hier niet. Wat hier
gemeten is, is gemeten op een cluster dat al draaide. Wie een echte verse doorloop
wil, moet die op een machine draaien die de developersleutel en `sops` heeft.

### Versiecontrole

`kubectl config current-context` gaf `kind-rig-sandbox`.

Vijf keer achter elkaar, na afloop van de rollout:

```
expect=e187015e
1: e187015e pod=operations-manager-747d6b5698-t54jn
2: e187015e pod=operations-manager-747d6b5698-t54jn
3: e187015e pod=operations-manager-747d6b5698-t54jn
4: e187015e pod=operations-manager-747d6b5698-t54jn
5: e187015e pod=operations-manager-747d6b5698-t54jn
```

Eén pod, één commit, geen mengsel.

**Wat hier misging**: `sandbox-deploy` meldde bij de tweede build zelf
`WARN - /version does not clearly show e187015e`. Het script leest `/version`
direct na `kubectl set image`, terwijl de oude pod dan nog antwoordt. Na
`kubectl rollout status` klopte het beeld wel. De waarschuwing is dus terecht
maar te vroeg gemeten - precies de valkuil die het plan beschrijft.

### De probepoort (nieuw in deze release)

Hier zit de belangrijkste bevinding van taak 1.

**`sandbox-deploy` zet alleen het image, niet het manifest.** De pod draaide daardoor
na de deploy nog op de OUDE deploymentspec: één containerpoort (8000) en alle drie de
probes op 8000. De probepoort uit deze release stond wél in de branch
(`bootstrap/rig-system/kustomize/operations-manager/base/deployment.yaml`), maar niet
op het cluster. Wie alleen `sandbox-deploy` draait en dan `describe pod` leest, meet
de vorige release en ziet dat niet.

De deployment blijkt niet door ArgoCD beheerd (geen `argocd.argoproj.io/instance`-label,
alleen `kubectl.kubernetes.io/last-applied-configuration`), dus de nieuwe spec is er met
een strategic-merge patch op gezet: de twee containerpoorten plus de drie probes precies
zoals de branch ze definieert. Daarna:

```
ports:
  - containerPort: 8000  name: http
  - containerPort: 8001  name: probe

Liveness:   http-get http://:probe/healthz  period=30s failure=3
Readiness:  http-get http://:probe/readyz   period=30s failure=3
Startup:    http-get http://:probe/healthz  delay=5s period=5s failure=60
Ready: True    Restart Count: 0
```

Port-forward naar 8001:

```
/healthz -> HTTP 200 {"status": "ok"}
/readyz  -> HTTP 200 {"status": "ok"}
```

De pod werd `Ready` met **0 herstarts**. Tijdens het opkomen faalt de startup-probe
een paar keer met `connection refused` op 8001 - dat is de verwachte race tussen
kubelet en het bindende proces, en de `failureThreshold: 60` vangt dat af.

Ook goed om vast te leggen: de `readinessProbe` staat nu op `periodSeconds: 30` met
`failureThreshold: 3`. Dat is de correctie op wat in RC-112 gemeld werd, waar
`failureThreshold: 1` één trage meting genoeg maakte om de pod uit de endpoints te
halen en de hele API een 503 te laten geven.

## Taak 2 - Unit, e2e en sandbox

(volgt)

## Taak 3 - De reallife-suite

(volgt)

## Taak 4 - Wat deze release nieuw heeft, in de browser

(volgt)

## Taak 5 - De API-weg en de documentatie

### De doorloop met curl tegen `/api/v2`

Alle stappen op `e187015e`, tegen het draaiende cluster. Elke asynchrone stap is
afgewacht op zijn **taak**, niet op een klok.

| Stap | Aanroep | Uitkomst |
|---|---|---|
| Aanmaken | `POST /api/v2/projects` | 202, `project_name=rd-xyt`, taak `completed` |
| Opvragen | `GET /api/v2/projects/rd-xyt` | 200 |
| Dienst | `POST /api/v2/projects/rd-xyt/services` (`postgresql-database`) | 202, taak `completed` |
| Deployment | `POST /api/v2/projects/rd-xyt/:upsert-deployment` (`prod`) | 202, taak `completed` |
| Component | `POST /api/v2/projects/rd-xyt/components` (`web`, gekoppeld aan `prod`) | 202, taak `completed` |
| Opvragen | `GET /api/v2/projects/rd-xyt` | component `web` + deployment `prod` staan erin |
| Verwijderen | `DELETE /api/projects/rd-xyt` | 200, alle 17 opruimstappen `success` |

De uitrol is ook echt op het cluster gecontroleerd en niet alleen op het antwoord:

```
namespace  rig-rd-xyt              Active
pod        prod-web-...            1/1 Running   0 herstarts
argocd     rd-xyt-prod             Synced  Healthy
```

Na het verwijderen was de namespace weg.

### Bevinding 1 - `/openapi.json` noemt de verkeerde beveiliging voor het aanmaken

**Dit is de bevinding die de CLI raakt.**

`POST /api/v2/projects` is het enige endpoint dat geen projectsleutel kan gebruiken -
het project bestaat nog niet. De code eist daarom een SSO-token
(`@validate_user_token`, `Authorization: Bearer <token>`), en de docstring van het
endpoint legt dat ook netjes uit.

Het OpenAPI-document zegt iets anders:

```
$ jq '.paths["/api/v2/projects"].post.security' openapi.json
[ { "APIKeyHeader": [] } ]

$ jq '.components.securitySchemes | keys' openapi.json
[ "APIKeyHeader" ]
```

Er staat maar één beveiligingsschema in het hele document, en dat wordt aan alle
**95** v2-operaties gehangen, inclusief deze. Een bearer-schema komt in het document
niet voor. Wie zich op het document baseert - en dat is precies wat een gegenereerde
client doet - stuurt `X-API-Key` en krijgt:

```
HTTP 401 {"detail":"Authentication required - provide a valid Authorization: Bearer token"}
```

Empirisch bevestigd: met de `ADMIN_API_KEY` in de header geeft dit endpoint 401, met
een `zad-cli`-token met `aud: zad-api` geeft het 202.

Het is een fout in het **document**, niet in het gedrag: de API doet precies wat hij
hoort te doen. Maar het document is hier de machineleesbare afspraak, en die klopt niet.

### Bevinding 2 - aanmaken zit op v2, verwijderen niet

`POST /api/v2/projects` bestaat; `DELETE /api/v2/projects/{project_name}` niet. Het
verwijderen zit op de oude route `DELETE /api/projects/{project_name}`. Wie de v2-API
afloopt vindt geen manier om een project op te ruimen. Werkt allemaal, maar de
levenscyclus staat op twee plekken.

Kleinigheid daarbij: die DELETE eist een body (`{"confirmDeletion": true}`) en geeft
zonder body een 422 met `loc: ["body"]`. Correct, maar niet te raden zonder het
document erbij.

### De toegestane waarden in `/openapi.json` (nieuw in deze release)

Dit is het stuk dat expliciet getoetst moest worden, en het klopt.

**Een vaste keuzelijst krijgt een echte `enum`.** Niet alleen een zin in de
beschrijving. Bijvoorbeeld `SleepModeConfig.wake-mode`:

```json
"enum": ["auto", "confirm", "manual"],
"x-choices": [
  {"const": "auto",    "title": "Automatisch", "description": "Wekt bij het eerste bezoek; ..."},
  {"const": "confirm", "title": "Met bevestiging", ...},
  {"const": "manual",  "title": "Alleen handmatig", ...}
]
```

**Een veld waarvan de keuzes per project verschillen krijgt géén verzonnen `enum`,
maar een machineleesbare verwijzing.** 25 keer in het document, bijvoorbeeld
`SleepModeConfig.waker-component`:

```json
"x-choices-source": {
  "endpoint": "GET /api/v2/projects/{project_name}/components",
  "path": "components[].name",
  "description": "De componenten van dit project. ..."
}
```

Er staat dus een endpoint en een pad naar de waarden - genoeg om een client de lijst
zelf te laten ophalen. Dat is precies wat het plan vroeg.

Alle 12 velden met `x-choices` zijn nagelopen, met `$ref`/`anyOf` opgelost:

- waar een `enum` bestaat, is die **exact gelijk** aan de `x-choices` (4 velden:
  `provide-as`, `scheme`, `account-link`, `tls`, `wake-mode`);
- waar geen `enum` staat, accepteert het configmodel ook werkelijk vrije tekst
  (`domain-mode` is `str | None` en legacy, `keycloak/template` is een bestandsnaam
  die op schijf bestaat, de duur- en maatvelden zijn vrije Kubernetes-quantities).
  De lijst is daar een suggestie en geen grens, en dat is een verdedigbaar verschil.

### Het formulier tegenover het configmodel

Het plan zegt: wijkt een keuzelijst in het formulier af van wat het configmodel
toestaat, dan is **dat** de bevinding. Dat was bij de eerste meting op `418533e5`
precies het geval, en het is de reden dat deze doorloop opnieuw moest:

- `NamespacePostgresConfig.storage` had `default: "10Gi"`, terwijl de keuzelijst
  `["50Mi","100Mi","250Mi","500Mi","1Gi"]` is. De standaardwaarde stond dus niet in
  de lijst waaruit je hem kon kiezen, en het document sprak zichzelf tegen.

`e187015e` repareert dat. Opnieuw gemeten op het draaiende cluster, met een controle
die per veld de `default` tegen de `x-choices` legt:

```
velden met x-choices: 12   OK: 12   PROBLEEM: 0

default-language     default='nl'
template             default='sso-only'
storage              default='1Gi'      <- was 10Gi
sleep-after-deploy   default='48h'
sleep-after-wake     default='1h'
wake-mode            default='auto'
```

Elke standaardwaarde staat nu in zijn eigen keuzelijst. De commit zet er bovendien een
test op, zodat het niet stilletjes terug kan komen.
