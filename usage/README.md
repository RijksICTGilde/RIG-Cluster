# Usage Queries

Prometheus queries voor het berekenen van geheugengebruik en kosten.

## Uitvoeren

- Voer de queries uit in de **billing tenant**
- Voer ze uit als **Instant query**, niet als Range query

## Bestanden

- `memory-usage-fixed-range.promql` — Vaste periode van 31 dagen. Handmatig de `[31d:1h]` en `31 * 86400` aanpassen voor andere periodes.
- `memory-usage-grafana-range.promql` — Gebruikt Grafana's time picker (`$__range`). Selecteer de gewenste maand in de time picker (bijv. 2026-01-01 tot 2026-02-01).

## Queries per paneel

Elk bestand bevat twee queries die als aparte queries in hetzelfde Grafana paneel gebruikt worden:

- **Query A**: Geheugengebruik in GiB
- **Query B**: Kosten in EUR (prijs per GiB: 27 EUR)
