# Schrijvers tegen het schema

Elke plek in de code die structuur in een projectbestand schrijft, wordt in de tests langs
dezelfde poorten gelegd als een echte save. En de lijst van die plekken staat in een test,
zodat een nieuwe schrijver niet stil kan ontstaan.

## Waarom dit er is

Twee keer landde dezelfde fout in productie:

| Wanneer | Wie schreef | Wat het schema niet kende | Gevolg |
|---|---|---|---|
| juni 2026 (`f071f10d`) | de registry-code | `registries[].secretName` | ALLE deploys van dp-bn7 stil geblokkeerd |
| 18 augustus 2026 (`c0fc23d1`) | de resource-tuner | `requests` in `resource-history-entry` | 25 waarschuwingen in de log, 25 formeel ongeldige bestanden |

Beide keren was de reparatie een schemaregel, en beide keren was de test daarbij een **met
de hand geschreven voorbeeld** van de nieuwe vorm. Zo'n voorbeeld bewijst dat het schema
toelaat wat de testschrijver bedacht -- niet dat het toelaat wat de code werkelijk
wegschrijft. Daar zat het gat: geen enkele test legde het resultaat van een schrijver langs
`validate_project_schema`.

De tweede helft van het antwoord op "waarom viel het niet op": validatie gebeurt **alleen bij
schrijven**, en op de programmatische schrijfpaden (`enforce_validation=False`) wordt een
schemafout gedegradeerd tot een waarschuwing in de log. Dat is een bewuste keuze -- hard
valideren blokkeerde in juni stil alle deploys van een project, en dat is erger -- maar het
betekent dat het enige signaal een logregel is die iemand moet zien langskomen.

## Wat er nu staat

### `tests/test_schrijvers_tegen_het_schema.py`

Draait de echte schrijver op een basisproject dat aantoonbaar geldig begint, en legt het
resultaat langs de twee synchrone poorten die `ProjectStore._validate` ook draait:
`validate_project_schema` (JSON-schema) en `validate_service_configs` (de pydantic-modellen
per dienst). De derde poort, `validate_project_structure`, staat er bewust niet in: die is
async en leest clusterconfiguratie, en de fouten die hij vindt (dubbele namen, hangende
verwijzingen) zijn niet de klasse die stil in 25 bestanden landt.

De sleuteltest is `test_de_tuner_laat_een_geldig_projectbestand_achter`. Haal `requests` weer
uit `project_v2.json` en die valt om met exact de productiemelding -- maar nu vanuit de kant
van de tuner, niet vanuit een voorbeeld.

### `tests/test_schrijvers_inventaris.py`

Twee grendels op de lijst zelf, allebei via de AST (een grep telt de naam in een docstring
mee en bewaakt dan iets anders dan hij zegt):

* **`INVENTARIS`** -- elke module die `save_and_commit_project` of een schrijfweg van de store
  aanroept, met wat hij schrijft en welke test dat resultaat controleert.
* **`HANDLER_MUTATOREN`** -- de zetters van `ProjectFileHandler`, die de meeste schrijvers
  werkelijk gebruiken.

Beide kanten zijn nodig. De zetter `append_deployment_component_resource_history` is generiek;
de vorm van het historie-item kwam van de tuner zelf. Een test op alleen de zetter had het gat
van augustus dus niet gevonden.

Komt er een schrijver bij, dan valt de grendel om met de vraag die twee keer niet gesteld is:
past wat jij schrijft in het schema, en legt een test dat vast?

## De inventaris

### Programmatische schrijvers (`enforce_validation=False`, dus stil bij een fout)

| Module | Schrijft | Gedraaid |
|---|---|---|
| `services/resource_tuning_service.py` | resources en `resources/history` op de deploymentcomponent, plus compactie | ja |
| `services/oom_watcher.py` | `disabled` / `disabled-reason` na image-pull-fouten | ja |
| `services/deployment_observation.py` | commit-punt voor de after-sync haken | ja (via de tuner) |
| `api/resource_router.py` | `disabled` / `disabled-reason` bij het opschonen van kapotte componenten | ja |
| `core/backup_tasks.py` | generatienummers per dienst, en een hele nieuwe deployment bij een kloon | ja |
| `api/restore_router.py` | generatienummers en de kloonstatus na een restore | ja |
| `services/catalog/sleep_mode/flow.py` | `deployments[].sleep`: stand, deadline, versleuteld wektoken | ja |
| `services/catalog/sleep_mode/scheduler.py` | dezelfde staat vanuit de nachtelijke veger | ja |
| `manager/keycloak_manager.py` | `services/keycloak/config/realms` | nee, zie hieronder |
| `manager/delete_project_manager.py` | haalt datzelfde realm-item er weer uit | nee, zie hieronder |

### Door de gebruiker gestuurde schrijvers (`enforce_validation=True`, dus luid bij een fout)

| Module | Schrijft | Gedraaid |
|---|---|---|
| `manager/project_manager.py` | vrijwel elk deel van het bestand | deels (registries, bijlagen) |
| `web/router_wizard.py`, `core/task_handlers_project.py`, `api/router.py` | het hele bestand zoals wizard/API het opleveren | ja (`test_create_project_api.py`) |
| `web/router.py` | de domeininstellingen van een deployment | ja |
| `web/router_detail_edit.py` | het deel dat de bewerkte stroom beslaat | nee (`tests/forms/`) |
| `web/router_approvals.py` | de goedkeuringsstaat van domeinen en subdomeinen | nee (eigen modeltests) |

### De twee niet-gedraaide schrijvers

De keycloak-manager bouwt zijn realm-item inline, midden in een methode die een levende
Keycloak nodig heeft; `delete_project_manager` haalt datzelfde item er weer uit. Dat mag
zolang hun vorm de poort niet kan raken, en dat is nagemeten:
het JSON-schema laat de config van een dienst vrij (`$defs/service-entry` beschrijft alleen
de omhulling), en `KeycloakRealm` staat met `extra="allow"` extra velden toe. Beide feiten
staan vastgepind in `test_de_keycloak_schrijver_kan_deze_klasse_fout_niet_maken`; verandert
er een, dan valt die test om en moet de schrijver alsnog gedraaid worden.

## Een schrijver toevoegen

1. Schrijf je code zoals altijd, en commit via `save_and_commit_project`.
2. `uv run pytest tests/test_schrijvers_inventaris.py` valt om en noemt je module.
3. Draai je schrijver in `tests/test_schrijvers_tegen_het_schema.py` en roep `poorten(...)`
   aan op wat hij oplevert (of op wat hij aan de gemockte save aanbood: `_aangeboden(save)`).
4. Zet je module in `INVENTARIS` met wat hij schrijft en de naam van die test.

Kun je je schrijver niet draaien (een levende Keycloak, een cluster), zet dan `gedekt_door`
op `None` en schrijf de reden erbij -- en pin die reden vast, zoals bij keycloak.
