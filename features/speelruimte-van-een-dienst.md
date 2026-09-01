# De speelruimte van een dienst

Wat het is: een dienst declareert per instelbaar veld **hoe ver een project mag gaan** --
de ondergrens, de bovengrens (of de toegestane verzameling), de standaardwaarde en de
lagen waarop het veld gezet mag worden. Uit die ene declaratie lezen alle drie de
consumenten: de samenvoeging van waarden over de lagen, de validatie van het
projectbestand, en het veld in de wizard.

Waarom dat nodig was: het patroon kwam twee keer los van elkaar boven. De connectielimiet
moet instelbaar worden per project en per deployment, en een project met een eigen
databasecluster moet zijn geheugen en volumegrootte kunnen zetten. In beide gevallen is de
vorm identiek -- de dienst bepaalt wat verantwoord is, het project kiest daarbinnen, en
iets moet controleren dat die keuze binnen de perken blijft -- en dat mechanisme bestond
niet. Een grens stond hardgecodeerd in `opi/connectors/postgres.py`, of als los getal in
een pydantic-model, of nergens: `storage: 500Gi` liep door tot het cluster het weigerde.

> Nog geen enkele dienst in de catalogus declareert iets. Dit is het gereedschap waarop de
> twee vervolgtaken bouwen; een dienst die niets declareert gedraagt zich precies zoals hij
> deed.

## De drie regels

**Een grens staat op precies een plek.** Het model, de wizard en de foutmelding putten
alle drie uit dezelfde declaratie. Zet je de bovengrens er als `le=100` naast in een
pydantic-veld, dan staan er binnen een maand twee getallen uiteen te lopen.

**Specifieker wint.** Deployment boven project, project boven de standaard van de dienst.
Een functie (`resolve_setting`), niet per dienst opnieuw bedacht.

**Wat een dienst niet declareert, is niet instelbaar.** Niet via de wizard, niet via de
API en ook niet door het met de hand in het projectbestand te zetten. Dat is meteen het
antwoord op "mag een gebruiker dit zelf zetten": alleen wat expliciet is opengezet.

## Declareren

In het servicepakket, op de dienst zelf:

```python
from opi.services.catalog.base import ConfigLayer, Service
from opi.services.catalog.config_settings import IntegerSetting, QuantityKind, QuantitySetting

class PostgresqlDatabaseService(Service):
    def config_settings(self):
        return (
            IntegerSetting(
                path="connection-limit",
                layers=(ConfigLayer.PROJECT, ConfigLayer.DEPLOYMENT),
                default=20,
                minimum=1,
                maximum=100,
                label="Connectielimiet",
            ),
            QuantitySetting(
                path="storage",
                layers=(ConfigLayer.PROJECT,),
                default="1Gi",
                minimum="1Gi",
                maximum="100Gi",
                kind=QuantityKind.MEMORY,
                grow_only=True,
                label="Opslag",
            ),
        )
```

`path` is puntgescheiden en ligt binnen het `config`-blok van de dienst, dus een genest
veld is `"resources.limits.memory"`. `layers` noemt elke laag waarop een project het veld
mag zetten; overal elders wordt het geweigerd. De standaardwaarde wordt bij het inladen
tegen de eigen grenzen gehouden -- een standaard buiten zijn eigen speelruimte is een fout
in de declaratie en geen verrassing in productie.

### Drie soorten grens, en geen vierde

| Soort | Klasse | Waarvoor |
|---|---|---|
| Geheel getal | `IntegerSetting(minimum=, maximum=)` | connectielimiet, `instances` |
| Kubernetes-hoeveelheid | `QuantitySetting(minimum=, maximum=, kind=)` | `storage`, `resources.*.cpu`, `resources.*.memory` |
| Gesloten verzameling | `ChoiceSetting(allowed=)` | `image`, `registry` |

Een hoeveelheid wordt **ontleed en als getal vergeleken**, nooit als tekst: `100m` en `2`
zijn allebei cpu, `512Mi` en `1Gi` allebei geheugen, en alfabetisch komt `1Gi` voor
`512Mi` terwijl hij vier keer zo groot is. `kind` kiest de parser, en beide parsers staan
in `opi/services/resource_analyzer.py` zodat die vraag op een plek wordt beantwoord.

`grow_only=True` op een hoeveelheid zegt dat het veld alleen omhoog mag. Een PVC kan niet
krimpen: een grens die alleen "tussen 1Gi en 100Gi" zegt laat een verkleining door die
daarna stilletjes niets doet of de uitrol laat vastlopen.

**Een veld dat een wijziging beoordeelt staat op precies een laag.** `grow_only` op meer
dan een laag wordt bij het inladen geweigerd, met een `ValueError` in de declaratie zelf.
De reden staat een stukje verderop bij de wijzigingsregel: die vergelijkt per configBLOK,
terwijl de effectieve waarde uit de meest specifieke laag komt die iets zegt. Staat
hetzelfde `grow_only`-veld op twee lagen, dan is 8Gi op projectniveau met een NIEUWE
deployment-override van 2Gi effectief een verkleining zonder dat er ook maar een blok
kleiner wordt -- dezelfde omzeiling als het veld weglaten, een laag lager opgeschreven.
Welke laag het is maakt niet uit, als het er maar een is; wie zo'n veld op meer dan een
laag nodig heeft, moet eerst de OPGELOSTE waarde per plek laten vergelijken, en dat is niet
gebouwd.

## Wat het mechanisme dan doet

**Samenvoegen.** `resolve_setting(setting, {laag: waarde})` geeft de effectieve waarde: de
meest specifieke laag die iets zegt, anders de standaard van de dienst. Een laag die de
dienst niet openzet wordt nooit geraadpleegd, dus een waarde die daar toch belandt kan
niets uitrichten.

**Valideren bij het inlezen.** Het chokepoint in `opi/manager/project_validation.py` loopt
alle vier de lagen langs; per configblok toetst `check_settings` elke gedeclareerde waarde
aan zijn declaratie. Buiten de speelruimte is een `ProjectIntegrityError` met een leesbare
zin -- bij het opslaan, niet pas bij het aanmaken van de resource:

```
Project 'demo': configuratie van service 'postgresql-database' op projectniveau is
ongeldig: 'storage' moet tussen 1Gi en 10Gi liggen; je gaf 50Gi.
```

**Een wijziging beoordelen.** `grow_only` gaat over een verandering, niet over een waarde,
dus die regel heeft de vorige versie van het bestand nodig. `ProjectStore` geeft die mee
(`validate_project_structure(data, previous=...)`), en `validate_service_setting_changes`
koppelt de blokken op hun PLEK -- het project zelf, een component bij naam, een deployment
bij naam -- zodat een verkleining niet met een ander component wordt vergeleken.

Beide versies worden **hetzelfde gelezen**, en dat begint bij het opzoeken van de vorige:
een blok wordt op zijn SLEUTEL (plek plus dienst) gepakt, niet op zijn waarde. Een dienst
die er kaal in stond -- geselecteerd, zonder configblok -- is daarmee een versie die op de
standaard staat, en geen afwezigheid. Daarna leest elke kant hetzelfde: de waarde als het
veld er staat, anders de standaard van de dienst. Een veld weglaten is dus dezelfde
verlaging als het veld expliciet verlagen, en dat is precies de bedoeling -- anders was de
regel te omzeilen door het veld (of het hele configblok) gewoon weg te laten, en een leeg
wizardveld doet dat.

De regel geldt in beide richtingen: het maakt niet uit aan WELKE kant het veld of het blok
ontbreekt. Deze drie wegen leveren dus dezelfde weigering op (en spiegelen ze de vorige
versie, dan ook). Ze staan hier op de projectlaag, maar de laag doet er niet toe: op een
dienst die het veld per deployment openzet leveren dezelfde drie wegen dezelfde weigering
per deployment op.

| van `storage: 5Gi` naar | uitkomst |
|---|---|
| `storage: 2Gi` | geweigerd -- "kan alleen omhoog" |
| `storage` weggelaten uit het configblok | geweigerd (effectief de standaard) |
| het hele configblok weg, dienst als kale string | geweigerd (idem) |
| de dienst helemaal niet meer gebruiken | toegestaan -- dat is een verwijdering |

Vergelijken per BLOK is hier hetzelfde als vergelijken per EFFECTIEVE waarde, en dat is
geen toeval maar de reden voor de eis hierboven: staat het veld op een laag, dan is het
blok op die laag het enige dat `resolve_setting` voor dat veld leest, dus een blok dat niet
krimpt is een waarde die niet krimpt. Zonder die eis houdt die gelijkstelling niet, en dan
is de grendel te omzeilen door de verlaging op de laag te schrijven die wint.

Alle plekken waar een serviceconfig kan wonen staan in een wandeling,
`iter_service_config_blocks`, die twee lezers bedient: de waardetoets gebruikt `where` en
`from_version`, de wijzigingstoets `location`. Daar horen ook de diensten bij waarvan de
config een component-EIGENSCHAP is (`user-env-vars`, `aliases`), en beide lezers toetsen
zo'n blok: op zijn grenzen (`_check_declared_settings`, naast het pydantic-model dat er al
op stond) en op zijn wijziging. Niet het een zonder het ander -- dan staat dezelfde
scheefte er weer, alleen omgekeerd.

Op zo'n eigenschapsblok **noemt de weigering de waarde niet**, om dezelfde reden waarom de
modelfout ernaast dat ook niet doet: `user-env-vars` is de eigen omgeving van een component
en accepteert een platte `dict[str, str]`, dus een waarde op een gedeclareerd pad kan daar
een geplakt geheim zijn -- en die zin gaat zowel het centrale log in als het antwoord aan de
aanroeper. De weigering wordt daar uit de DECLARATIE opgebouwd ("`'X'` valt buiten zijn
speelruimte", plus de speelruimte). Voor een blok in een `services:`-lijst blijft de waarde
er wel in staan: daar is het de grens zelf die wordt teruggeciteerd.

**Het wizardveld bouwen.** `setting_field(setting, service, layer)` maakt de `Editable` +
`EditableVisualizer` uit de declaratie: het yaml-pad via `config_path`, de invoercontrole
uit dezelfde `check` als de validatie, de helptekst uit `latitude()` en de prefill uit de
standaard. Het scherm kan dus niets beloven wat de save weigert.

```python
from opi.forms.visualizers.config_setting_fields import setting_field

VERBINDINGEN = setting_field(
    get_service(ServiceType.POSTGRESQL_DATABASE).config_setting("connection-limit"),
    ServiceType.POSTGRESQL_DATABASE,
    ConfigLayer.PROJECT,
)
```

Een `ChoiceSetting` verdient een keuzelijst, en een keuzelijst heeft een `OptionsProvider`
onder een naam nodig; geef `widget=WidgetType.SELECT` mee met de provider die
`setting.allowed` aanbiedt. De validator is de gesloten verzameling hoe dan ook, dus het
veld wordt door de declaratie beoordeeld welke widget het ook draagt.

## Wat dit niet is

**Geen quotum.** Een bovengrens per veld voorkomt dat een project een onzinnige waarde
opgeeft. Hij voorkomt niet dat honderd projecten allemaal het maximum vragen. Voor gedeelde
middelen -- de verbindingen op de gedeelde database zijn daar het voorbeeld van -- is een
quotum per project een aparte vraag.

**Geen autorisatie.** Dit controleert of een waarde geldig is, niet of degene die hem
invulde dat mocht.

## Afhankelijkheden

- `opi/services/catalog/config_settings.py` -- de declaratietypes, de samenvoeging en de toetsen
- `opi/services/catalog/base.py` -- `Service.config_settings()` en `Service.config_setting(path)`
- `opi/manager/project_validation.py` -- de toetsing bij het inlezen en bij een wijziging
- `opi/forms/visualizers/config_setting_fields.py` -- het wizardveld uit de declaratie
- `opi/services/resource_analyzer.py` -- de twee hoeveelheid-parsers
- `tests/test_service_config_settings.py`
