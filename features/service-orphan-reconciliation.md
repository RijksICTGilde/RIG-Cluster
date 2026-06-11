# Service-orphan reconciliation (rapport-eerst opruiming van DB's, Keycloak-clients, MinIO-buckets)

## Probleem

Deletes uit het pre-#123 tijdperk rapporteerden succes terwijl service-resources
bleven staan. De idempotente delete-API ("already absent" o.b.v. het projectbestand)
ruimt die historische wezen nooit op. Geïnventariseerd op productie 2026-06-11:

- **Keycloak realm `regel-k4c-odcn-production`: 74 PR-genummerde clients vs 11
  actieve previews → ~63 wezen, allemaal `public` clients** (PR's uit de 200-300
  reeks, maanden dood). Mild security-relevant: live OIDC-entrypoints met
  redirect-URI's naar dode preview-hosts.
- Realm `wies-odcn-production`: 50 clients — zelfde patroon, nog niet geauditeerd.
- rig-db: 7 wees-databases voor regel-k4c (`regel_k4c_pr104` t/m `pr128`) +
  `regel_k4c_pr748_v1` (clone-restant); cluster-totaal 55 databases incl. de
  bekende `marked_for_deletion`-backlog die geen purge-scheduler heeft.
- MinIO-buckets: nog niet geïnventariseerd, zelfde verdenking.

De huidige delete-flow is sinds #123 WEL schoon (bewijs: pr777 op 2026-06-11 —
ArgoCD-app, pods, database én Keycloak-clients allemaal verwijderd en geverifieerd).
Dit gaat puur om historisch afval.

## Ontwerp-eisen

1. **Rapport-eerst, nooit direct purgen.** `waggl_9et_productie` (live prod-DB!)
   stond ooit onterecht als marked_for_deletion — een blinde purge had hem
   gedropt. Sweep produceert een rapport; verwijdering alleen vanaf een
   bevestigde lijst.
2. Inventariseer per service: pg_database (rig-db), Keycloak clients per realm
   (admin-API of read-only SQL op de keycloak-DB), MinIO buckets (mc ls).
3. Match tegen de waarheid: live deployments uit alle projectbestanden
   (zad-projects repo) — zelfde bron als de delete-API gebruikt.
4. Let op naamgevingsvarianten: `_v1`-suffixen (clone-generaties),
   `-public`/`-private` clientparen, deployment- vs componentnamen.
5. Het bestaande orphan-detect is een stub (zie geheugen rig-db reconciliation)
   — dit vervangt/implementeert die.
6. Hergebruik de verificatie-aanpak van #123: k8s/service-API is ground truth,
   niet de eigen administratie.

## Inventarisatie-queries (de specificatie)

```bash
# Databases
kubectl -n rig-prd-operations exec rig-db-1 -- psql -U postgres -tAc \
  "SELECT datname FROM pg_database WHERE datistemplate=false ORDER BY datname"

# Keycloak clients per realm (read-only)
kubectl -n rig-prd-operations exec rig-db-1 -- psql -U postgres -d keycloak -tAc \
  "SELECT r.name, c.client_id, c.public_client FROM client c
   JOIN realm r ON c.realm_id=r.id ORDER BY r.name, c.client_id"
```

## Relatie met bestaand werk

- #123 (honest delete) — de flow vooruit is schoon; dit ruimt het verleden op.
- Geheugen: `project_rigdb_restarts_reconciliation` (purge-gap, waggl-waarschuwing),
  `project_incident_20260610_netpol` (context van deze week).
- Nachtelijke cleaner bestaat al voor deployments; dit is de service-laag eronder.

## Implementatie (2026-06-11)

### Sweep (rapport, nul mutaties)

```bash
curl -X GET "https://<opi>/api/v2/admin/orphans/report" -H "X-API-Key: $ADMIN_API_KEY"
```

Inventariseert databases (rig-db), Keycloak realms/clients en MinIO-buckets en
classificeert tegen de live projectbestanden:

| Classificatie | Betekenis | Verwijderbaar |
|---|---|---|
| `expected` | hoort bij een live deployment | nee |
| `system` | platform-infrastructuur (system-DB's, Keycloak built-ins, backup-buckets) | nee |
| `orphan_candidate` | projectnaamgeving, geen live deployment | **alleen via confirm** |
| `in_use_anomaly` | lijkt wees maar heeft actieve connecties | nee — onderzoeken |
| `unknown` | matcht geen naamgevingsconventie | nee |

Het rapport bevat ook `stale_marks`: marks waarvan de resource al weg is of
juist weer in de verwachte set zit.

### Bevestigen (start grace-periode)

```bash
curl -X POST "https://<opi>/api/v2/admin/orphans/confirm" \
  -H "X-API-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"items": [{"type": "postgresql_database", "name": "regel_k4c_pr104"},
                 {"type": "keycloak_client", "name": "regel-k4c-pr250-public", "realm": "regel-k4c-odcn-production"}]}'
```

De sweep wordt server-side opnieuw uitgevoerd; alleen items die op dat moment
`orphan_candidate` zijn worden geaccepteerd. Daarna geldt de normale
grace-periode (`DELETION_GRACE_PERIOD_DAYS`, 7 dagen) en verwijdert
`POST /api/v2/admin/reconciliation/trigger?dry_run=false` ze definitief.

### Veiligheidslagen (lessen van waggl-9et)

1. `_build_expected_resources` leest nu schema v1 én v2/v2.2 (catalog-
   componenten + legacy deployment-level blokken + clone-generaties).
2. Purge hercontroleert de verwachte set op het moment van verwijderen:
   een mark waarvan de resource weer in de YAML staat wordt ge-unmarkt.
3. Een database met actieve connecties wordt NOOIT gedropt door de purge
   (geweigerd + gerapporteerd); alleen de expliciete projectverwijdering
   mag connecties termineren.
4. `cleanup/trigger` (project-scoped) heeft dezelfde bescherming als de
   volledige reconcile.
