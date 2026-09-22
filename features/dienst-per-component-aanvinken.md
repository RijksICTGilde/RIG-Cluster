# Een dienst zegt zelf of je hem per component aanvinkt

De keuzelijst met diensten op een component toonde bijna alles wat het project had
aangezet. Ze stelde één vraag aan de dienst, `component_selection_follows_config`, en die
dekt precies één geval: image-registries, waar het eigen keuzeveld de selectie is. Voor al
het andere gold "staat het in de projectlijst, dan staat het in de lijst".

Dat leverde vinkjes op die aantoonbaar niets deden. Met een projectlijst van negen diensten
gaf de componentkeuze:

```
publish-on-web, keycloak, postgresql-database, send-email, sleep-mode, invite,
cross-domain-access, vlam
```

Drie daarvan deden niets. Bij de vierde, `vlam`, besliste het vinkje evenmin iets: de
projectkeuze deelde de dienst aan elk component uit.

## Het model

1. **Projectniveau: altijd.** Elke dienst die een project gebruikt staat in de root
   `services:`. Dat zegt alleen: dit project gebruikt deze dienst.
2. **Componentniveau: een ja of nee per dienst**, `selectable_per_component` op de
   `ServiceDefinition`. Ja (de standaard) betekent dat elk component hem zelf aan- en
   uitzet. Nee betekent dat het bij de projectkeuze blijft en de dienst zelf bepaalt waar
   hij werkt.
3. **Configuratie staat hier los van** en kan op alle vier de lagen zitten.

Een standaardwaarde, want het antwoord ligt bij de meeste diensten voor de hand. De
voorganger, `binding`, was verplicht zonder standaard, en zo ontstond de drift: vijf
diensten vulden iets in omdat het veld nu eenmaal moest.

## Wie geen vinkje krijgt, en waarom

| dienst | reden |
|---|---|
| `sleep-mode` | `config_layers()` geeft exact `[PROJECT]`; de dienst bepaalt zelf met `match:` welke deployments slapen |
| `invite` | een uitnodiging geldt voor het Keycloak-realm van het project |
| `cross-domain-access` | de componentkeuze bestaat wel, maar zit in de regel zelf (`to.component`, `from.component`) en dat is configuratie |
| `deployment-health`, `resource-tuning` | `kind=SYSTEM`, verschijnen toch nergens; ze krijgen de waarde omdat hij eerlijk is |

De verborgen varianten houden de standaard: `namespace-postgresql-database` wordt wel
degelijk per component aangevinkt (`algor-odc/component-1` doet het, `component-2` en `-3`
niet). `hidden` slaat alleen op de projectkaart.

## Twee declaraties, één vraag

`component_selection_follows_config` haalt hetzelfde vinkje weg om de andere reden: daar
is wél een keuze per component, maar die zit in het eigen configuratieveld. De picker
stelt er één afgeleide vraag over, `offers_component_checkbox(service)` in
`opi/services/catalog/base.py`. Waarom ze niet samengevouwen mogen worden, en wat er
verder aan hangt: `instructions/services.md`.

## Gedeeld per deployment is een derde feit

"Gedeeld per deployment" is echte informatie voor een gebruiker: één database voor alle
componenten van een deployment. Die zin zat in de oude enumwaarde en heeft nu zijn eigen
drager, `shared_per_deployment`, voor `postgresql-database`,
`namespace-postgresql-database`, `redis`, `namespace-redis` en `minio-storage`.

Hij staat los van de vraag wie de dienst aanvinkt, en bij postgres gelden ze allebei: elk
component zet hem zelf aan, en de componenten die dat doen delen één database. De
dienstkaart toont daarom twee chips.

## Vlam is een echte componentdienst geworden

Vlam was de enige dienst met `manifest_activated_by_project = True`. Gevolg: de
projectkeuze alleen gaf elk component van elke deployment de VLAM-variabelen, en de
egress-NetworkPolicy selecteerde de hele deployment, ook waar niemand erom vroeg (gemeten
in productie op `bouwm-6gn`, zie `features/vlam-service.md`).

Toegang hoort per component. Dus:

- de variabelen, de `hostAliases` en de CA-mount landen alleen op aangevinkte componenten;
- de NetworkPolicy staat per aangevinkt component, met `pod_selector {app:
  <deployment>-<component>}` en bestandsnaam `<deployment>-vlam-<component>-network-policy`.
  Dat is de vorm die send-email al had, en `Service.components_using_service()` is de
  gedeelde lezer waar die twee hem nu allebei uit halen;
- de oude `<deployment>-vlam-network-policy` verdwijnt vanzelf, want de prune werkt op
  bestandsnaam.

**Wat er in productie verandert:** alleen `bouwm-6gn/main-component-1` verliest
`VLAM_API_URL` en de egress-regel naar VLAM, die het nooit had aangevraagd. `tvas-7pb`
verandert niet (één component dat alles aanvinkt). Geen projectbestand hoeft aangepast:
beide vlam-afnemers hebben het vinkje al op het juiste component staan.

De dode vinkjes van `sleep-mode`, `invite` en `cross-domain-access` blijven in bestaande
projectbestanden staan. Ze deden niets en blijven niets doen; ze verdwijnen zodra iemand
dat component opnieuw opslaat.

## In de API

`GET /api/v2/services` en `GET /api/v2/services/{name}` droegen `binding` met de waarden
`component`, `deployment` en `project`. Dat veld is vervangen door de twee booleans
`selectable_per_component` en `shared_per_deployment`, die samen meer zeggen dan de enum
kon: postgres was `deployment` en dat las als "je kiest hem per deployment", wat hij nooit
deed.

## Bestanden

- `opi/services/services.py` -- `ServiceDefinition.selectable_per_component`,
  `ServiceDefinition.shared_per_deployment`
- `opi/services/catalog/base.py` -- `offers_component_checkbox`, `components_using_service`
- `opi/forms/visualizers/providers.py` -- `FilteredServiceOptionsProvider`, de componentkeuze
- `opi/manager/project_manager.py` -- `collect_manifest_contributions`, waar de selectie
  wordt gelezen
- `opi/services/config_location.py` -- `selection_labels`, de chips op de dienstkaart
- `tests/test_dienst_kiest_zijn_componentvinkje.py` -- de grendel over de hele catalogus
