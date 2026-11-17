# De allowlist van resource-soorten, en wat een tenant ziet van een type dat erbuiten valt

ArgoCD watcht op elk cluster alleen de resource-soorten die op een allowlist staan
(`spec.resourceInclusions` in de ArgoCD-CR). Een kind dat er niet op staat, rolt ArgoCD niet
uit: hij laat het object uit de gewenste toestand weg en zet een conditie op de Application.
Dit document legt uit waarom de lijst er is, waar hij staat, wat een tenant ziet als zijn
chart een ander kind bevat, en hoe een kind erbij komt.

## Waarom

De ArgoCD-controller draait namespace-scoped en watcht dan elk API-type in elke namespace
apart. Op odcn-production waren dat 589 types in 68 namespaces, en de client-limiter van de
controller (QPS 50) stond daardoor blijvend verzadigd: elke nieuwe watch moest wachten op een
plek in de wachtrij. Een nieuw project (hw-e7x, 17 november 2026) bleef zo 19 minuten
onzichtbaar voor ArgoCD, terwijl OPI na 240 seconden opgaf.

Met de allowlist watcht de controller niet 589 types maar enkele tientallen, en valt dat
verzadigingspunt weg. De prijs is het onderwerp van de rest van dit document: een kind dat
niet op de lijst staat, rolt niet uit, en dat is zichtbaar in plaats van stil.

## Waar de lijst staat

In `spec.resourceInclusions` van de ArgoCD-CR van elke overlay, want elke overlay draait zijn
eigen ArgoCD:

- `bootstrap/rig-system/kustomize/overlays/odcn-production/argocd-deployment.yaml`
- `bootstrap/rig-system/kustomize/overlays/odcn-staging/argocd-deployment.yaml`
- `bootstrap/rig-system/kustomize/overlays/sandboxed-local/argocd-deployment.yaml`

Die drie bestanden zijn de enige waarheid over welke kinds zijn toegestaan; lees ze daar, en
niet hier. De lijst (35 kinds bij invoering) dekt wat ArgoCD voor het platform uitrolt: de
soorten die de manifest-sjablonen in `operations-manager/python/manifests/` genereren (onder
andere Deployment, Service, Ingress, ConfigMap, Secret, ServiceAccount, Role, RoleBinding,
NetworkPolicy, PersistentVolumeClaim, Pod, Job, VerticalPodAutoscaler, Issuer, de
CNPG-Cluster, en ArgoCD's eigen Application en AppProject) plus de soorten die de
infrastructuur-Applications nodig hebben. Een soort die OPI zelf met kubectl toepast en niet
via GitOps, zoals de Namespace en de VolumeSnapshot van een backup, hoeft er niet op: de
allowlist begrenst alleen wat ArgoCD reconcilieert.

De allowlist zelf komt uit een eigen wijziging (branch `claude/argocd-resource-inclusions`) en
staat los van de zichtbaarheid die hier beschreven is: die werkt ook als ArgoCD om een andere
reden een resource uitsluit.

## Wat een tenant ziet van een kind dat erbuiten valt

ArgoCD haalt zo'n object uit de gewenste toestand en zet een conditie op de Application
(`controller/state.go`, argo-cd v3.5.6):

```
type: ExcludedResourceWarning
message: Resource gateway.networking.k8s.io/HTTPRoute web is excluded in the settings
```

De Application blijft daarbij `Synced` en `Healthy`: voor ArgoCD is er niets mis, het object
hoort gewoon niet bij de gewenste toestand. Zonder extra werk leest dat als een geslaagde
uitrol waarin stilletjes iets ontbreekt. Daarom:

1. **Het deploymentpaneel** toont boven de foutenlijst een waarschuwing per uitgesloten
   resource: "HTTPRoute web wordt niet uitgerold: dit type staat niet op de lijst die het
   platform ondersteunt." Een waarschuwing en geen fout, want de rest van de app draait wel.
2. **De projectpagina** zet in de statuskolom van die deployment een extra label
   ("1 resource niet uitgerold") naast Healthy en Synced, zodat de rij niet kaal groen is.
3. **De API v2** levert ze gestructureerd in `warnings` op een deployment: `type`, `severity`,
   `category`, `group`, `kind`, `name`, de Nederlandse `message` en de ruwe `raw_message` van
   ArgoCD. Gevuld bij elke status, ook een gezonde.
4. **De taak** (aanmaken, bijwerken) eindigt de subtaak van die deployment met "uitgerold,
   maar 1 resource niet (HTTPRoute web)" in plaats van "uitgerold en gezond". De taak zelf
   slaagt: een mislukte taak zou de rest van de uitrol blokkeren terwijl die wel werkt.
5. **De log** van OPI krijgt de conditie bij elke refresh van de Application, op
   warning-niveau. Alleen type en melding van ArgoCD, dus kind en naam, nooit inhoud uit een
   manifest.

De conditie wordt herkend op het TYPE van de conditie, niet op de tekst. Uit de melding komen
group, kind en naam; een onverwacht formaat valt terug op de ruwe tekst van ArgoCD.

## Een kind toevoegen

1. Zet het kind in `spec.resourceInclusions` van alle drie de CR's hierboven. Alle drie
   tegelijk, anders rolt iets wel uit op de sandbox en niet op productie, en dat verschil
   ontdek je pas bij de release.
2. Pas de CR toe (de normale weg uit `docs/operations/uitrollen.md`). De controller herstart
   daarop en begint de nieuwe soort te watchen.
3. Herverwerk het project waar het om gaat, of wacht op de volgende refresh: de conditie
   verdwijnt en het object rolt mee.

Weeg bij het toevoegen mee wat de meting hierboven zegt: elk type kost een watch per
namespace, dus een kind dat één project gebruikt kost een watch in alle namespaces.

## Bestanden

| Bestand | Rol |
|---|---|
| `bootstrap/rig-system/kustomize/overlays/*/argocd-deployment.yaml` | de allowlist zelf, per cluster |
| `opi/services/deployment_diagnostics.py` | `gather_excluded_resources`, de conditie herkennen en vertalen |
| `opi/web/router.py` | `excluded_resources` op de status van het paneel |
| `opi/templates_lotc/bg/_argocd-deployment-card.html.j2` | de waarschuwing in het paneel |
| `opi/services/argocd_overview.py` + `opi/http/render/lotc_switch.py` | het label in de statuskolom |
| `opi/api/v2/models.py` + `opi/api/v2/router.py` | `warnings` op een deployment |
| `opi/manager/project/rollout.py` | de melding van de subtaak |
| `opi/connectors/argo.py` | de conditie in de log bij een refresh |

Zie `features/argocd-render-error-surfacing.md` voor het mechanisme waar dit op meelift: de
andere condities die ArgoCD op een Application zet, en hoe die in het paneel terechtkomen.
