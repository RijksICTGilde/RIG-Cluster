# OOM-detectie tijdens runtime via een pod-watch

## Aanleiding

Incident van 2026-09-29: deployment `pr1644` (project `regel-k4c`) crashloopede een half
uur op OOMKilled terwijl niemand het doorhad; gebruikers zagen 503's. De bestaande
detectie (`oom_watcher.check_pod_health`) loopt alleen in een kort venster ná een
rollout (`schedule_oom_check`, drie pogingen). Een OOM op een pod die al dagen draait
wordt nu alleen toevallig opgepikt door de nachtelijke auto-tune via VPA-targets.

## Kernfeit dat het ontwerp stuurt

Kubernetes kent géén Event-object voor een OOM-kill. De informatie zit in de
pod-status: `containerStatuses[].lastState.terminated` met `reason = OOMKilled`,
`exitCode = 137`, `startedAt`, `finishedAt`. Dat hebben we 29 september zelf
bevestigd: vijf OOM-kills, nul events. Alle gangbare oplossingen (kwatch, Robusta,
de OOM-observer van de VPA-recommender) lezen daarom de pod-status; alleen
kube-state-metrics maakt er een metric van
(`kube_pod_container_status_last_terminated_reason`), en die gebruiken we als vangnet.

## Doel

Een OOM-kill op álle beheerde componenten, op elk moment, leidt binnen minuten tot
dezelfde automatische verhoging van de memory-limit die de rollout-watcher nu alleen
bij deploys toepast, met dezelfde veiligheidsremmen.

## Ontwerp

### 1. Pod-watcher in OPI (het eigenlijke werk)

Eén async background taak, naast de bestaande jobs. Geen polling: list-watch.

- Eénmalige volle LIST bij opstarten om de initiële `restartCount`-stand per
  pod+container in geheugen op te slaan.
- Daarna een WATCH-stream op pods cluster-wide, client-side gefilterd op de
  namespaces die deze OPI beheert (patroon uit de cluster-config, `rig-prd-*`
  op productie, `rig-*` op sandbox; let op uitzondering `rig-jongo-*`).
- Trigger: een MODIFIED-pod waarvan een container `restartCount` is gestegen
  ten opzichte van de cache. Alleen dán kijken we naar
  `lastState.terminated.reason`.
- Alleen `reason == OOMKilled` gaat het tune-pad in. Andere redenen (Error,
  Completed) negeren: crashloops en image-fouten hebben al eigen paden
  (sanitize / deployment-health) en willen we hier niet dubbel oppakken.

Kostenargument: de stream kost nul requests bij stilte en één pod-object per
wijziging; de lijst wordt alleen bij OPI-start en bij een verbroken stream
opnieuw opgehaald. Lineair in wijzigingen, niet in pods.

### 2. Reconnect en hervatten

- Watch met `resourceVersion` en bookmarks; bij verbreken: backoff, relist via
  de laatste resourceVersion, cache verversen.
- Dedupe op `finishedAt` (de kill zelf is uniek), gekoppeld aan de bestaande
  `oom_is_fresh_evidence`-hash, zodat een OPI-herstart tijdens een crashloop
  niet opnieuw tuned op een kill die al verwerkt is.
- De bestaande budgetrem blijft: `oom_tune_budget_spent` (max 3 tune-cycli per
  ronde) voorkomt dat een eindeloos OOMende pod onbeperkt omhoog geschroeft
  wordt.

### 3. Van pod naar component

De tune werkt per component in het projectbestand. De watcher moet van pod naar
`project/deployment/component` komen. Eerste keus: labels op onze gegenereerde
manifests (te verifiëren welke er al staan; `_get_current_pod_template_hash`
en de sanitize-matching via `unique_name`-prefix zijn de bestaande voorbeelden).
Pods zonder match (bijv. helmfile-deployments zoals grist) slaan we over in de
eerste versie: log-regel, geen actie.

### 4. Vangnet via Mimir (platform-onafhankelijk uitvoerbaar)

Periodiek (bijv. elk uur) één query op Mimir via de bestaande
`grafana_prometheus`-connector:

```
kube_pod_container_status_last_terminated_reason{reason="OOMKilled", namespace=~"rig-prd.*"}
```

Alles daar dat de watcher niet gezien heeft (downtime van de watch, OPI-deploy)
gaat alsnog het tune-pad in, via dezelfde `finishedAt`-dedupe. Deze stap is
afhankelijk van ODCN-metrics en staat daarom achter dezelfde cluster-config als
de bestaande metrics-integratie; op clusters zonder Mimir valt hij stilletjes weg.
Let op: de metric is EXPERIMENTAL in kube-state-metrics en per metric versie
beschikbaar; het vangnet mag nooit de enige detectie zijn, de watch blijft primair.

### 5. Configuratie en rollout

- Feature-flag in de cluster-config: watch aan op odcn-production, optioneel op
  sandboxed-local om te testen. Standaard uit tot de uitwerking bewezen is.
- RBAC: de OPI-serviceaccount heeft list/watch op pods nodig in alle beheerde
  namespaces. Verifiëren of de huidige rol dat al dekt (de kubectl-connector kan
  al pods lezen; een watch gebruikt dezelfde rechten).

## Bewust buiten scope

- Notificaties naar projecteigenaren (eigen feature, mogelijk later).
- Automatisch tunen van helmfile-deployments (grist): hun manifests komen niet
  van OPI, dus daar kan de tune niets schrijven. Detectie + melding kan later.
- `node-problem-detector` en Alertmanager-rules van ODCN: platformafhankelijk,
  geen tenant-pad.

## Openstaande keuzes voor de uitwerking

1. Waar de watcher leeft: `opi/services/oom_watcher.py` uitbreiden of losse job
   in `opi/jobs/`. Voorkeur nu: dezelfde module, losse loop-taak, zodat dedupe
   en budget-code gedeeld blijft.
2. Welke HTTP-laag de watch draait: de kubectl-connector is subprocess-gebaseerd
   en niet gebouwd voor streaming. De directe k8s-API met het in-cluster token
   (aiohttp is al een dependency) is waarschijnlijk betrouwbaarder dan
   `kubectl get pods -w -o json` parsen.
3. Welke labels nu daadwerkelijk op component-pods staan en of die volstaan voor
   de mapping, of dat de manifest-templates een label moeten krijgen.

## Uitvoeringsvolgorde met verificatie

1. [ ] RBAC en labels verifiëren → check: sandbox, `kubectl auth can-i watch pods --as=system:serviceaccount:rig-system:operations-manager -n rig-<project>`
2. [ ] Watch-loop met cache, trigger op `restartCount`-stijging, reason-filter → check: unittest met opgeslagen pod-objecten van het pr1644-incident (OOMKilled) en een normale herstart (Completed) die genegeerd moet worden
3. [ ] Koppeling aan de bestaande tune (`oom_is_fresh_evidence`, budget) → check: sandbox, pod met lage limit aanmelden, binnen enkele minuten staat er een bijgewerkte `limits.memory` met history-entry van source `oom-watcher` in het projectbestand
4. [ ] Reconnect-gedrag → check: watch-stream in sandbox geforceerd verbreken (404 Gone forceren), watcher hervat zonder dubbele tunes
5. [ ] Mimir-vangnet achter config → check: query levert op productie precies de pr1644-kill van 29 september (reproduceerbaar met tijdvenster), sandbox zonder Mimir logt "uitgeschakeld"
6. [ ] Feature-flag aan op odcn-production → check: een kunstmatige OOM op een testproject wordt binnen vijf minuten getuned

## Risico's

| Risico | Mitigatie |
|---|---|
| OPI draait ooit met replicas > 1, twee watchers tunen tegelijk | `finishedAt`-dedupe + budgetrem; bij invoering van HA alsnog leader-election toevoegen |
| Watch verwijderd door API-server (410 Gone) tijdens lange stilte | bookmarks + relist-pad uit stap 2 |
| Een flappende pod produceert elke paar minuten een OOM | bestaande budgetrem (3 per ronde) plus dedupe; daarna meldt de watcher alleen nog |
| kube-state-metrics wijzigt de EXPERIMENTAL metric | vangnet faalt dan stil; acceptabel, watch is primair |
