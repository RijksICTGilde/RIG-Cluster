# Een project als geheel opvragen

Status: plan, 10 augustus 2026. Antwoord op de RFC van zad-cli over `GET /api/v2/projects/{project_name}`. Alle getallen gemeten op `operations-manager/python` op branch `naar-het-nieuwe-componentensysteem`.

## De vraag

De CLI kan een project muteren maar niet tonen. Gevraagd wordt één endpoint dat teruggeeft hoe een project er nu uitziet: componentdefinities, deployments met hun images, en per gebruikte dienst de configuratie op de laag waar hij staat. Doel is `zad project describe`, voor een mens in tabelvorm en voor een agent als JSON.

## Wat er nu echt is

Het volledige v2-oppervlak, uitgelezen uit de routedecorators in `opi/api/v2/router.py`: **22 routes, waarvan 8 GET.**

| Methode | Pad | Handler |
|---|---|---|
| GET | `/projects` | `list_projects_v2` |
| GET | `/projects/{p}/deployments` | `list_deployments_v2` |
| GET | `/projects/{p}/deployments/{d}` | `get_deployment_v2` |
| GET | `/projects/{p}/pending-rollout` | `pending_rollout_v2` |
| GET | `/projects/{p}/services/{service}/config` | `get_service_config_v2` |
| GET | `/services` | `list_configurable_services_v2` |
| GET | `/services/{service}` | `describe_service_v2` |
| GET | (schemas) | `list_database_schemas_v2` |

De vier gaten uit de RFC, nagelopen:

1. **`GET /api/v2/projects/{project_name}` bestaat niet.** Bevestigd.
2. **Niet te zien welke diensten een project gebruikt.** Bevestigd. `/projects/{p}/services` bestaat wel als pad, maar alleen als POST (`add_service_v2`). Er is geen GET.
3. **Componentdefinities zijn niet leesbaar.** Bevestigd. `/projects/{p}/components` is POST-only (`add_component_v2`), plus een PATCH per component. Geen GET.
4. **De values-diensten zijn schrijf-only.** Half waar, en de afwijking verandert de aanpak.

## Correctie op gat 4, en waarom die de aanpak vereenvoudigt

Er zijn helemaal geen values-endpoints. `user-env-vars`, `aliases` en `attachments` hebben geen eigen routes in de v2-API, ook geen POST of DELETE. Wat er is: `ADD_COMPONENT_VALIDATORS` in `opi/api/validation.py:60-67` accepteert precies zes velden op de component-write:

```python
ADD_COMPONENT_VALIDATORS = {
    "name": ..., "image": ..., "path": ...,
    "cpu_limit": ..., "memory_limit": ...,
    "env_vars": COMPONENT_USER_ENV_VARS_EDITABLE,
}
```

Dus `env_vars` gaat mee in de component-write en is daarna nergens te lezen. `aliases` en `attachments` zijn via de API noch te schrijven noch te lezen; die komen alleen via de portal binnen.

Daarmee valt gat 4 samen met gat 3: **één component-GET lost beide op.** Er hoeven geen drie values-endpoints te komen, want er is niets om ze symmetrisch mee te maken.

Wel een verwachting bijstellen: de RFC noemt de component-GET "spiegel van de bestaande POST". Dat is hij niet, want de POST kent `type`, `ports`, `root` en `services` niet. Een GET mag meer teruggeven dan de POST accepteert, en dat hoort hij hier ook, maar het is uitbreiden en geen spiegelen.

## De echte kostenpost die de RFC onderschat: de geheimen

De RFC schrijft dat env-vars "als namen terug horen te komen, niet als waarden", alsof dat een filterregel is. Dat is het niet. `user-env-vars` staat opgeslagen als een **AGE-versleuteld blok**, opaak voor wie de sleutel niet heeft (`catalog/user_env_vars/config_model.py`). De namen zijn er niet uit te halen zonder te ontsleutelen. Namen teruggeven betekent dus dat dit leesendpoint secrets ontsleutelt om er metadata uit te halen, en elke fout in dat pad lekt waarden.

Het is wel te doen, want het pad bestaat al. `opi/web/router.py:1352-1396` ontsleutelt vandaag zowel component- als deployment-componentvariabelen met `decrypt_age_content` plus `validate_and_parse_env_vars`, en `templates/project-details/section-env-vars.html.j2:49` toont sleutel én waarde op de detailpagina. Dat pad zit alleen in de webrouter, dus het moet **geëxtraheerd** worden naar een gedeelde functie en niet gekopieerd, anders staan er twee ontsleutelpaden die uit elkaar gaan lopen.

Drie soorten inhoud met drie verschillende antwoorden, en de RFC behandelt ze als één:

- **`user-env-vars`**: waarde is per definitie geheim. Alleen namen.
- **`aliases`**: de sleutel staat plain in de mapping, de **waarde is per stuk AGE-versleuteld** (`catalog/aliases/config_model.py`). Het RFC-voorbeeld geeft `{"POSTGRES_HOST": "$DATABASE_SERVER_HOST"}` terug, dus mét waarde. Meestal is die waarde een platformvariabele en geen geheim, maar het model staat een geheim toe.
- **`attachments`**: de *koppeling* (`reference`, `provide-as`, `path`, `env-name`) is plain en onschuldig. De *catalogus* onder `services/attachments/data` bevat de base64-inhoud van het bestand en mag er nooit in.

**Het besluit dat dit plan neemt**, zodat het endpoint testbaar is in plaats van te wachten op een gesprek:

| Inhoud | In het antwoord |
|---|---|
| env-var namen | ja |
| env-var waarden | nooit |
| alias sleutels | ja |
| alias waarden | alleen als ze plain zijn opgeslagen, anders `"***"` |
| attachment-koppeling | ja |
| attachment-inhoud | nooit |
| `config/api-key`, `age-private-key`, `age-public-key` | nooit |

De aliasregel is uitvoerbaar met `is_age_encrypted()`, dat al bestaat, en houdt de bruikbaarheid overeind: een alias die naar `$DATABASE_SERVER_HOST` wijst is de hele reden dat je hem opvraagt.

Nog één ding over de toegangspoort, want dat is nu makkelijk verkeerd te lezen. `@validate_api_token` (`opi/api/endpoint_util.py:14`) doet één ding: de `X-API-Key` moet bij dit project horen. Er zijn geen rollen. Wie de sleutel heeft mag vandaag al env-vars schrijven via `POST .../components`, dus namen teruggeven is geen nieuwe bevoegdheid. Het argument om waarden weg te laten is dan ook niet privilege maar zichtbaarheid: dit antwoord belandt in terminalscrollback, in logs en in agenttranscripten. Ter vergelijking: `GET /projects` geeft de projectsleutel wél terug, maar alleen aan `admin` en `owner`, en die poort bestaat op `@validate_api_token` niet.

## Wat inderdaad goedkoop is

De RFC heeft gelijk dat dit vooral ontsluiten is.

`_collect_service_config` (`api/v2/router.py:1775-1810`) loopt voor één dienst alle vier de lagen af: project, component, deployment, deployment-component. Omkeren naar "alle diensten in één pas" is een kleine wijziging aan een functie die de laagkennis al bevat.

Eén valkuil daarbij, en het is precies de val waar een snelle hergebruikpoging in loopt. De functie slaat bewust bare selecties over:

```python
if config is None:
    return []   # een bare selectie is geen configuratie
```

Voor de vraag "welke dienst is hier geconfigureerd" klopt dat. Voor de vraag "welke diensten gebruikt dit project" is een bare selectie juist het antwoord: `- publish-on-web` zonder config betekent dat de dienst aanstaat. De nieuwe lezer heeft dus een andere regel dan de bestaande, en dat moet expliciet in de code staan in plaats van dat iemand de conditie stilletjes omdraait en het bestaande endpoint verandert.

Verder direct herbruikbaar: `PendingRolloutResponse` en `pending_rollout_v2` (`api/v2/router.py:569-610`) met `task_service.get_deferred_rollouts()`, en `get_project_store().get()`, dat de auth-decorator al aanroept, zodat het project al geladen is voordat de handler begint.

## Volgorde: de deelendpoints eerst, de samenvatting erbovenop

De RFC biedt het grote endpoint aan met een "kleiner alternatief" van drie losse endpoints als het te veel ineens is. Dat is een verkeerde tegenstelling: het zijn geen alternatieven maar lagen. Bouw de delen, en laat `GET /projects/{name}` een samenstelling zijn zonder eigen datalogica. Dan is er per gegeven één waarheid, en levert elke fase op zichzelf iets bruikbaars op.

**Fase 1: `GET /projects/{p}/services`.** Welke diensten dit project gebruikt, met per voorkomen de laag en de eventuele config. Haalt de 21 aanroepen weg die de CLI nu zou moeten doen. Verifieerbaar: voor `hwt-nqi.yaml` levert het publish-on-web op twee lagen (project met het `domains`-blok, component1 met `tls: standard`) en verder niets, en een bare selectie komt terug als gebruikt-zonder-config.

**Fase 2: `GET /projects/{p}/components`.** De componentdefinities: `name`, `type`, `ports`, `path`, `root`, `resources`, de dienstenlijst, de env-var-namen, de aliassen en de attachment-koppelingen, volgens de tabel hierboven. Hier zit het ontsleutelwerk, dus hier hoort de extractie van het env-var-leespad uit `web/router.py`. Verifieerbaar: een test die een project met versleutelde env-vars leest en faalt zodra er een waarde in het antwoord staat, plus een test die bewijst dat de detailpagina en het endpoint dezelfde functie gebruiken.

**Fase 3: `GET /projects/{p}`.** De samenstelling: projectkop, fase 1, fase 2, de bestaande deploymentlezer en `pending_rollout`. Geen eigen logica, alleen samenvoegen. Verifieerbaar: het antwoord is veldsgewijs gelijk aan wat de losse endpoints teruggeven, afgedwongen met een test die ze vergelijkt in plaats van beide met de hand te asserteren.

**Buiten scope:** `zad project describe` zelf. Dat is de CLI-kant en hoort in die repo.

## Aanscherping: status is tweeledig

Toegevoegd tijdens de uitvoering, en het wint van de rest van dit plan waar het ermee botst.

**(1) Draaistatus.** Fase 3 hergebruikt `DeploymentDetail` (`api/v2/models.py:213-258`) **in zijn geheel**: dus `status`, `sync_revision`, `last_synced_at` en `errors` met `ErrorCategory` en `explanation`. Het RFC-voorbeeld toont deployments met alleen `name`/`components`/`subdomain`/`urls` en laat precies dat weg; dat is een val, niet het ontwerp. Er hoort een test die faalt zodra een veld uit `DeploymentDetail` ontbreekt in het samengestelde antwoord.

**(2) Opbouwstatus.** Welke componenten er zijn, en welke diensten gebruikt worden mét hun configuratie op de laag waar die staat. Alle vier de lagen apart houden (project, component, deployment, deployment-component), elk voorkomen met zijn `target` plus component/deployment-identificatie. Nooit platslaan tot een dienst met één config, want dan is niet meer te zien of `tls: standard` voor het project geldt of voor een component.

**Eén autoriteit.** Het gelaagde services-blok is de waarheid; de kale namenlijst bij een component is een kruisverwijzing en draagt geen eigen config.

Env-var-namen wel, waarden nooit: dat stond al goed hierboven.

## Waar op te letten

**Zeg wat er wacht, en zeg het overal.** De RFC vraagt `pending_rollout` in de samenvatting en dat klopt: zonder dat beschrijft het antwoord het projectbestand en niet wat er draait. Maar dat geldt net zo goed voor de losse endpoints uit fase 1 en 2. Wie alleen de samenvatting van het waarschuwingsveld voorziet, bouwt de val in het endpoint dat de CLI straks het vaakst aanroept.

**Het projectbestand is de bron, niet het cluster.** Elk van deze endpoints leest via `get_project_store()`. Dat is een bewuste keuze en hij moet in de OpenAPI-omschrijving staan, want "wat er staat" en "wat er draait" lopen uiteen zodra iemand met `rollout=false` heeft opgeslagen.

**Geen extra schrijfoppervlak.** De verleiding bij een leesplan is om en passant `aliases` en `attachments` ook schrijfbaar te maken, want dan is het symmetrisch. Niet doen. Dat is een eigen ontwerpvraag over validatie en goedkeuring, en hij hoort niet in een leestaak binnen te sluipen.

**Ontsleutelen is geen filteren.** Als er één ding uit dit plan in de review moet worden nagelopen, dan is het dat het antwoord geen enkele ontsleutelde waarde bevat. Toets dat op de uitkomst en niet op de code: een test die het opgebouwde antwoord doorzoekt op de bekende plaintextwaarde is meer waard dan tien asserties op tussenfuncties.

## Afwijking bij de uitvoering

`root` bestaat niet meer op een v2-component. Het schema (`opi/schemas/project_v2.json`, `$defs/component`) kent het niet, en de enige verwijzingen ernaar staan in `opi/services/schema_migration.py`, die het v1-veld wegmigreert. Het is daarom niet in het component-antwoord opgenomen; alle andere velden uit fase 2 wel.
