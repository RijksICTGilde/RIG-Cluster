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

(volgt)
