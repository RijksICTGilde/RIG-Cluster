# Rollout-metingen: duur per stap, fases en de bank

**Status**: Implemented
**Date**: 2026-11-04
**Related**: [Eén voortgangsweergave](task-progress-view.md), [Async Task System](async-task-system.md),
[ArgoCD Refresh Performance](argocd-refresh-performance.md), [Wachten op de ArgoCD-sync](argocd-sync-wait.md)

## Wat het is

Een ZAD-taak met rollout duurde p50 30 tot 45 seconden terwijl de ArgoCD-sync erin een paar
seconden kost. Dat verschil was niet te zien: de taakvoortgang noemde de stappen zonder
duur, en er was geen metriek per fase. Deze meting maakt het zichtbaar, zodat elke
versnelling eraan is af te lezen en een wijziging die niets oplevert opvalt.

Drie dingen horen bij elkaar:

1. **Duur per stap** in de taakvoortgang.
2. **Een Prometheus-histogram per fase** van een rollout, met het taaktype als label.
3. **Een bank** die een vast stel scenario's op de sandbox meet en als tabel meldt.

## Duur per stap

Elke stap in de voortgang legt zijn begin- en eindtijd vast
(`opi/core/persistent_task_progress.py`, in de `subtasks`-kolom van de taak, dus zonder
migratie). De voortgangsweergave zet de duur op de tweede regel van de stap, naast het
onderwerp en de eventuele fout (`progress_step_detail` in
`opi/templates_lotc/widgets/_macros.html.j2`). Een stap die nog loopt heeft geen duur, en
een stap van voor deze wijziging ook niet: die mist de tijdstempels.

De opmaak staat in `format_step_duration` (`opi/core/template_helpers.py`), beschikbaar als
het Jinja-filter `stapduur`. Onder een minuut met een decimaal ("12,5 s"), daarboven in
minuten ("3 min 5 s").

## Het fase-histogram

`opi/core/metrics.py` houdt `opi_rollout_phase_seconds` bij, met labels `task_type` en
`phase`. De fases:

| fase | wat erin zit |
|---|---|
| `git_prepare` | het klaarzetten van een working copy (clone of worktree uit de warme cache) |
| `generate` | de manifests genereren en wegschrijven |
| `provision` | database, opslag, Keycloak, Redis, mail per deployment |
| `git_push` | de push naar een repo, inclusief rebase-pogingen |
| `argo_project_gate` | wachten tot `<project>-project` gesynchroniseerd is |
| `argo_umbrella` | het verversen van `user-applications` tot onze commit gezien is |
| `argo_wait_synced` | van het begin van de wacht tot de applicatie `Synced` meldt |
| `argo_wait_healthy` | van `Synced` tot `Healthy`: dit is de pod, niet ZAD |
| `after_sync` | de waarneming na de sync (health, OOM, image-pull) |

Daarnaast telt `opi_argocd_umbrella_refresh_lost_total` hoe vaak een refresh van
`user-applications` opging in een reconcile die zijn revisie al had opgehaald. Dat is de
situatie uit [argocd-refresh-performance](argocd-refresh-performance.md), en de teller zegt
of die nog voorkomt.

Het label `task_type` komt van de taakworker (`set_rollout_task_type`). Een pad dat buiten
een taak loopt (een scheduler, de CLI) meet onder `unknown`.

**De tijd die ZAD zelf toevoegt** is de taakduur min `argo_wait_synced` plus
`argo_wait_healthy`. Dat is het getal waar een versnelling op gemeten wordt: de pod-start
en de sync zijn een ondergrens die hier niet aan de orde is.

## De bank

```bash
eval "$(sandbox-gegevens --env)"
export ZAD_METRICS_TOKEN=$(kubectl -n rig-system get secret operations-manager-secrets \
  -o jsonpath='{.data.PROMETHEUS_METRICS_AUTH_TOKEN}' | base64 -d)

PROJECT=mijnproject LABEL=voor JSON=/tmp/bench-voor.json task bench-rollout
# wijziging uitrollen
PROJECT=mijnproject LABEL=na   JSON=/tmp/bench-na.json   task bench-rollout
```

De bank (`operations-manager/python/scripts/bench_rollout.py`) drijft een BESTAAND
sandboxproject via de API en meet per scenario:

| scenario | handeling |
|---|---|
| `a1` | een nieuwe deployment in een project met 1 bestaande deployment |
| `a5` | dezelfde handeling bij 5 bestaande deployments |
| `b` | een image-update op een bestaande deployment |
| `c` | `configure_service` met een deployment in de payload |

Scenario `c` schrijft de bestaande config van een dienst terug. Dan verandert er niets aan
wat er uitrolt en meet de gang precies wat de taak eromheen kost.

De bank kiest haar image niet zelf: ze leest het image dat het project al draait, want dat
staat al op de node en een image dat de sandbox niet kan pullen zou elk scenario op een
ImagePullBackOff laten stranden. Scenario `b` gaat naar datzelfde image met zijn digest erbij:
een andere verwijzing, dezelfde bytes, dus de hele uitrol loopt terwijl de pod uit de cache
start. Met `--alt-image` geef je zelf een ander image mee.

De deployments die de bank aanmaakt ruimt ze weer op, tenzij `KEEP=1`. Dat laatste is
handig voor een tweede gang: dan meet die op dezelfde deployments.

### Wat de bank niet meet

Scenario `d` uit het onderzoeksplan (een nieuw project) staat er niet in.
`POST /api/v2/projects` vraagt een SSO-bearertoken en de sandbox heeft geen SSO-Rijk, dus
een project aanmaken gaat daar via de wizard in een browser. Die meting hoort bij een mens.

De bank meet op een cluster dat ze met niemand deelt in de tijd dat ze loopt: een andere
taak van hetzelfde taaktype zou de histogramdiff vervuilen.

## Afhankelijkheden

- Een draaiende sandbox met de branch erop (`sandbox-deploy`).
- `ZAD_SECRET_KEY` of `E2E_SECRET_KEY` van het cluster, voor de beheerderssessie waarmee de
  bank een projecttoken uitgeeft (dezelfde weg als `scripts/sandbox_project_tool.py`).
- `ZAD_METRICS_TOKEN`: zonder dat token antwoordt `/metrics` met een 401.
