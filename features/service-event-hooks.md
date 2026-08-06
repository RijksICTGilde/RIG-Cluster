# Twee event-lijsten, één manier van inhaken

Een dienst haakt in met één decorator, en er zijn twee families events: `ActionEvent`
(er gebeurt iets, een dienst verandert toestand) en `UIEvent` (waar ben ik zichtbaar).
De registry indexeert die declaraties één keer, dus "wie luistert er naar X" is een
opzoeking en geen scan die per uitbreidingspunt opnieuw geschreven wordt.

Ingevoerd in RC-39. Vervangt `HookPoint` en `registry.services_for_hook()`.

## Waarom

Elk uitbreidingspunt was een methode op de `Service`-basisklasse plus een plek elders die
de registry scande op wie die methode overschreef. Twee bewerkingen buiten de dienst per
haak, en zes van die methoden hadden precies één bewoner: maatwerk op een gedeelde
basisklasse, geen contract.

Het bezwaar tegen een generieke dispatch was verlies van typecontrole. Dat klopte niet:
één payload-object per event mét overloads levert méér *afdwingbare* controle op dan losse
argumenten, want dit project draait pyright met `reportCallIssue` en `reportArgumentType`
uit -- die argumentenlijsten werden nergens nagekeken. Zie regel 3 hieronder voor wat er
vandaag wél en niet uitkomt.

## Zelf inhaken

```python
from opi.services.catalog.events import on
from opi.services.services_enums import ActionEvent, UIEvent

class SleepModeService(Service):
    @on(UIEvent.DEPLOYMENT_STATE)
    def report_sleep_state(self, ctx: DeploymentStateContext) -> list[DeploymentStateFact]:
        ...

    @on(ActionEvent.REDEPLOY)
    async def wake_on_rollout(self, ctx: RedeployContext) -> list[str]:
        ...
```

De methodenaam is vrij: hij beschrijft wat de dienst op dat moment doet. `order=`
(standaard 100) bepaalt de plek tussen de andere luisteraars van dát event.

## De twee families

|  | `ActionEvent` | `UIEvent` |
|---|---|---|
| Wat het doet | verandert toestand | geeft iets terug om te tonen |
| Vorm | `async` | synchroon |
| Muteert | `payload.project_data`, in place | niets |
| Committeert | **nooit** -- de aanroeper commit één keer voor de hele scan | n.v.t. |
| Faalt | zichtbaar via de uitkomst die de runner verzamelt | zichtbaar: geen sectie |
| Dispatch | `await service.handle_action(event, payload)` | `service.handle_ui(event, payload)` |

Eén enum voor allebei zou dat verschil verstoppen, en juist het commit-contract is het
soort ding dat stil sneuvelt zodra er een tweede bewoner bij komt. Twee diensten die
allebei committen geven twee commits en een lost update.
`tests/test_service_events.py` meet beide regels op de broncode van elke handler in de
catalogus: async-vs-synchroon, en geen `save_and_commit_project` in een actie-handler.

## De events

| Event | Payload | Geeft terug | Bewoners |
|---|---|---|---|
| `ActionEvent.AFTER_SYNC` | `DeploymentObservationContext` | `list[ObservationOutcome]` | resource-tuning |
| `ActionEvent.REDEPLOY` | `RedeployContext` | `list[str]` (opgeruimde toestand, in mensentaal) | sleep-mode, deployment-health |
| `UIEvent.PROJECT_SECTIONS` | `ProjectPageContext` | `list[DetailPageSection]` | keycloak, attachments, invite |
| `UIEvent.DEPLOYMENT_SECTIONS` | `DeploymentPageContext` | `list[DetailPageSection]` | metrics-scraper + elke back-upbare dienst |
| `UIEvent.DEPLOYMENT_STATE` | `DeploymentStateContext` | `list[DeploymentStateFact]` | sleep-mode, deployment-health |

Elk event noemt ook wat het doorloopt (`event.level`: project, deployment of component).

## Drie regels die het mechanisme dragen

1. **Een handler geeft een lijst bijdragen terug.** Elk event, elke handler. De dispatch
   plakt de bijdragen van álle handlers van een dienst aan elkaar, dus een dienst die een
   pagina-mixin draagt naast zijn eigen blok levert beide -- zonder dat de mixins via
   `super()` moeten samenwerken. Een mixin die dat vergat slikte vroeger stilletjes het
   blok van de ander in.
2. **Deelname is afgeleid, nooit dubbel verklaard.** De index komt uit de gedecoreerde
   methoden zelf. Een dienst kan niet als luisteraar in een lijst staan zonder het event
   te implementeren, en ook niet andersom.
3. **De payload is één object per event.** Een echt type, gekoppeld aan het event via de
   overloads op `handle_ui` / `handle_action`. Een verkeerde payload geeft
   `No overloads for "handle_ui" match the provided arguments` plus de regel welk type
   niet past.

   Nagemeten, en met een kanttekening: die fouten verschijnen alleen met
   `reportCallIssue` en `reportArgumentType` aan, en dit project heeft ze allebei uit --
   dus vandaag wordt hij hier onderdrukt, net zoals bij de losse argumenten die dit
   vervangt. Het verschil is dat de koppeling nu een echt type is: die controles
   aanzetten begint hem meteen af te dwingen, zonder verdere verbouwing. Dat aanzetten
   staat als apart punt op de lijst, precies zoals het plan zegt.

## Wie luistert er

```python
from opi.services.registry import listeners

listeners(UIEvent.DEPLOYMENT_STATE)                 # iedereen
listeners(UIEvent.PROJECT_SECTIONS, project_data)   # alleen de diensten die dit project gebruikt
```

Met `project_data` erbij blijft de lijst beperkt tot de diensten die het project echt
gebruikt -- wat een pagina wil. Zonder wordt iedereen gevraagd, wat toestand-events
willen: een dienst heeft iets vastgelegd in het projectbestand en dat moet opgeruimd
worden, ook als het project de dienst vandaag niet meer noemt (sleep-mode kan clusterbreed
aanstaan). Een dienst die niets vastlegde meldt niets, dus iedereen vragen kost niets.

## Wat het meetbaar oplevert

| | Voor | Na |
|---|---|---|
| Publieke methoden op `Service` | 29 | 27 (vijf haken weg, `handle_ui` / `handle_action` / `listens_to` erbij) |
| Methoden die generieke code bij naam aanroept om te haken | 5 | 0 |
| Plekken die bepalen wie er meedoet aan een haak | 4 (`services_for_hook`, `_HOOK_DEFAULTS`, twee eigen collectorlussen) | 1 (`listeners`) |
| Wat een zesde event kost buiten de dienst | een methode op de basisklasse + een scan | een enum-lid + een payload-type |

Eerlijk erbij: het aantal plekken dat over `SERVICES` itereert blijft 20
(`grep -rE 'SERVICES\.(values|items)\(\)|for .* in SERVICES\b' opi/`). Die twintig gaan
grotendeels over configuratie en manifesten, niet over haken; van de haakkant zijn ze
opgegaan in één index. Het volgende event voegt er geen enkele aan toe, en dat is de
opbrengst die blijft.

## De inventarisatie: wat is wél een event en wat niet

Van de 29 publieke methoden op de basisklasse zijn er vijf een event geworden. De rest is
bewust gebleven wat het was -- uniformiteit is geen doel op zich, de winst zit in de
scanplekken en de eenmalige haken.

| Methode | Uitkomst |
|---|---|
| `detail_page_sections` | → `UIEvent.PROJECT_SECTIONS` |
| `deployment_page_sections` | → `UIEvent.DEPLOYMENT_SECTIONS` (was eenmalig bewoond) |
| `deployment_state` | → `UIEvent.DEPLOYMENT_STATE` |
| `observe_deployment` | → `ActionEvent.AFTER_SYNC` (was eenmalig bewoond) |
| `on_redeploy` | → `ActionEvent.REDEPLOY` |
| `config_editables`, `config_form_section`, `config_api_fields`, `config_layers`, `config_model_for`, `config_model_field_names`, `migrate_config`, `validate_config` | **blijft**: dit is de vórm van de configuratie, geen moment waarop iets gebeurt. 17 diensten hangen eraan en die werken |
| `config_component_layout` / `-visualizers`, `config_deployment_component_layout` / `-visualizers` | **blijft**: bouwstenen van één formulier, geen event. De twee deployment-component-varianten waren eenmalig bewoond, maar ze zijn de laag-tegenhanger van de component-varianten; ze eruit halen zou de laag onbeschrijfbaar maken |
| `config_approvals`, `approval_specs`, `get_approval` | **blijft**: een declaratie van wat goedkeuring nodig heeft, gelezen door de approval-code -- geen moment, geen payload |
| `provision`, `handle_service_removal` | **blijft** een actie, maar géén `ActionEvent`: ze maken echte resources aan en op en vallen dus buiten het contract "muteer `project_data`, commit niet". Ze eronder schuiven zou juist die regel verwateren |
| `contribute_manifest_context`, `build_secret_files`, `contribute_deployment_manifests`, `manifest_activation_types`, `contributes_to_manifests` | **blijft**: manifestgeneratie is een derde soort (produceren, niet tonen en niet muteren). `contribute_deployment_manifests` was eenmalig bewoond; een derde familie voor één bewoner is de fout die dit plan juist opruimt |
| `web_routers` | **blijft**: bedrading bij het opstarten, eenmalig verzameld, geen moment in een levensloop |
| `applies_to`, `listens_to` | **blijft**: filters die generieke code op de scan legt |

Twee dingen zijn hier bewust nog niet aangeraakt: de actieknoppen
(`definition.actions_provider`) en de uitleg, allebei UI-kandidaten. RC-38 verbouwt
juist de manier waarop een dienst zijn acties declareert; dezelfde plek tegelijk vanuit
twee kanten verbouwen levert alleen een merge-conflict op. Ze zijn de volgende groep.

## Waar het vandaan komt

- De decorator en de index per klasse: `opi/services/catalog/events.py`
- De twee enums en hun niveaus: `opi/services/services_enums.py`
- De dispatch met de overloads: `opi/services/catalog/base.py` (`Service.handle_ui`,
  `Service.handle_action`, `Service.listens_to`)
- De luisteraarsindex: `opi/services/registry.py` (`listeners`)
- De runners: `opi/services/redeploy.py`, `opi/services/deployment_observation.py`,
  `opi/services/deployment_state.py`, en de twee sectiecollectors in `registry.py`
- Tests: `tests/test_service_events.py`, plus `tests/test_redeploy_hook.py`,
  `tests/test_deployment_state.py`, `tests/test_deployment_observation.py`,
  `tests/test_service_detail_sections.py`, `tests/test_service_deployment_sections.py`
