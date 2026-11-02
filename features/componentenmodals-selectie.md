# Een selectie in de componentenmodals

## Wat het is

Een lijstmodal toont normaal de hele lijst. Met een selectie toont hij alleen de
componenten die je aanklikte. Bij een project met tien componenten is dat het verschil
tussen tien formulieren en één.

De selectie staat in de URL van de modal:

```
GET /projects/{project}/modal-wizard/modal-edit-components?components=web
GET /projects/{project}/modal-wizard/modal-edit-deployment-0?components=web,api
```

Zonder `?components=` doet de modal wat hij altijd deed: de hele lijst.

## Twee soorten component, twee modals

Dat zijn twee verschillende dingen, en de modal die je krijgt hangt af van waar je klikt.

| | de componentdefinitie | het component in een deployment |
|---|---|---|
| in het projectbestand | `components[]` | `deployments[].components[]` |
| wat er staat | naam, type, poorten, resources, paden, diensten | het container image, en wat per deployment anders is (certificaat, bijlage) |
| de lijstmodal | `modal-edit-components` | `modal-edit-deployment-{index}` |
| de selectiesleutel | `name` | `reference` |
| waar je erop klikt | tabblad Componenten | tabblad Deployments, onder een deployment |

De env-vars stonden ook in deze modals; sinds RC-388 zitten ze daar niet meer, maar in
een eigen venster achter een verse step-up (zie
[omgevingsvariabelen-achter-step-up.md](omgevingsvariabelen-achter-step-up.md)).

Op het tabblad Deployments opent een klik op een component die deployment met alleen dat
component erin. Op het tabblad Componenten opent "Bewerken" op een componentkaart het
eigen bewerkscherm van dat component (`modal-edit-component-{index}`), dat er al was:
dat toont per definitie één component en draagt bovendien het toevoegpad en de
resource-intentie. De knoppen voor de hele lijst blijven staan.

## Hoe het werkt

Een flow die een lijst bewerkt, zegt zelf welke lijst dat is:

```python
COMPONENTS_SELECTABLE = ListSelectable("components", "name")

MODAL_EDIT_COMPONENTS_FLOW = FormFlow(
    ...,
    selectable=COMPONENTS_SELECTABLE,
)
```

Bij het openen van de modal wordt het projectbestand versmald tot de geselecteerde items
(`narrow_to_selection`), en pas daarna gesplitst over de secties. Daardoor dragen zowel
`step_data` als `base_data` alleen het getoonde deel.

Bij het opslaan zet `splice_selection` de getoonde items terug op hun eigen plek in de
lijst zoals die op dat moment in git staat. Wat de gebruiker weghaalde laat zijn plek
leeg, wat hij toevoegde komt achteraan, en de niet-getoonde items staan er nog precies zo
als ze stonden.

Dat laatste is de reden dat het zo gebouwd is: een niet-getoond component staat **niet**
in het formulier, ook niet als verborgen veld. Het kan dus niet overschreven worden door
een stale waarde uit de sessie, en het kan ook niet als "weggehaald" tellen omdat het
formulier het niet terugstuurde.

## Weigeringen

| situatie | antwoord |
|---|---|
| een naam die niet in het project staat | 404 bij het openen, met de naam erin |
| een selectie op een flow zonder `selectable` | 400 |
| een geselecteerde naam is tijdens het bewerken uit het project verdwenen | de opslag wordt geweigerd met de uitleg dat het scherm opnieuw geopend moet worden |

Die laatste is dezelfde regel als `guard_target_still_points_at_the_same_item`: liever
niets schrijven dan iets schrijven over een item dat er niet meer is.

## De selectie in de modal aanpassen

Nog niet. Een filter dat items in het geopende formulier verbergt is precies de vorm die
dit ontwerp vermijdt: een verborgen rij die niet meegestuurd wordt leest bij het opslaan
als een verwijdering. En een selectie serverkant wijzigen betekent de modal opnieuw
openen, wat ongeopslagen invoer weggooit. De selectie wordt daarom bepaald door wat je
aanklikt.

## Waar het staat

| | |
|---|---|
| de eigenschap op de flow | `ListSelectable` in `opi/forms/visualizers/flows.py` |
| versmallen, terugzetten, parsen | `opi/forms/wizard/selection.py` |
| welke rij een ingediende rij is | `reeksrij-sleutel-in-de-inzending.md` |
| in de sessie | `WizardState.selection` |
| openen van de modal | `modal_wizard_init` in `opi/web/router_detail_edit.py` |
| opslaan | `apply_modal_edit` in `opi/forms/wizard/save.py` |
| de klik op het tabblad Deployments | `opi/templates_lotc/bg/_section-deployments.html.j2` |
| toetsen | `tests/forms/test_component_modal_removal_and_selection.py`, `tests/forms/test_modal_noop_roundtrip.py` |
