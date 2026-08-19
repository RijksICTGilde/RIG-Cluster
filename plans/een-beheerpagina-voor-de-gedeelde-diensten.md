# Een beheerpagina voor de gedeelde diensten: hoe vol, hoe druk, en wanneer moeten we bijsturen

Er is geen overzicht van hoe de gedeelde diensten ervoor staan: hoe vol de PVC's en databases zitten, hoeveel verbindingen er openstaan. Daardoor merken we het pas als iets omvalt. Deze taak bouwt dat overzicht. Alerting is een volgende stap en valt hier buiten, maar het overzicht moet er wel op voorbereid zijn.

## Wat er al gemeten kan worden (18 augustus 2026, tegen productie)

Belangrijk voor wie dit bouwt: **productie leest niet uit onze eigen Prometheus maar via Grafana uit Mimir.** `METRICS_BACKEND=grafana`, datasource-uid `mimir-prd`, en `get_metrics_connector()` (`opi/connectors/prometheus.py:1014`) levert dan de `GrafanaPrometheusConnector`. In onze eigen Prometheus (`prometheus.rig-prd-operations:9090`) staan 737 metrieken en dáár ontbreken volume- en containermetrieken volledig; in Mimir staan ze wel. Bouw dus op de connector en niet op een URL, anders bouw je tegen de verkeerde bron. Dat is bij het dashboard eerder misgegaan.

Deze drie zijn gedraaid tegen productie en geven data terug:

| Wat | Query | Uitkomst bij de meting |
|---|---|---|
| PVC-vulling | `100 * kubelet_volume_stats_used_bytes / kubelet_volume_stats_capacity_bytes` | werkt, per namespace en claim |
| Databasegrootte | `cnpg_pg_database_size_bytes` | werkt, per database |
| Verbindingen | `cnpg_backends_total` (som 90), `cnpg_backends_waiting_total` | werkt |

Verder beschikbaar en de moeite waard: `kubelet_volume_stats_inodes_used`/`_free` (een volle inode-tabel geeft "no space left" terwijl de bytes meevallen), `cnpg_backends_max_tx_duration_seconds` (een transactie die blijft hangen houdt vacuum tegen), `cnpg_pg_database_xid_age` (wraparound), en `kube_persistentvolumeclaim_resource_requests_storage_bytes` (wat er gevraagd is, naast wat er gebruikt wordt).

**Dit staat er nu al, en het is precies waarom dit overzicht nodig is:**

```
92.7%  rig-prd-ubbw-0i1/production-typesense-data-pvc
62.8%  rig-prd-mb-docs-helmfile-infrastructure/mb-docs-helmfile-db-1
60.8%  rig-prd-algor-odc-infrastructure/algor-odc-db-1
40.5%  rig-prd-operations/minio-storage-versioned
```

Die eerste is een reëel probleem dat nu niemand ziet.

## Wat er NIET gemeten kan worden

- **Redis**: alleen `argocd_redis_*`, en dat zijn de clientmetrieken van ArgoCD, niet de toestand van onze Redis. Er is geen redis-exporter.
- **MinIO**: nul metrieken. De vulling van zijn PVC is er wel (via kubelet), de interne toestand niet.

**Bouw daar geen lege vakken voor.** Noem in het verslag wat er ontbreekt en wat ervoor nodig zou zijn (een exporter, en wie die aanzet), zodat dat een eigen afweging wordt. Een overzicht met drie lege kaarten leest als een kapotte pagina.

## Wat er gebouwd moet worden

Een beheerpagina, in de trant van `/admin/usage` en `/admin/approvals`, met per gedeelde dienst wat er werkelijk te meten valt. Richtinggevend, niet voorschrijvend:

- **Opslag**: per PVC de vulling, gesorteerd op volst, met de namespace en de claimnaam erbij. Dit is het belangrijkste blok; het is waar de eerste echte melding vandaan komt.
- **Databases**: grootte per database, aantal verbindingen, wachtende verbindingen, langste transactie.
- **Wat er niet gemeten wordt**: expliciet benoemd op de pagina zelf, niet weggelaten. Een beheerder moet kunnen zien dat Redis en MinIO niet in beeld zijn, anders leest afwezigheid als "goed".

Drempels mogen erbij (een PVC boven de 85% valt op), maar houd ze **zichtbaar en op één plek**, want de volgende stap is alerting en die moet dezelfde grenzen gebruiken. Verzin geen tweede waarheid.

## Valkuilen

**Bouw op de connector, niet op een URL.** Zie hierboven; `get_metrics_connector()` kiest de juiste bron per omgeving. In de sandbox is dat de lokale Prometheus, en die heeft deze metrieken misschien niet: zorg dat de pagina daar leeg maar heel is, en zeg waarom.

**Eén verzoek per blok, niet per rij.** Het projectdashboard doet 132 ArgoCD-aanroepen per weergave en duurt daardoor 2 tot 3 seconden; dat schaalt lineair mee met de omvang van het platform. Vraag hier per blok één query die alle reeksen tegelijk teruggeeft, en laat de blokken lui laden zoals de detailpagina dat al doet.

**Een mislukte meting hoort hoorbaar te zijn.** Op het dashboard werd een fout op DEBUG gelogd, waardoor een kapotte grafiek er identiek uitzag als "geen verkeer", en dat is maanden zo gebleven. Log op WARNING en toon op de pagina het verschil tussen "niets te melden" en "kon niet meten".

**Wie mag dit zien.** Dit toont gegevens over alle projecten heen. Haak aan bij de bestaande beheerderscontrole (`require_platform_admin`), en zet er een test op.

## Wat hier buiten valt

- **Alerting.** Wel zo bouwen dat de drempels straks herbruikbaar zijn.
- Exporters aanzetten voor Redis en MinIO; wel benoemen wat er nodig is.
- Metingen per project; dit gaat over de gedeelde diensten.

## Verifieerbaar

- De pagina toont op productie de PVC's op volgorde van vulling, met die van 92,7% bovenaan.
- Databasegrootte en verbindingen komen overeen met wat een directe query op Mimir teruggeeft; zet beide getallen in de PR.
- Zonder bereikbare metriekbron blijft de pagina heel en zegt hij dat hij niet kon meten.
- Een niet-beheerder komt er niet in.
- `uv run pytest tests/ -q` groen, plus `ruff check`, `ruff format`, `pyright`.
