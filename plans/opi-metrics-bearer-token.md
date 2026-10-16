# OPI: /metrics alleen met het scrape-token

## Waarom

OPI's eigen Prometheus-endpoint `/metrics` (`opi/api/prometheus_router.py`) is voor iedereen leesbaar. De SSO-middleware slaat het pad over (`SKIP_AUTH_EXACT = ("/metrics",)` in `opi/middleware/authorization.py:27`), de route zelf controleert niets, en de NetworkPolicy laat poort 8000 open. De security review van RC-263 (PR #239) merkte dit op als bestaand gat.

Het ontwerp om dit dicht te zetten bestaat al. Het wordt alleen nog niet door OPI zelf gebruikt:

- Onze Prometheus stuurt bij de job `kubernetes-pods` de header `Authorization: Bearer <token>` mee, met het token uit het secret `prometheus-metrics-auth` (`infrastructure/bootstrap/infrastructure/prometheus/controller/base/configmap.yaml` en `overlays/odcn/configmap-patch.yaml`, `credentials_file: /etc/prometheus/secrets/metrics-auth/token`).
- OPI krijgt hetzelfde token als `PROMETHEUS_METRICS_AUTH_TOKEN` (`opi/core/config.py:480`), in odcn-production en sandboxed-local via `patches/deployment.yaml`.
- Projectapps met de service `metrics-scraper` krijgen dat token al om de header zelf te controleren (`opi/services/catalog/metrics_scraper/__init__.py`).

OPI wordt alleen door die job gescrapet, via de pod-annotaties `prometheus.io/scrape`, `prometheus.io/port: "8000"` en `prometheus.io/path: "/metrics"` (`bootstrap/rig-system/kustomize/operations-manager/base/deployment.yaml:23-25`). In `rig-prd-operations` scrapen de ServiceMonitors en de PodMonitor alleen ArgoCD en rig-db, niet OPI (gemeten op 16 oktober 2026). Een header-controle breekt dus geen bestaande scrape. Ook gemeten op 16 oktober 2026: het secret `prometheus-metrics-auth` staat in `rig-prd-operations`, de OPI-pod leest `PROMETHEUS_METRICS_AUTH_TOKEN` eruit (sleutel `token`), en onze Prometheus draait in diezelfde namespace.

## Wat bewezen moet worden

1. `/metrics` geeft alleen een antwoord met inhoud als de header `Authorization: Bearer <PROMETHEUS_METRICS_AUTH_TOKEN>` klopt.
2. Zonder header, met een ander schema dan `Bearer`, of met een fout token komt er geen enkele metriek terug.
3. Zonder geconfigureerd token is het endpoint dicht, niet open.
4. `/metrics-explorer` en de andere routes gedragen zich als voorheen.

## Basis

Bouw op forgejo/main. Bij het schrijven van dit plan stond RC-263 (PR #239, event-loop-blokkades) nog open; die raakt `opi/core/metrics.py` maar niet de route of de middleware. Wordt RC-263 eerder gemerged, dan is dat geen conflict voor deze taak.

## Het werk

### 1. Controle op de route

Volg het bestaande patroon van `validate_admin_api_key` in `opi/api/endpoint_util.py` (rond regel 110-140): een decorator die de header leest, met `secrets.compare_digest` vergelijkt, en bij een ontbrekende configuratie dicht blijft. Schrijf geen tweede vergelijkingsroutine ernaast; als een gedeelde helper de drie decorators eenvoudiger maakt, mag dat, maar alleen als de bestaande twee zich daarna precies gelijk gedragen.

Gedrag:

| situatie | antwoord |
|---|---|
| geen `Authorization`-header | 401 |
| header zonder `Bearer `-voorvoegsel | 401 |
| `Bearer` met fout token | 401 |
| `PROMETHEUS_METRICS_AUTH_TOKEN` niet gezet | 501, zoals `validate_admin_api_key` bij een ontbrekende `ADMIN_API_KEY` |
| `Bearer` met het goede token | 200 met de huidige inhoud |

Het antwoord bij 401 en 501 bevat geen metrieken en geen hint over het token. Log een mislukte poging op WARNING zonder de headerwaarde. Let op: `opi/utils/logging_config.py:25` sluit `/metrics` uit van de toegangslog; laat dat zo, de WARNING komt uit de decorator.

`SKIP_AUTH_EXACT` blijft `("/metrics",)`: Prometheus heeft geen SSO-sessie, dus de SSO-middleware moet het pad blijven overslaan. De bescherming zit in de route. Pas het commentaar op `authorization.py:26-27` aan zodat het niet meer "public" zegt maar verwijst naar de token-controle in de route. Doe hetzelfde voor de verwijzingen in `opi/utils/csrf.py:50-62` en `opi/middleware/maintenance.py:22` als hun commentaar "public" of "onbeschermd" zegt; de bypass zelf blijft.

### 2. Toetsen

In een nieuw testbestand naast de bestaande route-tests, via de FastAPI-testclient op de echte app:

- de vijf regels uit de tabel hierboven;
- bij 200 staat een bekende OPI-metriek in de body (bijvoorbeeld een `process_`-metriek);
- bij 401 en 501 staat geen enkele `# HELP`-regel in de body;
- `/metrics-explorer` geeft zonder header hetzelfde als voorheen (een redirect naar login of het bestaande gedrag; toets wat er nu gebeurt, niet een aanname).

`tests/test_authorization.py::test_metrics_scrape_endpoint_skips_auth` blijft gelden (de middleware slaat het pad over). Hernoem of herformuleer de docstring als die zegt dat het endpoint "public" moet blijven.

Controleer of de e2e-testserver (`tests/e2e/testserver.py`) of een andere test `/metrics` zonder header opvraagt; zo ja, geef die het token mee.

### 3. Documentatie

Werk `features/metrics-endpoint-security.md` bij: OPI's eigen `/metrics` eist nu dezelfde Bearer-header als de projectapps, met het token uit `prometheus-metrics-auth`, en hoe je hem met de hand opvraagt:

```bash
TOKEN=$(kubectl get secret -n rig-prd-operations prometheus-metrics-auth -o jsonpath='{.data.token}' | base64 -d)
kubectl exec -n rig-prd-operations deploy/operations-manager -- \
  sh -c 'curl -s -H "Authorization: Bearer $PROMETHEUS_METRICS_AUTH_TOKEN" localhost:8000/metrics | head'
```

De sleutel in het secret heet `token` (`infrastructure/bootstrap/infrastructure/secrets/templates/prometheus-metrics-auth-secret.yaml`, en zo leest de odcn-overlay hem ook).

Als `features/event-loop-lag-watchdog.md` (RC-263) al op forgejo/main staat en zegt dat `/metrics` onbeschermd is, pas dat aan.

## Randvoorwaarden

- Niets uitrollen, geen wijziging aan clusters of secrets.
- Geen wijziging aan de Prometheus-configuratie: die stuurt de header al.
- Geen nieuwe dependency.
- Code-stijl volgens `CLAUDE.md`: imports bovenaan, moderne type hints, specifieke exceptions, minimaal commentaar, Nederlandse prose met Engelse vaktermen, nooit em dashes.

## Na de uitrol (niet in deze taak, voor de PR-beschrijving)

In de PR-beschrijving een korte controle voor na de uitrol: in de targets van onze Prometheus staat de OPI-pod op `up`, en `curl` op `/metrics` zonder header geeft 401. Staat de target op `down` met 401, dan mist de Prometheus van die cluster het secret of de `authorization`-sectie.

## Klaar als

- De toetsen uit punt 2 staan er en zijn groen; de 401-toetsen faalden zonder de wijziging (noteer dat in de PR).
- `cd operations-manager/python && uv run ruff check . && uv run ruff format --check . && uv run pyright` is schoon.
- De bestaande testsuite is groen.
- `features/metrics-endpoint-security.md` beschrijft het nieuwe gedrag.
