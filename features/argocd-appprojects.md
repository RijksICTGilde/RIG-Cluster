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

## Hangt samen met

- `features/futures/argocd-applicationset-migration.md`: dat ontwerp geeft de AppProjects een
  eigen Application, want een ApplicationSet kan ze niet genereren.
- De plannen over ondertekende commits: `signatureKeys` op deze AppProjects is daar het werk.
  Zonder dat kan een AppProject dat via de argo-repo binnenkomt zichzelf nog ruim zetten.
