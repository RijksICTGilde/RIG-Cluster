# AppProjects: wat een Application mag uitrollen

Een AppProject in ArgoCD begrenst per Application uit welke repo's hij mag lezen, naar welke
bestemmingen hij mag schrijven en welke resource-soorten hij mag aanmaken. Op dit platform
heeft elke Application er een, en `default` laat niets toe.

## Waarom

ArgoCD maakt het AppProject `default` zelf aan zodra het ontbreekt, met `sourceRepos: ['*']`,
alle bestemmingen en alle resource-soorten. De drie handgeschreven platform-Applications
stonden daarop, dus naast de begrensde projecten die OPI per project schrijft stond er een
open deur: een Application op `default` kon uit elke repo naar elke namespace uitrollen.

BIO2-control 8.32 (wijzigingsbeheer) en 8.02 (speciale toegangsrechten) vragen dat wat uit git
uitrolt alleen kan wat het hoort te kunnen. Dit is defense in depth naast de controles die OPI
zelf doet: ook een manifest dat buiten OPI om in een van de repo's belandt, komt niet verder
dan wat zijn AppProject toelaat.

## De vier AppProjects

| AppProject | Applications | Bron | Bestemming |
|---|---|---|---|
| `default` | geen | niets | niets |
| `zad-app-of-apps` | `user-applications` | de argo-repo | de ArgoCD-namespace |
| `zad-platform` | `production-infrastructure` (staging: `staging-infrastructure`, sandbox: `sandbox-infrastructure`), `ron-infrastructure`, en op staging ook `infrastructure-secrets` en `ron-secrets` | deze repo (staging: plus de private repo `zad-staging-secrets`; sandbox: de infrastructuur-repo in Forgejo) | de operations- en de ron-namespace |
| `{project}-{namespace}` | de Applications van een project | de repository's van dat project | de namespace van dat project |

De eerste drie staan in `bootstrap/rig-system/kustomize/`: `default` in `appprojects/` (die map
hoort bij alle drie de overlays), de andere twee per overlay. Elke overlay draait een eigen
ArgoCD in een eigen namespace, dus de AppProjects van odcn-production, odcn-staging en
sandboxed-local staan naast elkaar en delen niets. Het vierde soort genereert OPI per
project uit `operations-manager/python/manifests/argocd-appproject.yaml.jinja`.

## Hoe je ermee werkt

**Een nieuwe handgeschreven Application** krijgt een `spec.project` dat niet `default` is. Zet
hem op `zad-platform` als hij uit deze repo naar een platformnamespace uitrolt; hoort hij
ergens anders, geef hem dan een eigen AppProject. `tests/test_argocd_handwritten_appprojects.py`
is rood zolang er een op `default` staat, en ook bij een `'*'` in `sourceRepos` of
`destinations`.

**Een infrastructuur-component met een nieuwe resource-soort** valt buiten
`namespaceResourceWhitelist` van `zad-platform` en laat de sync daarop vallen met een melding
die de soort noemt. Die soort hoort er dan bij, in de overlay van het cluster waar hij
rendert. De lijsten verschillen bewust: ODCN (productie en staging) heeft external-dns
(ServiceAccount, Role, RoleBinding) en een cert-manager Issuer, de sandbox heeft forgejo
(StatefulSet). Meet de lijst, raad hem niet:

```bash
kustomize build --enable-alpha-plugins --enable-exec --load-restrictor LoadRestrictionsNone \
  infrastructure/bootstrap/clusters/odcn | yq e '[.apiVersion,.kind]|join("/")' - | sort -u
```

`clusterResourceWhitelist` blijft leeg: de ArgoCD op ODCN draait namespaced en geen van de drie
clusters rendert iets cluster-scoped. Een leeg veld weigert, in beide whitelists; alleen een
AFWEZIG `namespaceResourceWhitelist` laat alles toe (`IsGroupKindNamePermitted` in ArgoCD
v3.5.1: nil is toestaan, lengte 0 is geen match). Daarom staat het op `default` met `[]` en
niet helemaal niet.

**Een repo-URL in `sourceRepos`** staat er letterlijk zoals de Application hem noemt, userinfo
incluis (`https://user-applications@github.com/...`). ArgoCD vergelijkt genormaliseerde URL's,
maar normaliseren laat de userinfo staan; een URL zonder die userinfo matcht dus niet. Voor de
projecten gebeurt dat vanzelf: `argo_manager` haalt beide door
`make_argocd_repository_url_unique`.

## Uitrol

De drie handgeschreven AppProjects gaan mee met de bootstrap-overlay, die met
`task bootstrap-argo-system` wordt toegepast: een handmatige stap, niet via ArgoCD. Het
AppProject per project landt bij de eerstvolgende verwerking van dat project; tot die tijd
houdt het bestaande bestand in de argo-repo zijn `sourceRepos: ['*']`.

### In twee stappen, op een cluster dat al draait

Een cluster waar de Applications nog op `default` staan, krijgt deze wijziging in twee
applies. Als `default` en de omschakeling in één apply gebeuren, mogen Applications die nog
op `default` staan niets. Daarom gaat `default` als laatste dicht.

Bewaar eerst de huidige toestand als terugvaloptie, met de namespace van het cluster
(`rig-prd-operations` op productie, `rig-staging-operations` op staging):

```bash
kubectl get applications,appprojects -n rig-staging-operations -o yaml > terugval.yaml
```

Stap 1, de nieuwe AppProjects en de omgezette Applications, met `default` nog zoals hij in
het cluster staat en met prune uit:

```bash
task bootstrap-argo-system CLUSTER_TYPE=odcn-staging KEEP_DEFAULT_APPPROJECT=true PAUSE_PRUNE=true
```

`KEEP_DEFAULT_APPPROJECT=true` laat alleen het AppProject `default` uit de gerenderde
overlay weg; de rest gaat mee. `PAUSE_PRUNE=true` zet `prune: false` op de Applications uit
de overlay. Rendert een app-of-apps in dit venster maar half, dan haalt ArgoCD daardoor geen
child-Application weg, en via de finalizer dus ook niet de workloads eronder. Met de hand
uitzetten vóór de taak helpt niet: de apply zet `prune: true` uit git meteen terug.
Controleer daarna dat elke Application Synced en Healthy is, en dat geen enkele een
`ComparisonError` over zijn project meldt:

```bash
kubectl get applications -n rig-staging-operations \
  -o custom-columns=NAAM:.metadata.name,PROJECT:.spec.project,SYNC:.status.sync.status,HEALTH:.status.health.status
```

Stap 2, `default` dichtzetten, pas als stap 1 groen is. Prune blijft nog uit:

```bash
task bootstrap-argo-system CLUSTER_TYPE=odcn-staging PAUSE_PRUNE=true
```

Stap 3, prune weer aan, als ook stap 2 groen is:

```bash
task bootstrap-argo-system CLUSTER_TYPE=odcn-staging
```

Op productie is het dezelfde volgorde met `CLUSTER_TYPE=odcn-production` en
`rig-prd-operations`. Op een cluster dat van nul af wordt opgebouwd is de laatste stap genoeg:
daar is nog geen Application die op `default` staat.

## Hangt samen met

- `features/futures/argocd-applicationset-migration.md`: dat ontwerp geeft de AppProjects een
  eigen Application, want een ApplicationSet kan ze niet genereren.
- `features/ondertekende-gitops-commits.md`: `signatureKeys` op `zad-app-of-apps` en op de
  AppProjects per project, bedoeld om een AppProject dat buiten OPI om in de argo-repo
  belandt zichzelf niet ruim te laten zetten. Die eis staat op de AppProjects, maar de
  ArgoCD van dit cluster dwingt hem nog niet af: zie de status in dat document.
