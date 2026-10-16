# Keycloak: de platform-client verdwijnt echt als een project verdwijnt

## Waarom

Als een project wordt verwijderd, ruimt `delete_project_manager.py` in de Keycloak-stap drie dingen op: het projectrealm, de projectadmin in `master`, en de platform-client `{project}-{cluster}-platform` in realm `rig-platform` (`settings.KEYCLOAK_DEFAULT_REALM`). Die laatste stap doet niets, en meldt toch succes.

Gemeten in productie op 16 oktober 2026, 08:41 CEST, bij het verwijderen van project `tvas-7pb`:

```
Deleting client '-tvas-7pb-odcn-production-platform' for deployment 'tvas-7pb-odcn-production-platform' in project ''
Client '-tvas-7pb-odcn-production-platform' not found in realm 'rig-platform'
Deleted platform client tvas-7pb-odcn-production-platform
```

De oorzaak staat in de aanroep (forgejo/main, `opi/manager/delete_project_manager.py` rond regel 647):

```python
await keycloak.delete_deployment_client(
    deployment_name=platform_client_id,
    project_name="",
    realm_name=settings.KEYCLOAK_DEFAULT_REALM,
)
logger.info(f"Deleted platform client {platform_client_id}")
```

`delete_deployment_client` (`opi/connectors/keycloak.py:930`) is bedoeld voor deploymentclients en bouwt de naam zelf: `f"{project_name}-{deployment_name}"`. Met een leeg project wordt dat `-tvas-7pb-odcn-production-platform`, die client bestaat niet, en de functie geeft `False`. De aanroeper kijkt niet naar die uitkomst, logt "Deleted" en zet de operatie op `success`.

Zo is het sinds `d081d980a` (21 november 2025). Elk sindsdien verwijderd project heeft zijn platform-client in `rig-platform` laten staan, met zijn secret en redirect-URI's naar een realm dat niet meer bestaat. De service-orphan-sweep (`opi/jobs/service_orphan_sweep.py`) herkent ze als `orphan_candidate` ("platform client naming but no live project claims it"), maar rapporteert alleen.

## Wat bewezen moet worden

1. **Een projectverwijdering verwijdert de platform-client met zijn echte naam** uit `rig-platform`.
2. **Het resultaat zegt wat er gebeurde.** Bestond de client niet, dan is de operatie `not_found` en staat er geen "Deleted" in de log. Gooit Keycloak een fout, dan komt die in `errors`, zoals nu.

## Basis

Bouw op forgejo/main (`e98a2cfaa` bij het schrijven). RC-263 en RC-265 lopen tegelijk en raken `delete_project_manager.py` op andere plekken (de deploymentverwijdering, niet de Keycloak-opruiming van het project). Houd de wijziging tot deze stap beperkt.

## Het werk

### 1. De juiste functie aanroepen

Vervang in de Keycloak-opruiming van het project de aanroep van `delete_deployment_client` door de functie die er al voor bestaat: `keycloak.delete_client_by_client_id(realm_name, client_id)` (`opi/connectors/keycloak.py:1949`). Die zoekt op clientId, verwijdert, geeft `True` of `False`, en zet het realm terug naar `master`. De purge van de orphan-sweep gebruikt hem al voor precies dit soort clients (`opi/jobs/reconciliation.py`, rond regel 581). Schrijf geen nieuwe helper.

Een platform-client heeft geen `-public`-tweeling: `keycloak_yaml_handler.py` maakt er één aan (rond regel 928). `delete_deployment_client` zocht wel naar `{client_id}-public`; dat is hier niet nodig.

### 2. De uitkomst gebruiken

- `True`: log `Deleted platform client {id}` en operatie `success`, zoals nu.
- `False`: log op INFO dat de client er al niet was, en operatie `not_found`. Geen fout: het doel is bereikt, net als in de purge.
- Een exceptie: blijft gaan zoals nu (`errors`, `logger.exception`).

Kijk of er in dezelfde functie meer stappen zijn die een `False` of `None` negeren en toch `success` noteren (realm, admin). Noem wat je vindt in de PR; repareer alleen wat in dezelfde paar regels zit.

## Toetsen

In de bestaande tests van de projectverwijdering (zoek op `keycloak_realm_deletion` of `Cleaning up project Keycloak realm` in `tests/`), met een gestubde Keycloak-connector:

- Een project `foo` op cluster `odcn-production`: de stub ziet precies één verwijdering in `rig-platform` voor `foo-odcn-production-platform`, en geen enkele aanroep met een naam die met `-` begint.
- De stub geeft `False`: de operatie is `not_found` en de log bevat geen "Deleted platform client".
- De stub gooit: de fout staat in `errors`.

Draai de eerste toets eerst zonder de fix en noteer in de PR dat hij dan faalt.

## Buiten deze taak

- **De achtergebleven clients opruimen.** Dat is beheer en geen code. De route bestaat: `GET /api/v2/admin/orphans/report` (alleen lezen) toont ze als `orphan_candidate` in `keycloak_clients`, en `POST /api/v2/admin/orphans/confirm` markeert gekozen clients voor verwijdering na de grace-periode. De nachtelijke purge draait in prod als dry-run (#205), dus het echte verwijderen is een bewuste stap.
- `delete_deployment_client` zelf is in orde voor zijn eigen gebruik (deploymentclients, `keycloak_manager.py` rond regel 427).

## Randvoorwaarden

- Niets uitrollen. Geen wijziging aan clusters of aan Keycloak, geen push naar GitHub.
- Code-stijl volgens `CLAUDE.md`: imports bovenaan, moderne type hints, specifieke exceptions, minimaal commentaar, Nederlandse prose met Engelse vaktermen, nooit em dashes.

## Klaar als

- De toetsen hierboven staan er en zijn groen; de PR noemt dat de eerste zonder de fix faalde.
- `grep -rn 'project_name=""' operations-manager/python/opi` geeft niets meer.
- `cd operations-manager/python && uv run ruff check . && uv run ruff format --check . && uv run pyright` is schoon, en de bestaande testsuite is groen.
