# Metriekbackends: transport in de connector, queries in de domeinlaag

## Wat het is

Het portaal meet via twee backends. Op de sandbox praat `PrometheusConnector`
rechtstreeks met Prometheus, in productie en op staging gaat `GrafanaPrometheusConnector`
via Grafana naar Mimir. `get_metrics_connector()` kiest op `METRICS_BACKEND`, en een
aanroeper merkt niet welke hij kreeg.

De connectors doen alleen **transport**. Elke PromQL-query staat in de domeinlaag, in
`opi/services/metrics_queries.py`, en wordt dus een keer geschreven voor beide backends.

## Hoe je het gebruikt

```python
from opi.connectors.prometheus import get_metrics_connector
from opi.services.metrics_queries import TimeWindow, component_timeseries

backend = await get_metrics_connector()
if backend.is_connected:
    reeksen = await component_timeseries(backend, "rig-demo", "productie-api", TimeWindow())
```

Een nieuwe meting voeg je toe als functie in `metrics_queries.py`, met de backend als
eerste parameter. Niet als methode op een connector: dan heeft hij meteen twee varianten.

Een scherm dat losse totalen in plaats van een tijdreeks wil, haalt ze op bij een bouwer
en bevraagt ze zelf: `namespace_usage_queries()` geeft de totalen die het dashboard en de
projectpagina beide tonen, `usage_per_namespace_queries()` dezelfde totalen met
`by (namespace)` erbij zodat een query alle projecten tegelijk dekt, en
`network_traffic_queries()` het verkeer voor een range-query.

### Het transportcontract

`opi/connectors/metrics_backend.py` beschrijft wat een backend biedt
(`MetricsBackend`), en houdt de twee fouttypes en de tijdparser die ze delen.

| Methode | Wat het doet |
|---|---|
| `query(expr, *, at=None, datasource_uid=None)` | Een instant-query. `at` is het moment van evalueren, `datasource_uid` een andere datasource (alleen Grafana; de directe connector weigert hem) |
| `query_range(expr, start, end, step)` | Een range-query. De grenzen mogen epoch-seconden of ISO-8601 zijn |
| `query_batch(queries)` | Meer instant-queries, resultaat per sleutel |
| `query_range_batch(queries, start, end, step)` | Meer range-queries over een venster |
| `is_connected`, `start_probe()` | Beschikbaarheid, zie [metrics-graceful-degradation.md](metrics-graceful-degradation.md) |

Grafana zet een batch in een HTTP-verzoek; Prometheus draait hem in een thread, want
`prometheus_api_client` is synchroon en zou anders de event loop vasthouden.

| Fout | Wanneer |
|---|---|
| `MetricsUnavailableError` | De backend is onbereikbaar of weigert het token. Alleen Grafana gooit hem: de directe connector kent geen beschikbaarheid |
| `MetricsQueryError` | De backend antwoordde, de query mislukte |

`discover_metric_names()` staat alleen op `PrometheusConnector`: die praat met
`/api/v1/series` en niet met PromQL. Zie
[application-metrics-explorer.md](application-metrics-explorer.md).

## Verschillen die blijven

Deze drie zijn gemeten en vastgelegd in `tests/test_metrics_backend_parity.py`. Ze zijn
niet gewenst, ze zijn er.

- **De limietlijn.** `component_timeseries` vraagt de limiet en de request met `max()`
  over de pods op Prometheus en met `sum()` op Grafana (`_limit_aggregation`). Met meer
  dan een pod tekent de productiegrafiek de limiet N keer te hoog.
- **De step.** Prometheus krijgt de step als resolutie, Grafana als `interval`, wat daar
  een minimum is: Grafana leidt de echte stap af uit het bereik en `maxDataPoints`. Over
  een lang bereik heeft productie dus minder punten dan de sandbox.
- **Beschikbaarheid.** `PrometheusConnector.is_connected` is altijd `True`, dus de
  sandbox meldt nooit "niet beschikbaar" maar wel lege data.

## Afhankelijkheden

- `opi/connectors/metrics_backend.py`: het contract, de fouttypes, `parse_query_time`
- `opi/connectors/prometheus.py`: de directe backend plus `get_metrics_connector()`
- `opi/connectors/grafana_prometheus.py`: de Grafana-backend
- `opi/services/metrics_queries.py`: de queries, de eenheden en de interpretatie

## Tests

| Test | Wat hij pint |
|---|---|
| `tests/test_metrics_backend_parity.py` | Dat hetzelfde antwoord door beide backends dezelfde reeksen oplevert, plus de drie verschillen hierboven en welke exceptie een storing wordt |
| `tests/test_metrics_queries.py` | De queries zelf: tijdreeksen per workload, PVC-opslag, workload-discovery en de naamontleding |
| `tests/test_prometheus_query_blokkeert_de_loop_niet.py` | Dat een trage query de event loop niet vasthoudt |
| `tests/test_grafana_batch_queries.py` | Dat een batch een HTTP-verzoek is |
