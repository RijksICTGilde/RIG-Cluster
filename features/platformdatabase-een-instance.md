# Platformdatabase: één instance

`rig-db` is de ene PostgreSQL waar Keycloak, Forgejo, OPI, de mailrelay en alle gedeelde
projectdatabases op staan. Hij draait op elk cluster met één instance, zonder
PodDisruptionBudget.

## Wat er staat waar

| Laag | `instances` | `enablePDB` |
|---|---|---|
| `database/base` | 1 | niet gezet (CNPG-default) |
| `overlays/sandboxed-local` | 1 (van base) | niet gezet |
| `overlays/odcn-staging` | 1 (van base) | `false` |
| `overlays/odcn` | 1 (van base) | `false` |

## Waarom geen tweede instance

RC-308 zette productie op twee instances met een PDB en anti-affinity. Dat is
teruggedraaid, voor staging en productie allebei. Een replica staat stand-by: hij neemt
het pas over bij een failover, nodeonderhoud of een herstart, en wint dan een paar
seconden ten opzichte van een pod die opnieuw start. Daarvoor betaal je een tweede pod,
een tweede PVC en het geheugen en de CPU nog een keer, tegen het tenantquotum van ODCN.
Dat weegt niet op tegen die winst.

Wat dat betekent: een herstart van `rig-db` (een postmaster-parameter, een CNPG-upgrade,
nodeonderhoud) is korte downtime voor het hele platform. Plan zo'n wijziging op een
rustig moment.

## De PDB staat expliciet uit

De Kyverno-regel `pdb-cnpg-cluster-validation` eist `enablePDB: false` zolang
`instances < 2`: een PDB over een enkele pod blokkeert een node-drain in plaats van hem te
overleven. CNPG zet hem standaard aan, dus de ODCN-overlays zetten hem zelf uit. Dezelfde
regel zit in de template voor projectdatabases (`manifests/postgresql-cluster.yaml.jinja`).

`tests/test_platformdatabase_een_instance.py` toetst dit op de gerenderde overlays.
