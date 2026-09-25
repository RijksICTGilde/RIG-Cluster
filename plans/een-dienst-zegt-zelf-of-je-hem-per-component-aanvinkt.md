# Een dienst zegt zelf of je hem per component aanvinkt

Gemeten tegen `forgejo/main` op f1092713d.

## Het probleem

De keuzelijst met diensten op een component toont bijna alles wat het project heeft aangezet. Ze stelt één vraag aan de dienst, `component_selection_follows_config`, en die dekt precies één geval: image-registries, waar het eigen keuzeveld de selectie is. Voor alles daarbuiten geldt nog steeds "staat het in de projectlijst, dan staat het in de lijst".

Gemeten op main met een projectlijst van negen diensten levert de componentkeuze dit op:

```
publish-on-web, keycloak, postgresql-database, send-email, sleep-mode, invite, cross-domain-access, vlam
```

Vier daarvan horen er niet. Het vinkje doet bij `sleep-mode`, `invite` en `cross-domain-access` aantoonbaar niets:

- Geen van drieën heeft een `manifest_secret_class` of overschrijft `contribute_manifest_context`, dus `contributes_to_manifests()` is onwaar en ze zitten niet in `manifest_services()`. De componentpoort in `collect_manifest_contributions()` draait voor hen nooit.
- De twee generieke plekken in `project_manager.py` die de componentdienstenlijst lezen noemen specifiek postgres en authorization-wall.
- `allows_implicit_project_selection` staat bij alle drie op False, dus een vinkje kan ze ook niet op projectniveau inschrijven.
- De enige plek waar zo'n vinkje registreert is `selected_services()`, dat de unie neemt van de projectlijst en alle componentlijsten. Omdat deze drie altijd al in de projectlijst staan, verandert die unie niets.

Sleep-mode is het duidelijkste geval: `config_layers()` geeft exact `[PROJECT]`, en de dienst bepaalt zelf met `match:` welke deployments slapen. Het enige dat buiten die scope staat is `deployments[].sleep`, en dat is toestand, apart gedeclareerd via `deployment_runtime_keys()` juist omdat het anders voor configuratie wordt aangezien.

De vierde, `vlam`, is een ander verhaal en staat verderop.

## Het model

1. **Projectniveau: altijd.** Elke dienst die een project gebruikt staat in de root `services:`. Dat zegt alleen: dit project gebruikt deze dienst. Geen veld, geen keuze.
2. **Componentniveau: een ja of nee per dienst.** Ja betekent dat elk component hem zelf aan- en uitzet. Nee betekent dat het bij de projectkeuze blijft en de dienst zelf bepaalt waar hij werkt.
3. **Configuratie staat hier los van** en kan op alle vier de lagen zitten. Dat werkt al en daar raakt dit plan niets aan.

## De wijziging

### 1. De vraag die nog niet gesteld wordt

Op `ServiceDefinition`:

```python
selectable_per_component: bool = True
```

Met een standaardwaarde, want het antwoord ligt bij de meeste diensten voor de hand. Vandaag is `binding` verplicht zonder standaard, en dat is precies hoe de drift ontstond: vijf diensten vulden iets in omdat het veld nu eenmaal moest. Uitzonderingen declareren, de rest erft.

Wordt het ooit rijker dan ja of nee, dan verandert het veld van vorm zonder dat de diensten die niets declareerden worden aangeraakt. Moet het antwoord ooit afhangen van cluster of project, dan is `available_on_cluster()` het bestaande patroon voor een dynamische vraag.

### 2. Hoe dit zich verhoudt tot `component_selection_follows_config`

Die bestaat al en blijft. Het zijn twee verschillende vragen die allebei hetzelfde vinkje weghalen:

| declaratie | betekent | voorbeeld |
|---|---|---|
| `component_selection_follows_config` | er is wél een componentkeuze, maar die zit in het eigen configuratieveld met zijn "geen"-optie | image-registries |
| `selectable_per_component = False` | er is geen componentkeuze; de dienst bepaalt zelf waar hij werkt | sleep-mode |

Ze hebben verder verschillende gevolgen. De eerste stuurt ook het leeggooien aan (`_prune_service_map_entry`, `ServiceAdapter.remove_service_config`) en het overslaan in `_strip_removed_services_from_components`. De tweede stuurt waar de manifestbijdrage zijn selectie leest. Ze mogen dus niet samengevouwen worden.

Wat wél samen moet: de picker mag ze niet als twee losse `continue`-takken uitvoeren. Eén afgeleide vraag, zodat er één plek is waar de twee feiten samenkomen:

```python
def offers_component_checkbox(service) -> bool:
    return service.definition.selectable_per_component and not service.component_selection_follows_config
```

### 3. Wat eruit gaat

`ServiceBinding` met zijn drie waarden, `BINDING_LABELS`, de nooit gezette `filter_binding`, `is_component_service()`, `is_deployment_service()` en `manifest_activated_by_project`.

`ServiceBinding.DEPLOYMENT` beschreef nooit een selectie. Er bestaat geen dienstenkeuze per deployment. De waarde zei "gedeeld per deployment", en dat is een eigenschap van de voorziening, geen antwoord op de vraag wie hem aanvinkt.

### 4. Dat ene feit dat wel moet blijven

"Gedeeld per deployment" is echte informatie voor een gebruiker: één database voor alle componenten van een deployment. Die zin zit vandaag in de enumwaarde en zou met `ServiceBinding` verdwijnen. Hij krijgt zijn eigen drager op de definitie:

```python
shared_per_deployment: bool = False
```

`True` voor `postgresql-database`, `namespace-postgresql-database`, `redis`, `namespace-redis` en `minio-storage`: de diensten die één voorziening per deployment leveren waar alle aangevinkte componenten dezelfde inloggegevens van krijgen.

Twee velden in plaats van één enum is hier geen splitsing om de splitsing: het zijn twee verschillende feiten die in één waarde waren gepropt, en dat samenvouwen is precies waar dit plan over gaat.

### 5. De consumenten

- De componentkeuze: `offers_component_checkbox()`.
- `collect_manifest_contributions()`: leest de projectlijst als `selectable_per_component` onwaar is, anders de componentlijst. Daarmee vervalt `manifest_activated_by_project`.
- De dienstkaart: "Per component aan te zetten" of "Geldt voor het hele project", en daarnaast "Gedeeld per deployment" wanneer `shared_per_deployment` waar is. Die twee zinnen staan los van elkaar en gelden bij postgres allebei.

### 6. De waarden

`selectable_per_component = False` voor `sleep-mode`, `invite`, `cross-domain-access`, `deployment-health` en `resource-tuning`. De laatste twee zijn `kind=SYSTEM` en verschijnen toch nergens; ze krijgen de waarde omdat hij eerlijk is.

Al het andere blijft op de standaard, inclusief de verborgen varianten: `namespace-postgresql-database` wordt wel degelijk per component aangevinkt (`algor-odc/component-1` doet het, `component-2` en `-3` niet). `hidden` slaat alleen op de projectkaart.

`cross-domain-access` krijgt `False` om een andere reden dan sleep-mode. Zijn componentkeuze bestaat wel, maar die zit in de regel zelf (`to.component`, `from.component`) en dat is configuratie. Een vinkje voegt er niets aan toe. Aan de dienst zelf verandert niets.

### 7. Vlam wordt een echte componentdienst

Vlam is de enige dienst met `manifest_activated_by_project = True`. Gevolg: de projectkeuze alleen geeft elk component van elke deployment de VLAM-variabelen, en de egress-NetworkPolicy selecteert de hele deployment.

Gemeten in productie op `bouwm-6gn`: `component-2` vinkt vlam aan, `component-1` niet, en toch draagt `main-component-1` gewoon `VLAM_API_URL` en valt het onder `vlam-main-network-policy` met podSelector `{deployment: main, project: bouwm-6gn}`.

Toegang hoort per component. Dus:

- `manifest_activated_by_project` weg, zodat de variabelen, de `hostAliases` en de CA-mount alleen op aangevinkte componenten landen.
- De NetworkPolicy per aangevinkt component in plaats van per deployment, met `pod_selector {app: <deployment>-<component>}` en bestandsnaam `<deployment>-vlam-<component>-network-policy`. Dat is de vorm die send-email al heeft.
- `_components_using_service()` staat nu privé in `send_email`. Met een tweede gebruiker hoort hij in `base.py`.
- De oude `<deployment>-vlam-network-policy` verdwijnt vanzelf, want de prune werkt op bestandsnaam.

### 8. De toets die het bestendig maakt

Een test over de hele catalogus: `selectable_per_component is False` betekent geen `COMPONENT`- en geen `DEPLOYMENT_COMPONENT`-configuratie, en geen vinkje in de componentkeuze. En de omkering: wie configuratie op een van die twee lagen draagt, moet de standaard houden.

Plus de regressietest die nu zou falen: een componentkeuze op een project met `sleep-mode`, `invite` en `cross-domain-access` levert die drie niet op, en `image-registries` blijft uitgesloten zoals `test_image_registries_component_choice.py` al vastlegt.

Dit is het punt van de hele wijziging. Zonder die test staat dit over een half jaar opnieuw scheef.

## Wat er in productie verandert

Alleen `bouwm-6gn/main-component-1`: dat verliest `VLAM_API_URL` en de egress-regel naar VLAM, die het nooit had aangevraagd. Dat is de bedoeling van de wijziging, maar het is een project van iemand anders. Melden voordat het uitrolt.

`tvas-7pb` verandert niet, dat heeft één component dat alles aanvinkt. Geen projectbestand hoeft aangepast: beide vlam-afnemers hebben het vinkje al op het juiste component staan.

De dode vinkjes van `sleep-mode`, `invite` en `cross-domain-access` blijven in de bestaande projectbestanden staan. Ze deden niets en blijven niets doen; ze verdwijnen zodra iemand dat component opnieuw opslaat.

## Tekst bijwerken

`instructions/services.md` noemt sleep-mode nog als voorbeeld van `hidden=True` (regel 295) en dat klopt allang niet meer. Verder gaat de tabel bij "Binding is not a config layer" na deze wijziging over `selectable_per_component`, en hoort het nieuwe veld naast `component_selection_follows_config` beschreven te worden, met het verschil tussen die twee in één alinea.

## Wat hier NIET in zit

- **De cascade.** Cross-domain-access is de enige dienst met echte gelaagde configuratie: een projectregel geldt voor elke deployment, en een deploymentregel met dezelfde naam patcht hem veld voor veld, met een `disabled` om hem daar uit te zetten. Wat je niet invult erft de projectregel. Dat werkt en is netjes gedeclareerd, maar het is per dienst gebouwd; er is geen gedeelde resolver. Geen acuut probleem, dus niet nu.
- **Vlam per component als ontwerp.** Dit plan verplaatst alleen de bestaande werking naar het component. Of er meer per component moet gaan gelden dan de variabelen en de egress-regel, is een aparte vraag.
