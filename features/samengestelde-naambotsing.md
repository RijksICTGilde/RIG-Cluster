# Samengestelde namen botsen niet tussen projecten

Een database, bucket, MinIO-gebruiker, Keycloak-client en ArgoCD-application worden benoemd
door de **projectnaam en de deploymentnaam aan elkaar te plakken** met een scheidingsteken.
Dat scheidingsteken mag ook in de namen zelf voorkomen, dus twee verschillende paren kunnen
exact dezelfde naam opleveren:

| Project | Deployment | Databasenaam | Bucketnaam |
|---|---|---|---|
| `amt` | `odc-prd-staging` | `amt_odc_prd_staging` | `amt-odc-prd-staging` |
| `amt-odc-prd` | `staging` | `amt_odc_prd_staging` | `amt-odc-prd-staging` |

## Waarom dat erger is dan een dubbele naam

Provisioning behandelt een bestaande resource met die naam als de zijne.
`database_manager._create_or_update_user` ziet `status == "exists"`, zet het wachtwoord om en
geeft de rol aan het nieuwe project; `minio_manager` doet hetzelfde met de secret key van de
MinIO-gebruiker. Het tweede project krijgt daarmee de database van het eerste in handen, en
het eerste project kan er niet meer bij omdat zijn wachtwoord niet langer geldig is. Dat is
een overname van de data van een ander project, niet alleen een naamconflict.

## Twee lagen

`composite_name_collision` in `opi/manager/clone_validation.py` is de enige plek waar de
beslissing valt. De functie roept de echte naamfuncties uit `opi/utils/naming.py` aan in
plaats van hun formaat te kopiëren, zodat een gewijzigd formaat de guard niet stil kan laten
afwijken van de naam die hij beschermt.

**Laag 1, bij het schrijven.** `validate_project_structure` is het knooppunt dat elke
schrijfweg passeert (wizard, API v1 en v2, en een hand-geschreven bestand via
`GitProjectStore._validate`). Het weigert met een melding in het Nederlands zodra een
**nieuw** paar de naam van een ander project oplevert. Alleen nieuwe paren: een paar dat al
in het vorige bestand stond blijft opslaan, zodat een botsing van vóór deze guard geen van
beide projecten uit zijn eigen bestand sluit. Uitrollen doet zo'n paar niet meer, want laag
2 weigert hem alsnog.

**Laag 2, bij het provisionen.** `database_manager.create_resources_for_deployment` en
`minio_manager.create_resources_for_deployment` stellen dezelfde vraag voordat ze een rol,
database of gebruiker aanraken, en falen luid. Dit vangt een projectbestand dat laag 1 nooit
zag, bijvoorbeeld omdat het er al stond. Ook een paar dat laag 1 liet passeren omdat het al
in het vorige bestand stond: bij het provisionen is er geen vorige versie om dat aan af te
lezen, en doorgaan zou het wachtwoord van de ander omzetten.

## Eigendom komt uit de projectstore

De vraag "hoort deze naam bij dit project?" wordt gesteld aan de projectstore
(`other_project_deployments` leest de in-memory projectcache), niet aan PostgreSQL of MinIO.
Dat is de eenvoudigste bron, want hij vraagt geen migratiepad voor de rollen die er al zijn,
en de betrouwbaarste: een `COMMENT ON ROLE` is door een tenant met `SUPERUSER` zelf te
herschrijven, een projectbestand niet.

## Welke namen meedoen

Alles in `opi/utils/naming.py` dat `{project}` en `{deployment}` met een scheidingsteken
samenvoegt, in drie vormen:

| Vorm | Naamfuncties |
|---|---|
| `{project}_{deployment}` | `generate_database_name`, `generate_database_username`, `generate_database_schema`, `generate_minio_username`, `generate_minio_policy_name` |
| `{project}-{deployment}` | `generate_bucket_name`, `generate_keycloak_client_id`, `generate_argocd_application_name`, `generate_argocd_application_filename`, `generate_project_deployment_prefix` |
| `{deployment}-{project}` | `generate_redis_username`, `generate_redis_key_prefix` |

De omgekeerde vorm botst op een ander paar dan de eerste twee, dus alle drie staan in de
guard. Niet meegenomen:

- **De PVC-naam.** `generate_pvc_name` plakt deployment en component, niet project en
  deployment, en een PVC staat in de eigen namespace van het project.
- **De hostnaam-familie** (`generate_hostname`, `generate_ingress_map`). Die voegt drie
  namen samen (component, deployment, project) en wordt al afgedwongen door het
  subdomein-register en `DomainConfigEnforcer`.
- **Het generatie-achtervoegsel.** `{project}_{deployment}_v1` kan ook door een paar met een
  `_v1` in de naam worden samengesteld. De guard vergelijkt de basisnaam; de generatie is een
  smallere restrisico die een eigen taak verdient.

## Tests

`operations-manager/python/tests/test_samengestelde_naambotsing.py` legt vast dat
`amt`/`odc-prd-staging` tegen `amt-odc-prd`/`staging` op elke schrijfweg wordt geweigerd, dat
de tweede laag geen wachtwoord zet voor een vreemde rol, en dat een bestaande populatie
projectbestanden door de guard niet wordt geweigerd (zet `ZAD_PROJECTS_AUDIT_DIR` naar de map
met projectbestanden; zonder die map slaat die ene toets zichzelf over).
