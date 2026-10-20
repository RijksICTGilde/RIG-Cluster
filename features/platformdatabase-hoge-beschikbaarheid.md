# Platformdatabase: twee instances en een PodDisruptionBudget

`rig-db` is de ene PostgreSQL waar Keycloak, Forgejo, OPI, de mailrelay en alle gedeelde
projectdatabases op staan. In productie draait hij met een primary en een replica, zodat
nodeonderhoud, een CNPG-upgrade of een postmaster-herstart een switchover wordt in plaats
van downtime voor het hele platform.

## Wat er staat waar

| Laag | `instances` | `enablePDB` | Anti-affinity |
|---|---|---|---|
| `database/base` | 1 | niet gezet (CNPG-default) | niet gezet |
| `overlays/sandboxed-local` | 1 (van base) | niet gezet | niet gezet |
| `overlays/odcn` | 2 | `true` | `preferred`, `kubernetes.io/hostname` |

De sandbox blijft op een instance: een Kind-cluster heeft een node, dus een tweede
instance levert daar geen redundantie op en kost alleen geheugen en een PVC.

Alles staat in `infrastructure/bootstrap/infrastructure/postgresql/database/overlays/odcn/kustomization.yaml`.

## De keuzes

**PDB alleen bij twee instances.** De Kyverno-regel `pdb-cnpg-cluster-validation` eist
`enablePDB: false` zolang `instances < 2`: een PDB over een enkele pod blokkeert een
node-drain in plaats van hem te overleven. Dezelfde regel zit in de template voor
projectdatabases (`manifests/postgresql-cluster.yaml.jinja`).

**Asynchrone replicatie**, de CNPG-standaard. Synchroon (`minSyncReplicas: 1`,
`maxSyncReplicas: 1`) voorkomt dat de laatste transacties bij een failover verloren gaan,
maar laat elke schrijfactie wachten zodra de replica weg is. Op een database waar het hele
platform aan hangt weegt beschikbaarheid hier zwaarder dan de laatste commit.

**Anti-affinity `preferred`, niet `required`.** De replica hoort op een andere node, maar
het aantal schedulable workernodes op ODCN ligt niet in deze repo vast. Met `required`
blijft de replica eeuwig Pending als er maar een node voor hem is, en dan is er geen
replica in plaats van een slecht geplaatste. Staat het aantal nodes vast, dan is `required`
de betere keuze.

## Capaciteit

Een tweede instance vraagt alles nog een keer, uit dezelfde overlay:

| | per instance | totaal met twee |
|---|---|---|
| requests | `1Gi` geheugen, `250m` cpu | `2Gi`, `500m` |
| limits | `4Gi` geheugen, `2` cpu | `8Gi`, `4` |
| opslag | `20Gi` (`ocs-storagecluster-ceph-rbd`) | `40Gi` |

Toets dit bij de uitrol tegen het tenantquotum van ODCN.

## Een switchover uitlokken

```bash
kubectl cnpg promote rig-db rig-db-2 -n rig-prd-operations   # gericht
kubectl delete pod rig-db-1 -n rig-prd-operations            # of: primary weghalen
```

De service `rig-db-rw` volgt de nieuwe primary. Clients verliezen hun bestaande
verbinding en moeten opnieuw verbinden; OPI en Keycloak doen dat zelf via hun pool.

Switchover is niet geoefend op de gedeelde sandbox. Oefen het in een stil venster of op een wegwerp-Kind-cluster.

## Buiten deze wijziging

- Een back-up van de platformdatabase.
- Keycloak en OPI met twee replica's: die vragen respectievelijk Infinispan-clustering en
  een leader-gekozen takenwerker.
