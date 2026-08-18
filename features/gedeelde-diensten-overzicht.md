# Gedeelde diensten: hoe vol, hoe druk

Een beheerpagina op `/admin/diensten` die laat zien hoe de GEDEELDE diensten ervoor staan:
hoe vol elke PVC zit, hoe groot elke database is, hoeveel verbindingen er openstaan.

Aanleiding: dat was nergens te zien, dus merkten we het pas als er iets omviel. Bij de
meting tegen productie van 18 augustus 2026 stond `production-typesense-data-pvc` in
`rig-prd-ubbw-0i1` op 92,7% en zag niemand dat.

Alerting valt hier buiten. De drempels staan wel al op een plek zodat alerting straks
dezelfde grenzen kan gebruiken.

## Wat de pagina toont

| Blok | Inhoud |
|---|---|
| Opslag | Per PVC de vulling (volst eerst), de namespace, de claim, gebruikt/capaciteit en de inodevulling |
| Databases | Per database grootte, verbindingen, langste transactie en XID-leeftijd; daaronder per instantie de verbindingen en de WACHTENDE verbindingen |
| Wat er niet gemeten wordt | Redis en MinIO, met de reden en wat ervoor nodig zou zijn |
| Drempels | De grenzen waarop een rij hierboven kleurt, met eenheid en uitleg |

Alleen een platformbeheerder komt erin (`require_platform_admin`) - de pagina toont
gegevens over alle projecten heen. Die grendel staat ook op de twee fragmenten, want dat
zijn gewone URL's.

## Gebruik

Menu > Beheer > Gedeelde diensten, of rechtstreeks `/admin/diensten`.

Een rij kleurt volgens `DREMPELS` in `opi/services/gedeelde_diensten.py`:

| Meting | Let op vanaf | Kritiek vanaf |
|---|---|---|
| `pvc_vulling` | 75% | 85% |
| `pvc_inodes` | 75% | 85% |
| `verbindingen_wachtend` | 1 | 5 |
| `langste_transactie` | 300 s | 3600 s |
| `xid_leeftijd` | 150.000.000 | 500.000.000 |

Een waarde die er niet is wordt `Onbekend` en nadrukkelijk niet `OK`: niet kunnen meten is
geen goed nieuws.

## Hoe het werkt

```
/admin/diensten            de pagina: kaders, drempels, wat er niet gemeten wordt
  |-- /admin/diensten/opslag      htmx hx-trigger="load" -> haal_opslag()
  +-- /admin/diensten/databases   htmx hx-trigger="load" -> haal_databases()
                                        |
                                  get_metrics_connector()
                                        |
                          Prometheus (sandbox) of Grafana -> Mimir (productie)
```

Bestanden:

| Bestand | Wat |
|---|---|
| `opi/services/gedeelde_diensten.py` | Drempels, queries, het omzetten van reeksen naar rijen |
| `opi/web/router_shared_services.py` | De pagina en de twee fragmenten |
| `opi/templates_lotc/bg/admin-gedeelde-diensten.html.j2` | De pagina |
| `opi/templates_lotc/bg/_gedeelde-diensten-opslag.html.j2` | Het opslagblok |
| `opi/templates_lotc/bg/_gedeelde-diensten-databases.html.j2` | Het databaseblok |
| `opi/templates_lotc/bg/_diensten-status.html.j2` | De weergave van een toestand, en van een leeg/kapot blok |

### Drie keuzes die er toe doen

**De bron is de connector, niet een URL.** In productie staat `METRICS_BACKEND=grafana` en
loopt alles via Grafana naar Mimir; onze eigen Prometheus in `rig-prd-operations` heeft de
volume- en containermetrieken helemaal NIET. `get_metrics_connector()` kiest de juiste bron
per omgeving. Wie hier een URL invult, bouwt tegen de verkeerde bron - dat is bij het
dashboard eerder misgegaan.

**Een query per blok, nooit een per rij.** Elke query geeft alle reeksen tegelijk terug
(`sum by (namespace, persistentvolumeclaim) (...)`), en de queries van een blok gaan samen
via `asyncio.gather`. Het projectdashboard doet 132 ArgoCD-aanroepen per weergave en duurt
daardoor seconden; dat schaalt lineair mee met het platform. Beide blokken laden lui, zodat
de pagina er meteen staat en een trage bron hem niet ophoudt.

**"Kon niet meten" is niet "niets te melden".** Een mislukte meting logt op WARNING (niet
DEBUG) en levert `gemeten=False` met de fout op; de pagina zegt dan expliciet dat hij niet
kon meten. Op het dashboard zag een kapotte grafiek er maandenlang identiek uit als "geen
verkeer".

## Wat er NIET gemeten wordt, en waarom

- **Redis**: er zijn alleen `argocd_redis_*`-metrieken, en dat zijn de clientmetrieken van
  ArgoCD, niet de toestand van onze Redis. Nodig: een redis-exporter plus een scrape-config.
- **MinIO**: nul metrieken. De vulling van zijn PVC staat er wel (die komt van de kubelet),
  de interne toestand niet. Nodig: MinIO's eigen `/minio/v2/metrics` aanzetten en scrapen.

Ze worden op de pagina BENOEMD in plaats van weggelaten: een leeg vak leest als "in orde".

## Afhankelijkheden

- kubelet-volumemetrieken (`kubelet_volume_stats_*`) voor het opslagblok
- de CNPG-exporter (`cnpg_*`) voor het databaseblok
- in productie: Grafana met de `mimir-prd`-datasource; in de sandbox de lokale Prometheus

Zonder bereikbare bron blijft de pagina heel en zegt hij dat hij niet kon meten. Ontbreken
alleen de metrieken, dan is het blok leeg met "Niets te melden".

## Tests

| Test | Wat hij pint |
|---|---|
| `tests/test_gedeelde_diensten.py` | De drempels, de sortering, het onderscheid gemeten/niet-gemeten (inclusief het WARNING-niveau) en dat het aantal queries niet met het aantal rijen meegroeit |
| `tests/test_admin_diensten_toegang.py` | Dat de pagina EN beide fragmenten een niet-beheerder weigeren |
| `tests/e2e/test_gedeelde_diensten_pagina.py` | Dat de blokken echt renderen, in de goede volgorde, met Redis en MinIO benoemd |
