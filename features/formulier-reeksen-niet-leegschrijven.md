# Een reeks die niet op het scherm stond wordt niet leeggeschreven

## Wat het is

Een formulier post alleen wat het rendert. Voor een herhaalbare reeks (`WidgetType.SEQUENCE`)
levert dat twee toestanden die er in de inzending identiek uitzien:

1. de gebruiker heeft de laatste regel verwijderd -- er zijn nul items;
2. deze sectie ging niet over die lijst -- er staat niets over in de inzending.

De verwerker maakte daar tot RC-79 in beide gevallen `[]` van. Voor geval 1 is dat wat de
gebruiker vroeg; voor geval 2 is het gegevensverlies. Zo zijn in productie
`additional-clients`-vermeldingen uit projectbestanden verdwenen, zonder foutmelding en
zonder zichtbaar leeg veld -- het veld bestond op het scherm gewoon niet.

Elke gerenderde reeks stuurt nu een verborgen veld met haar eigen pad mee. Daarmee zegt het
formulier zelf wat het tekende, en is het verschil tussen "leeggemaakt" en "ging er niet over"
een gegeven in plaats van een gok.

## Hoe het werkt

`opi/templates_lotc/widgets/sequence.html.j2` rendert per reeks:

```html
<input type="hidden" name="_gerenderde-reeksen[]" value="<pad van de reeks>">
```

De haakjes vallen weg in `static/js/json-enc.js`, zodat meerdere reeksen als lijst aankomen --
dezelfde route die `services[]` al loopt. Het pad is het formulierpad: voor gevirtualiseerde
dienstconfiguratie dus `_services-config/keycloak/config/additional-clients`, niet het
yaml-pad.

`opi/forms/editables/rendered_sequences.py` leest het terug:

| Situatie | Wat de verwerker doet |
|---|---|
| pad staat in de lijst, geen items | schrijft `[]` (of verwijdert de sleutel bij `remove_when_none`) |
| pad staat er niet in, geen items | schrijft niets; de opgeslagen waarde blijft staan |
| het veld ontbreekt volledig in de inzending | oud gedrag, ongewijzigd |

Die derde regel is er voor de eindinzending van de wizard: die geeft de samengevoegde
projectgegevens door als "inzending", kent geen formuliervelden, en heeft de lijsten al in de
juiste toestand staan.

Dezelfde regel geldt bij het snoeien van een rij in `_process_sequence_json`: een genest
reeks-kind dat voor die rij niet getekend is, wordt niet uit het origineel gesnoeid. Anders
verdween de lijst alsnog langs de andere kant, want het kind schrijft hem niet terug.

## Wat je moet doen als je een reeks toevoegt

Niets. Het merk zit in de widget, niet in de dienst. Wel geldt de tweede helft van dezelfde
les:

**Een editable die in `editables` van een sectie staat moet ook in haar `layout` staan.**
Staat hij er niet in, dan telt hij mee voor wat de stroom mag schrijven maar toont het
formulier hem nooit. `tests/test_service_config_layout_coverage.py` loopt datagedreven over de
hele catalogus en faalt op elke dienst waar die twee lijsten uit elkaar lopen.

## Toetsen

- `tests/test_service_config_sequence_preservation.py` -- meet op de OPGESLAGEN gegevens, via
  de hele keten (verwerker -> `_extract_section_data` -> `get_merged_data` ->
  `apply_write_paths`). Een toets die alleen de merge-functie aanroept had dit nooit gevonden:
  die doet precies wat zijn docstring belooft.
- `tests/test_modal_edit_nondestructive.py::TestDienstconfiguratieLijsten` -- datagedreven over
  de catalogus, per lijstveld beide richtingen.
- `tests/test_service_config_layout_coverage.py` -- de poort op de kloof tussen `editables` en
  `layout`.

## Grens

De opslagmontages (`components[*]/services{X}/config`) hangen onder de dienstSELECTIE van het
component: staat de dienst aan, dan tekent het formulier de lijst. "Wel gekozen, niet getekend"
is geen toestand die het formulier kan maken. Wat daar wel kan spelen is de overlay van de
componentenrij zelf -- de `services`-lijst wordt in zijn geheel vervangen door wat de inzending
draagt -- en dat is een eigen mechanisme dat hier niet bij hoort.
