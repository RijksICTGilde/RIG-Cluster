# Systeemdiensten met een gebruikersinterface

Wat het is: `ServiceKind.SYSTEM` -- "draait altijd, staat nooit in de lijst" -- kan sinds
RC-25 ook een *gebruikersinterface* hebben. `resource-tuning` bewees de hoofdloze helft van
dat begrip; `user-env-vars` en `aliases` bewijzen de andere.

Waarom dat nodig was: eigen omgevingsvariabelen en aliassen gedragen zich precies als een
dienstconfig -- ze bestaan op twee lagen met een samenvoeging ertussen, dragen versleutelde
waarden, horen op bepaalde plekken in de UI, en hebben validatie nodig -- maar stonden als
kale eigenschappen in het schema, zonder configmodel, schemafragment, editable met
validator of validatie bij het opslaan. Een gebruiker moet ze alleen niet hoeven aanvinken,
en dat bezwaar verdwijnt met `SYSTEM`.

## Wat een systeemdienst met UI anders doet

| | Gebruikersdienst | Systeemdienst met UI |
|---|---|---|
| Keuzekaart in de wizard | ja | nooit (`kind=SYSTEM` houdt hem uit de picker) |
| Vermelding in `services:` | ja | nooit |
| `applies_to()` | alleen als het project hem koos | altijd `True` |
| Fieldset in het componentformulier | `depends_on="services"` + `show_when` | onvoorwaardelijk |
| Configmodel, schemafragment, editables, formuliersecties | ja | ja, identiek |

De onvoorwaardelijke fieldset is het enige echte verschil in de formulierlaag: een
fieldset die zich verstopt tot de dienst is aangevinkt zou bij een systeemdienst nooit
verschijnen.

## `owned_property`: een dienst die een gewone eigenschap bezit

Normaal staat de config van een dienst in een `services:`-lijst
(`components[*]/services{metrics-scraper}/config`). `user-env-vars` en `aliases` bezitten
een *gewone eigenschap* van het component: `components[*]/user-env-vars`,
`components[*]/aliases` en `deployments[*]/components[*]/user-env-vars`, precies waar die
altijd al stonden. Er verandert dus niets in enig projectbestand.

Dat wordt gedeclareerd met `owned_property` op de dienst:

```python
class UserEnvVarsService(Service):
    service_type = ServiceType.USER_ENV_VARS
    config_model = UserEnvVarsConfig
    config_schema_version = "1.0"
    owned_property = "user-env-vars"
```

Wat die declaratie oplevert:

- `registry.property_owning_services()` verzamelt ze, en `validate_service_configs`
  loopt hun eigenschap langs op elke laag waar de dienst editables declareert. Daarvoor
  werden env-vars en aliassen bij het opslaan helemaal niet gevalideerd.
- De generieke config-API genereert er *geen* route voor. Die endpoint leest en schrijft
  precies het `config`-blok in een `services:`-lijst, en dat blok bestaat hier niet; een
  route zou een caller een blok laten schrijven dat nooit iets leest.

## Validatie

`user-env-vars` -- `UserEnvVarsConfig` accepteert drie vormen, want alle drie komen voor:
een AGE-**blok** (`-----BEGIN AGE ENCRYPTED FILE-----`), de opgeslagen vorm; platte tekst
in `KEY=value`- of YAML-formaat, wat het formulier post voor versleuteling en wat een
handgeschreven projectbestand kan bevatten; en de legacy mapping (`{API_KEY: secret}`)
van voor het veld een enkele string werd. De eenregelige `base64+age:...` is hier
uitdrukkelijk **niet** geldig: dat is de wachtwoordvorm elders in een projectbestand, niet
de vorm van dit veld. Ik heb dat onderweg zelf verkeerd gehad en teruggedraaid in
d53d3144; `test_the_stored_encrypted_shape_is_a_block_not_a_prefix` houdt het vast. Platte
tekst gaat door `validate_and_parse_env_vars`, dezelfde parser als de deploy-route, dus
wat hier valideert deployt ook. Een dollar in een wachtwoord is geen fout: de deploy-route
is daar bewust mild (`substitute_known_variables`), dus het model mag niet strenger zijn.

`aliases` -- `AliasesConfig` controleert de sleutels: een alias wordt een
omgevingsvariabele en moet een geldige naam hebben. Daarnaast eist de formuliervalidator
(`AliasMapValidator`) dat elke alias ergens naar verwijst (`$VAR` of `${VAR}`), want dat is
wat een alias is; een vaste waarde hoort bij de omgevingsvariabelen ernaast.

Die tweede regel staat bewust in de formulierlaag en niet in het model: een reeds
opgeslagen alias zonder verwijzing deployt prima (`substitute_variables` laat hem met rust),
dus hem op bestandsniveau afkeuren zou werkende projecten breken. Op het formulier heeft de
auteur de waarde nog voor zich.

En dus alleen dan. Aliaswaarden worden per waarde versleuteld onder hun eigen (dynamische)
naam, dus de redactie van de wizardsessie (`opi/forms/wizard/secrets.py`) vervangt ze een
voor een door de plaatshouder `__opi-redacted-secret__`. Die kwam terug in het veld en werd
gelezen als een constante zonder verwijzing, waarna elke volgende opslag van de
componenten-modal werd geweigerd -- voor elk component dat ooit een alias had opgeslagen,
ook voor een gewone gebruiker die iets heel anders wilde wijzigen. De validator slaat de
plaatshouder nu over, net als een AGE-blok: allebei waarden die de invuller niet op het
scherm heeft en dus niet bedoeld kan hebben. Bij opslag zet `restore_redacted_secrets` de
opgeslagen waarde terug.

## De haak voor de deployment-componentlaag

Tot RC-25 had `ConfigLayer.DEPLOYMENT_COMPONENT` als enige laag geen enkele dienst-eigen
haak: zijn velden werden met de hand geschreven in `forms/editables/fields/deployments.py`.
Er is nu een tegenhanger van de componenthaken:

```python
def config_deployment_component_visualizers(self): ...
def config_deployment_component_layout(self): ...
```

verzameld door `registry.deployment_component_service_editables()` /
`..._visualizers()` en `wizard_sections._service_deployment_component_layouts()`, in
`config_component_order`. `user-env-vars` is de eerste gebruiker.

## Waar aliassen en env-vars heen gaan

De richting is dat env-vars de rol van aliassen overnemen: sinds augustus 2026 lost
`substitute_known_variables` `$VAR` en `${VAR}` ook op in een user-env-var. Aliassen doen
nog één ding dat env-vars niet doen: ze falen *hard* op een onbekende verwijzing
(`substitute_variables`), terwijl een env-var mild is omdat een dollar daar vaak gewoon in
een wachtwoord staat. Dat verschil is bewust en moet ergens landen voordat aliassen
verdwijnen. Deze stap modelleert ze naast elkaar; het samenvoegen is een aparte beslissing.

## Een systeemdienst met UI toevoegen

1. `ServiceType`-lid, `ServiceDefinition` met `kind=ServiceKind.SYSTEM`, regel in
   `registry.SERVICES` (zoals elke dienst, zie `instructions/services.md`).
2. `config_model` + `config_schema_version`, en regenereer het fragment met
   `uv run python -m opi.services.config_schema`.
3. Bezit de dienst een gewone eigenschap in plaats van een `services:`-blok? Declareer
   `owned_property`.
4. Editables/visualizers/layout per laag, met een *onvoorwaardelijke* fieldset.
5. Draait de dienst geen verbindingsvariabelen naar buiten? Zet hem in `SKIP` in
   `scripts/generate_probe_spec.py`, anders faalt de drift-guard van de e2e-testimage.
