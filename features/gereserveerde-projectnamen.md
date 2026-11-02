# Gereserveerde projectnamen

Wat het is: een projectnaam die samen met de clusterprefix op een namespace van het
platform zelf uitkomt, wordt bij het aanmaken geweigerd. Op elke schrijfweg, en afgeleid
uit de clusterconfiguratie in plaats van uit een losse lijst.

## Waarom

De namespace van een project is de clusterprefix plus de projectnaam
(`get_prefixed_namespace`): `rig-`, `rig-prd-` of `rig-staging-`. De naam `operations`
levert op productie dus `rig-prd-operations` op, en dat is de namespace van de operations
manager zelf. `validate_project_name` toetste alleen de VORM van de naam, dus zo'n project
kwam door.

Wat er dan gebeurt: OPI schrijft het secret `sops-age-key` met de PROJECTsleutel in die
namespace, en overschrijft daarmee de platformsleutel, waarna ArgoCD de platformapplicaties
niet meer rendert. En een projectverwijdering verwijdert de namespace van het platform.

Gevonden in het onderzoek naar ZAD als project op 21 oktober 2026. In de projects-repo
staat geen project met zo'n naam.

## Welke namen

Niet een eigen lijst: `get_platform_namespaces()` leest de platformnamespaces uit
`CLUSTER_CONFIG`, over ALLE clusters, uit de sleutels `namespace`, `argo_namespace`,
`backup_namespace`, `mail_relay_namespace`, `database_operator_namespace` en
`service_namespaces`. Daarnaast gelden de vaste voorvoegsels `kube-`, `openshift-` en
`cnpg-`. Een platformnamespace die aan de clusterconfiguratie wordt toegevoegd is zo
meteen gereserveerd.

`is_reserved_project_name(naam)` is waar op zodra `prefix + naam` op een van de clusters
een platformnamespace is. Vandaag levert dat deze namen op:

| naam | namespace |
|---|---|
| `operations` | `rig-prd-operations` (productie) |
| `backup` | `rig-prd-backup` (productie) |
| `ron` | `rig-ron` (sandbox), `rig-prd-ron` (productie) |
| `system` | `rig-system` (sandbox) |
| `backup-destination` | `rig-backup-destination` (sandbox) |
| `prd-operations`, `prd-backup`, `prd-ron` | dezelfde namespaces, via de sandboxprefix `rig-` |
| `staging-operations`, `staging-backup`, `staging-ron` | de staging-tegenhangers |

Een cluster dat zijn eigen namespaces anders noemt, verschuift deze tabel mee.

## Waar het weigert

De regel geldt bij AANMAKEN. Een bestaand project blijft opslaanbaar, ook al zou zijn naam
vandaag geweigerd worden; er zijn er geen, maar een bestaand project laten vastlopen op
zijn eigen naam is geen verbetering.

| weg | waar |
|---|---|
| elke schrijfweg naar het projectbestand (wizard, API, service-config) | `validate_project_structure`, als er geen vorige versie is |
| de taak die een project aanmaakt | `handle_create_project`, bij `is_new_project` |
| `POST /api/projects/self-service` | `opi/api/router.py` |
| een projectbestand dat rechtstreeks in zad-projects wordt gecommit | `ProjectManager.process_project_from_git`, als het bestand er nog niet was |

De wizard en `POST /api/v2/projects` leiden de technische naam af van de weergavenaam en
zetten er altijd een postfix achter (`ensure_unique_project_name`), dus daar kan zo'n naam
niet uit komen. De weigering staat er voor de wegen waar de naam wel wordt meegegeven.

## De tweede laag

Weigeren bij het aanmaken is de regel; de twee plekken waar de schade zou vallen, weigeren
zelf ook, zodat een namespace van het platform ook langs een onvoorzien pad buiten bereik
blijft:

- `SopsHandler.store_project_sops_key_in_namespace` schrijft `sops-age-key` nooit in een
  platformnamespace en gooit daar een `ValueError`;
- `KubectlConnector.delete_namespace` verwijdert nooit een platformnamespace en gooit daar
  een `KubectlExecutionError`.

## Waar het staat

| onderdeel | bestand |
|---|---|
| `get_platform_namespaces`, `is_platform_namespace`, `is_reserved_project_name` | `opi/core/cluster_config.py` |
| de melding, naast `validate_project_name` | `RESERVED_PROJECT_NAME_MESSAGE`, `opi/utils/project_utils.py` |
