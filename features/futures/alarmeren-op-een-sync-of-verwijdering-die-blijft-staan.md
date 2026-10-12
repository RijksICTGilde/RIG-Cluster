# Alarmeren op een sync of een verwijdering die blijft staan

Status: ontwerp, niets gebouwd. Opgeschreven op 5 oktober 2026 naast RC-250, en gemeten op
het sandboxcluster (ArgoCD `v3.5.1-rig1`, argocd-operator).

## Waarom dit er ligt

Twee storingen met dezelfde vorm, en beide keren was niet het probleem het probleem maar de
tijd die eroverheen ging voordat iemand het zag.

* Een sync-operation die 45 minuten `Running` bleef en elke volgende sync op die
  Application tegenhield. Zichtbaar geworden doordat de klant het meldde
  (`plans/een-sync-die-niet-afbreekt-blokkeert-elke-volgende.md`).
* Een Application met een `deletionTimestamp` die veertien dagen bleef hangen en 350
  resources liet liggen, waaronder 140 secrets
  (`features/argocd-vastgelopen-verwijdering.md`).

RC-250 heeft daar de reparaties voor gebouwd: een bovengrens op de controller
(`--sync-timeout=600`) en OPI die geen vastgelopen operation meer achterlaat. Allebei
werken ze op de Application waar iemand op dat moment iets mee doet. Wat ze niet dekken is
een Application waar niemand naar kijkt. Het delete-plan noemt dat al als buiten-scope
onder het bredere gat in servicemonitoring; dit document maakt er een eigen taak van in
plaats van het stil mee te nemen.

## Wat er gemeten is

### De metriek bestaat, de scrape niet

`argocd_app_info` draagt een `operation`-label, en dat is leeg zodra er geen operation
loopt. Gemeten op de sandbox terwijl een sync van RC-250 vastzat:

```
argocd_app_info{name="rc250-proef", ..., health_status="Degraded", operation="sync",
                sync_status="OutOfSync"} 1
argocd_app_info{name="jc-77j-poc",  ..., health_status="Healthy",  operation="",
                sync_status="Synced"} 1
```

Maar die metriek wordt nergens opgehaald:

| gemeten | uitkomst |
|---|---|
| `kubectl get servicemonitors -A` | `No resources found` |
| actieve Prometheus-jobs op de sandbox | `prometheus, kubernetes-cadvisor, kubernetes-kubelet, kubernetes-pods, kube-state-metrics, cloudnative-pg, minio, keycloak-rig-metrics`; geen argocd |
| `job_name` in `infrastructure/.../prometheus/controller/base/configmap.yaml` en in de `odcn`-patch | dezelfde lijst, geen argocd |
| `spec.monitoring.enabled` en `spec.prometheus.enabled` in beide ArgoCD-CR's | `false` |

De services zijn er wel (`argocd-metrics:8082`, `argocd-server-metrics:8083`), dus de
eerste stap is een scrape en niet een metriek.

### De verwijdering is er niet uit te lezen

`argocd_app_info` zegt niets over een `deletionTimestamp`. Daar zijn twee wegen voor, en de
keuze hoort bij deze taak:

* `kube-state-metrics` met `customResourceStateMetrics` voor `argoproj.io/Application`.
  kube-state-metrics draait al en wordt al gescrapet, maar die configuratie bestaat nog
  niet en is een eigen stuk werk.
* OPI exposeert het zelf. OPI leest de Applications al via
  `KubectlConnector.list_argocd_applications` en heeft al Prometheus-metrieken en een
  `PrometheusRule` op odcn-production
  (`bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/prometheusrule-billing.yaml`).

De tweede weg is kleiner en legt de kennis waar hij al zit; de eerste is generieker en
dekt ook Applications die OPI niet kent.

### Waar de regel zou landen

Op odcn-production evalueert de OpenShift-monitoring onze `PrometheusRule`-objecten; de
billing-regel hierboven is er het bewijs van. Daar hoort een alarmregel dus als
`PrometheusRule` naast die ene. Op de sandbox en `local` draait een eigen Prometheus met
statische scrape-configs en geen operator, dus daar is het een `job_name` in diezelfde
ConfigMap en geen `PrometheusRule`. De twee omgevingen vragen dus twee vormen, en dat is
het grootste deel van het werk.

## Wat er zou moeten komen

Twee regels, beide met een drempel van een kwartier. Een kwartier omdat een gezonde uitrol
bij ons binnen tien minuten klaar hoort te zijn (dat is waarom `--sync-timeout` op 600
staat), dus wat na een kwartier nog loopt is langs het vangnet gekomen.

1. **Een operation die een kwartier `Running` staat.** Met de scrape erbij is dit
   uitdrukbaar:

   ```promql
   max by (name, namespace, dest_namespace) (argocd_app_info{operation!=""})
   ```

   met `for: 15m`. De aggregatie is niet cosmetisch: `argocd_app_info` draagt
   `health_status` en `sync_status` als label, en die wisselen tijdens een vastgelopen sync
   (gemeten: `Progressing` naar `Degraded` en terug). Elke wissel is een nieuwe tijdreeks,
   en een kale selector met `for: 15m` zou daarop zijn teller opnieuw beginnen en nooit
   vuren. Dit is dezelfde valkuil die RC-237 al tegenkwam bij een kale selector naast een
   `max_over_time`-zuster.

2. **Een Application met een `deletionTimestamp` ouder dan een kwartier.** Vraagt eerst de
   keuze uit "De verwijdering is er niet uit te lezen" hierboven.

## Wat er NIET in hoort

Een alarm op `health_status="Degraded"` of op `sync_status="OutOfSync"`. Dat is de gewone
toestand van een tenant die zijn eigen applicatie stuk heeft, en die hoort bij de tenant en
niet bij een platformalarm. Deze twee regels gaan over iets anders: een ZAD-mechanisme dat
niet afrondt, en dat is onze storing en niet de hunne.
