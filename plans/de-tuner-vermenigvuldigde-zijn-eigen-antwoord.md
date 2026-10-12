# De tuner vermenigvuldigde zijn eigen antwoord

Op 6 oktober 2026 stond in de productielog van OPI dit, en het leek een registry-storing:

```
ERR project_manager: Runtime pod-health issue(s) after sync: asses-k2n-redirect:
de registry kon het image voor component 'redirect-redirect' niet leveren
```

Het was geen storing. De image-tag was bij de bron opgeruimd, en de reden dat die pull er ooit kwam is dat de resource-tuner elke nacht een nieuwe pod uitrolde voor een wijziging van 1Mi. Bij het natrekken daarvan kwam een tweede, grotere zaak boven: bij toets-hn7 ging dezelfde tuner in 42 seconden van 25Mi naar zijn plafond van 200Mi, zonder dat er ooit iets gemeten is.

Dit plan dekt beide, plus twee lege foutmeldingen en twee bronnen van logruis die in hetzelfde onderzoek naar boven kwamen.

## Wat al geschreven staat

Op `claude/log-triage-fixes` (forgejo) staan twee commits die hier niet opnieuw bedacht hoeven te worden:

- `153f686a3` implementeert de punten 1 tot 4 hieronder, met tests, en is groen op ruff, pyright, 1030 tests in `-k "kopia or backup or postgres or argo"` en de 142 van de tuner-suite.
- `9d1d1ec5c` bevat punt 6, plus een **rode** test die punt 5 reproduceert.

Cherry-picken mag. Opnieuw doen mag ook, als het beter kan. De handgemaakte PR 219 die erbij hoorde gaat dicht ten gunste van deze taak.

## 1. Een timeout naar ArgoCD logde niets

Twee keer stond dit in de log, en er stond niets achter:

```
opi.connectors.argo - ERROR - Error getting application status:
```

`asyncio.TimeoutError` heeft een lege `str()`, en de brede vangst in `get_application_status` interpoleerde de uitzondering kaal. Juist de meest voorkomende fout op dat pad logde dus precies niets. Het liet twee keer een hele taak van toets-hn7 sneuvelen en de log-watcher stuurde de lege regel door als alert.

Een helper die terugvalt op de klasse van de uitzondering als de tekst leeg is, toegepast op de acht brede vangsten in `opi/connectors/argo.py`. Uitdrukkelijk **niet** de repr: `test_argo_connect_retry.py` legt vast dat `Cannot connect to host argocd-server:80` leesbaar blijft, en de repr daarvan is een `ConnectionKey` vol velden. Die test moet groen blijven zonder aanpassing.

## 2. De deadband gold voor de verkeerde waarden

asses-k2n/productie, component `redirect`, had vier ReplicaSets met hetzelfde image en een geheugenwaarde die elke nacht 1Mi heen en weer schoof:

| aangemaakt | limit / request |
|---|---|
| 28-09 23:04 | 96Mi / 32Mi |
| 29-09 23:00 | 104Mi / 40Mi |
| 04-10 23:00 | 105Mi / 41Mi |
| 05-10 23:00 | terug naar 104Mi / 40Mi |

`min_delta_mi` is 16 en `passes_deviation_gate` werkt correct, dus de poort was het probleem niet. Het zit in de orde: de poort toetst de **recommendation**, maar de headroom-regel en het groeiplafond draaien daarna en draaien die beslissing bijna helemaal terug. De keten, met buffer 25%, limit_factor 1.5 en headroom 64Mi:

| stap | limit | request |
|---|---|---|
| huidig | 104Mi | 40Mi |
| recommendation (32.5Mi gemeten) | 49Mi | 41Mi |
| poort | -55Mi is 53%, DOOR | +1Mi, STOP |
| headroom: limit >= request + 64 | 105Mi | 41Mi |

De poort keurde een daling van 55Mi goed; wat geschreven werd is een stijging van 1Mi. Elke flip is een nieuw pod-template, dus een rollout en een image-pull, en daar liep asses-k2n op vast.

Een her-toets van de deadband op de **eindwaarden**, na headroom, plafond en gebruikerswens. Die her-toets staat **voor** het honoreren van de gebruikerswens: een handmatige pin mag wel een klein verschil zetten, dat is een keuze en geen drift. Voor een OOM geldt de deadband niet, daar net zo min als bij de poort.

## 3. argocd-server was te krap

```
restarts=4, lastState=OOMKilled exit=137 at=2026-10-05T08:44:44Z
limit 512Mi, request 128Mi, feitelijk gebruik 375Mi
```

De request lag een factor 3 onder het werkelijke gebruik en de limit liet geen ruimte voor een piek. Naar 1Gi met een request van 384Mi, in `bootstrap/rig-system/kustomize/overlays/odcn-production/argocd-deployment.yaml`. Dit is de enige wijziging in dit plan die via ArgoCD moet landen en niet in een OPI-release.

## 4. Twee bronnen van logruis

In zes uur log: 47 keer kopia en 69 keer postgres, samen bijna de helft van alle warnings.

Kopia waarschuwt een keer per project zonder backups tijdens de retention sweep van 06:00. De guard daarvoor bestond al, maar keek op `repository not found` en `no repository` terwijl Kopia `repository not initialized in the provided storage` zegt. Verbreden, en toepassen op alle vier de connect-plekken in plaats van alleen de eerste. Dit is dezelfde valkuil als `reference_fieldwarning_breekt_de_enforcer`: een guard die op woorden matcht die de werkelijkheid niet gebruikt.

Postgres waarschuwt dat een database, schema of user al bestaat. Dat is de normale uitkomst van een idempotente provisioning-ronde en de aanroeper handelt het af via `status: exists`. Naar DEBUG, de drie plekken in `opi/connectors/postgres.py`.

## 5. De kern: de OOM-bump vermenigvuldigde bewijs van een oudere pod

Dit is het grootste punt en het enige dat nog ontworpen moet worden.

toets-hn7/pr-103, component `frontend`, gedeclareerd op 25Mi met **limit gelijk aan request**, dus nul headroom. De app viel om bij het opstarten (`exit 137`, container `app`). Omdat een crashloopende pod nooit gescrapet wordt gaf Grafana 19 keer "no data" voor precies deze pods. Daarna, uit de log:

```
10:35:48  OOM: pr-103-frontend-6694c99686-dj56w container app, exit 137
10:35:51  tuner: baseline 25Mi   -> schrijft 139Mi    (task type=upsert_deployment)
10:36:29  OOM: pr-103-frontend-6694c99686-dj56w        (zelfde pod)
10:36:30  tuner: baseline 139Mi  -> 278Mi, afgetopt op 200Mi (task type=refresh_deployment)
```

De rekensom, met `oom_factor` 3.0 onder 64Mi en 2.0 onder 256Mi, en de headroom-marge van 64Mi erbovenop:

| ronde | baseline | factor | tussen | +headroom | geschreven |
|---|---|---|---|---|---|
| 1 | 25Mi (de huidige limit) | x3 | 75Mi | 139 | **139Mi** |
| 2 | 139Mi (wat ronde 1 schreef) | x2 | 278Mi | plafond | **136/200Mi** |

Dat `136/200` is wat production-frontend vandaag nog heeft, voor een app die 2Mi gebruikt: `200 = 8 x 25` en `136 = 200 - 64`. De enige rem die het stopte was het groeiplafond van 28 augustus.

**Waar het zit.** `opi/services/resource_analyzer.py`, in de OOM-tak van `compute_memory_recommendation`:

```python
oom_minimum = current_limit_mb * oom_factor
```

Dat is **per pod-generatie juist**: de pod is omgevallen bij de huidige limit, dus hij heeft meer nodig dan die limit. Het is fout zodra het bewijs van een oudere generatie komt, en dat was hier het geval: beide OOM-regels noemen dezelfde pod `6694c99686-dj56w`, die op het 25Mi-template liep. Ronde 2 vermenigvuldigde 139Mi op bewijs van een pod die bij 25Mi omviel.

**Waarom de rem niet werkte.** `oom_is_fresh_evidence` in `opi/services/oom_watcher.py` doet precies dit: één tune per pod-generatie. Hij wordt gelezen op drie plekken, alle drie in de OOM-watcher (regels 817, 1116, 1449). De after-sync hook die hier vuurde staat in `opi/manager/project_manager.py:3729` en heeft hem niet, en `deployment_observation.py` heeft ook geen eigen rem: geen hash, geen cooldown, geen budget. Erger nog, die hook zet via `requeue_refresh` zelf een refresh in de wachtrij, dus hij voedt zichzelf. Vandaar twee rondes in 42 seconden.

**Wat eraan ontbreekt.** `ComponentHealth` (`opi/services/catalog/base.py:383`) draagt drie booleans en geen pod-identiteit, dus de rem is op dit pad niet te voeden. Beide producenten zullen die mee moeten geven: `project_manager.py:3725` uit `ComponentFailure` (dat al een `container_name` heeft maar geen pod of hash) en `oom_watcher.py:709`, die de hashes al in handen heeft.

**Richting, als voorstel.** De rem hoort op de gedeelde deur, dus in de `AFTER_SYNC`-handler van de tuner (`opi/services/catalog/resource_tuning/__init__.py`), zodat beide producenten hem krijgen in plaats van dat elke aanroeper hem apart moet onthouden. Een veldnaam als `oom_pod_template_hash` op `ComponentHealth` is een voorstel, niet een afspraak. Wie het bouwt mag een betere vorm kiezen, zolang deze drie dingen gelden:

1. Een tune op OOM-bewijs gebeurt **maximaal een keer per pod-generatie**, op elk pad dat tunet, niet alleen op de watcher-paden.
2. Ontbreekt de pod-identiteit, dan blijft het gedrag zoals nu, namelijk tunen. Een ontbrekende hash mag geen stille blokkade worden; `oom_is_fresh_evidence` doet dit al zo en logt het.
3. `compute_memory_recommendation` blijft op `current_limit_mb` vermenigvuldigen. Dat is de juiste rekensom bij vers bewijs, en het is niet de plek van de bug.

**De rode test die erbij hoort** staat in `9d1d1ec5c` als `tests/test_tuner_oom_zonder_meting_escaleert_niet.py`. Hij voert twee opeenvolgende rondes uit met de productiegetallen en faalt nu met:

```
de tuner escaleert nog: ronde 1 gaf 139Mi, ronde 2 gaf 200Mi, en het plafond is 200Mi
```

Die test moet groen worden. Hij toetst op het plafond en niet op een exact getal, zodat hij de reparatie niet voorschrijft.

## 6. Een meting onder het clusterminimum is een meting

`min_memory_limit_mi` is 25 op elk cluster (`opi/core/cluster_config.py`), terwijl `min_observed_mi` 5.0 is. Die twee staan in de verkeerde verhouding: je kunt niets onder 25Mi zetten, dus elke meting onder 25Mi kan maar één antwoord opleveren. Onder de 5 viel hij juist in de tak die bij een OOM gaat vermenigvuldigen, en dat is de slechtste van de twee.

Een gemeten waarde boven 0 maar onder het clusterminimum wordt op dat minimum gezet. Alleen bij **nul** meetpunten blijft het "ik weet niets". Staat al in `9d1d1ec5c`.

Dit is niet de oorzaak van punt 5, want daar was er helemaal geen meting. Het haalt wel dezelfde valkuil weg voor de componenten die hem het hardst raken, en toets-hn7/frontend is er een van: 2Mi gemeten tegen een minimum van 25Mi.

## Wat er buiten valt

- **De verlopen image-tag van asses-k2n.** `ghcr.io/rijksictgilde/haproxy-redirect:2026.08.19` geeft upstream 404, de taglijst begint op 2026.08.21, en het canonieke pad is inmiddels `minbzk/base-images/haproxy-redirect`. Dat is een projectbestand en geen code. De eigenaar beslist.
- **Dat een 500 van de rcr-proxy in de portal landt als "de registry gaf geen antwoord, hier is niets voor je te doen".** Die tekst klopt voor een echte storing en niet voor een opgeruimde tag, maar het onderscheid is niet betrouwbaar uit de melding te halen. Apart uitzoeken.
- **De valse `ERR` op `kubectl patch application`.** `terminate_argocd_application_operation` vangt "missing path" en "does not apply", maar k8s 1.33 antwoordt met het generieke "The request is invalid". Gereproduceerd met `--dry-run=server`. Vraagt een merge patch (`--type merge -p '{"operation":null}'`), waarna het filter op foutteksten weg kan. Bewust apart: het raakt het delete-pad en niet de tuner.
- **Het gedeclareerde geheugen van toets-hn7/frontend.** 25Mi tegen een feitelijk gebruik van 2Mi en een boot die het niet haalt, met limit gelijk aan request. Dat is een projectbestand, niet onze code.
- **De retry op ArgoCD-connectiefouten.** Die staat al op main sinds RC-236 (2 oktober). Productie draait `2026.09.28.2215-a5fef305`, van vier dagen eerder, en dat is waarom twee taken op een hik van een seconde sneuvelden: om 10:57:26 een `Cannot connect`, om 10:57:27 dezelfde host weer 200. Geen code nodig, wel een uitrol.

## Verifieerbaar

- `tests/test_tuner_oom_zonder_meting_escaleert_niet.py` groen, inclusief de test die nu rood staat.
- `tests/test_tuner_deadband_na_naverwerking.py` groen. Die faalt aantoonbaar zonder punt 2, met `limit 104Mi -> 105Mi, request 40Mi -> 41Mi`.
- `tests/test_argo_timeout_tekst.py` groen, en `tests/test_argo_connect_retry.py` groen **zonder** aanpassing: dat is de controle dat punt 1 de leesbare meldingen niet heeft stukgemaakt.
- De tuner-suite groen: de twaalf bestanden rond `test_resource_*`, `test_oom_*`, `test_auto_tune_*`, `test_tuning_dedup`, `test_gebruikerswens_resources` (142 tests op het moment van schrijven).
- `uv run ruff check . --fix`, `uv run ruff format .`, `uv run pyright` schoon.
- Een poort op het schrijfpad: toon aan dat een tweede tune-ronde op onveranderd bewijs **geen** commit naar de projects-repo doet. De bug kostte vier pod-rollouts en vier commits op bewijs van één pod, dus "geen wijziging" is hier de meetbare uitkomst en niet een bijzaak.
- `kustomize build bootstrap/rig-system/kustomize/overlays/odcn-production` moet blijven bouwen na punt 3.
