# Een wizard heeft een basis en mutaties

## Wat het is

Een wizard levert twee dingen bij elkaar op: een **basis** en een reeks **mutaties**.

- De **basis** (`WizardState.base_data`) is wat er al was voordat de gebruiker begon.
  Bij een nieuw project is dat een skelet met de startwaarden die de eerste stappen
  nodig hebben; bij bewerken is het het deel van het projectbestand dat deze flow niet
  zelf bezit.
- De **mutaties** (`WizardState.step_data`, één per stap) zijn wat de gebruiker
  veranderde. Een stap bewaart alleen de velden die zijn eigen editables bezitten.

`get_merged_data()` is basis plus mutaties, in die volgorde. Create en edit verschillen
in wát de basis is, en verder in niets.

## De regel voor "ontbreekt"

> Een sleutel die niet in een inzending zit, is **ongewijzigd** — nooit verwijderd.
> Verwijderen gebeurt **expliciet**.

Een formulier verstuurt wat het rendert. Wat het niet rendert — een dichtgeklapte
sectie, een stap die de gebruiker nooit opende, een vergrendeld veld — komt binnen als
"er niet". Dat lezen als "de gebruiker heeft het weggehaald" is hoe er diensten uit
projectbestanden verdwenen.

Expliciet verwijderen kan op twee manieren:

| Soort veld | Hoe een verwijdering eruitziet |
|---|---|
| een gewoon veld | `CLEARED_FIELD`, een grafsteen die na het samenvoegen wordt opgeruimd |
| een selectielijst | het formulier bood de waarde aan en de gebruiker vinkte hem uit |

Dat tweede is wat `opi/forms/wizard/mutation.py` toevoegt. `apply_selection_mutation`
krijgt daarom de **aangeboden** verzameling mee: dat is wat "uitgevinkt" onderscheidt van
"nooit getoond". Een dienst die de kiezer niet toont (`hidden=True`, zoals
`namespace-postgresql-database`) kán niet zijn uitgevinkt en blijft dus staan — vóór deze
regel viel hij, met zijn hele configuratie, weg bij elke opslag van de dienstenstap.

## Vergrendeld is niet hetzelfde als niet-verstuurd

`disabled` in HTML betekent twee dingen tegelijk: niet aanpasbaar én niet versturen. Wij
bedoelen alleen het eerste. Een vergrendelde dienstkaart rendert daarom een gewone,
verstuurbare checkbox met `aria-disabled="true"`; het slot wordt bewaakt door

1. de browser: `wizard.js` draait het uitvinken van een vergrendelde kaart terug en zegt
   waarom;
2. de server: `apply_services_mutation` vult een vereiste dienst hoe dan ook aan.

De meereizende hidden input die dit eerder opving is weg. Die was een pleister op een
koppeling die er niet had moeten zijn, en hij leverde de dienst twee keer aan in de POST.

## Eén weg naar de dienstconfig

Dezelfde configuratie staat op twee plekken, afhankelijk van waar je bent: onder
`services` in een opgeslagen projectbestand, en onder de virtuele sleutel
`_services-config` tijdens de wizard. Een lezer hoort dat niet te hoeven weten.

- `smart_get_value` en `smart_path_exists` (in `opi/forms/editables/service_path.py`)
  zoeken allebei eerst onder de genoemde sleutel en dan onder de andere. Lezen alleen —
  een SCHRIJFactie blijft bij de sleutel die het pad noemt, anders landt een wijziging
  ergens waar het formulier hem niet terugleest.
- Het sleutelpaar zelf staat één keer, als `SERVICE_VIRTUALIZE` in
  `opi/forms/editables/editable.py`. De dienstpakketten gebruiken het om virtualisatie te
  VERKLAREN en `service_path` om het op te LOSSEN; die twee mogen niet uit elkaar lopen.
  Vijftien dienstpakketten schreven het paar eerder met de hand uit.

## Hoe je het gebruikt

Bij een nieuwe selectielijst in een formulier:

```python
from opi.forms.wizard.mutation import apply_selection_mutation, offered_selection_values

offered = offered_selection_values(section.editables, "mijn-lijst")
if offered is not None:
    resultaat = apply_selection_mutation(basis, ingezonden, offered)
```

`offered_selection_values` geeft `None` terug als de sectie het veld helemaal niet draagt.
Dat is geen lege verzameling: een stap die het veld niet toont, zegt er niets over, en dan
mag er niets uit verdwijnen.

Voor de projectbrede dienstenlijst is dat al gedaan: beide routers roepen
`apply_services_mutation(section.editables, yaml_data, submitted_yaml)` aan. Er is geen
voorwaarde op een sectienaam meer — het gaat erom of de inzending een dienstenlijst
draagt, niet hoe de stap heet.

## Testen

Zie `instructions/wizard-tests.md`. De vier bugs van 6 augustus 2026 zijn gedekt op
niveau 2 (`tests/forms/test_wizard_base_and_mutations.py`, zonder browser) en het
vergrendelde-dienst-geval ook op niveau 5
(`tests/e2e/test_wizard_locked_service.py`, Playwright) — dat laatste is het enige niveau
dat ziet wat de browser werkelijk verstuurt, en daar zat de bug.

## Afhankelijkheden

- `opi/forms/wizard/state.py` — `WizardState`, `get_merged_data`, `CLEARED_FIELD`
- `opi/forms/wizard/mutation.py` — de regel voor "ontbreekt"
- `opi/forms/editables/service_path.py` — één weg naar de dienstconfig
- `opi/forms/editables/editable.py` — `SERVICE_VIRTUALIZE`
- Verwant: [wizard-write-set.md](wizard-write-set.md) (wat een flow mag schrijven),
  [service-config-location.md](service-config-location.md) (waar een dienst geconfigureerd wordt)
