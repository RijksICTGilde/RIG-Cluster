# Een rij in een reeks stuurt haar sleutel mee

## Wat het is

Een herhaalbare reeks (`WidgetType.SEQUENCE`) rendert per rij een stel velden met een
geïndexeerde naam: `components[0]/name`, `components[1]/name`. De server krijgt dus een
lijst rijen, en moet elke rij koppelen aan de rij die er in het projectbestand bij hoort:
de velden die het formulier niet beheert (het image, de dienstconfiguratie, de history van
de resources) komen uit die opgeslagen rij, en de readonly velden worden er na de merge uit
teruggezet.

Die koppeling liep tot RC-378 over de INDEX zodra de sleutel niet in de inzending stond. En
dat was precies het geval dat telt: de naam van een component is readonly in een
bewerkformulier, en een readonly veld rendert als `disabled`, dus de browser stuurt het
niet mee. Haalde de gebruiker een rij uit het MIDDEN van de lijst weg, dan schoof alles
erachter een plek op:

```
[web, alpha, beta, gamma], de gebruiker haalt 'beta' weg
na opslaan: web(8000), alpha(8001), beta(8003)
```

`gamma` was verdwenen, `beta` stond er nog met de poorten van `gamma`, en de deployment was
daar consequent op bijgewerkt. Stil, en geldig volgens het schema.

## Hoe het werkt

Elke gerenderde rij stuurt haar sleutel mee als verborgen veld, naast het pad dat de reeks
al meestuurt (zie `formulier-reeksen-niet-leegschrijven.md`):

```html
<input type="hidden" name="components[0]/name" value="web">
```

De sleutel is `reference`, en anders `name` (`IDENTITY_KEYS`). Hij wordt alleen getekend als
de rij hem niet al zelf post, dus voor een readonly sleutelveld. De verwerker paart daarop,
op elke plek waar hij paart: de merge met de opgeslagen rij, het terugzetten van de readonly
velden, en het ordenen van de velden binnen een rij.

De sleutel bepaalt alleen MET WELKE opgeslagen rij een rij hoort. Hij is geen waarde die de
inzending zet: het readonly veld wordt na de merge teruggezet uit de gepaarde rij, dus een
gewijzigde sleutel in de inzending kan een component niet omdopen. Kent de inzending een
sleutel die niet in de opgeslagen lijst voorkomt, dan valt de koppeling terug op de index,
zoals voorheen.

Een lijstmodal op een selectie paart om dezelfde reden op de sleutel in plaats van op de
positie (`splice_selection`): viel het eerste item van een selectie van twee weg, dan
schoven de niet-getoonde items een plek op.

## Wat je moet doen als je een reeks toevoegt

Niets. Het zit in de widget, niet in de sectie. Wel: geef een reeks waarvan de rijen een
identiteit hebben die identiteit in `reference` of `name`, en niet onder een eigen naam.
Een reeks zonder sleutel (een lijst poorten, een lijst paden) heeft er geen nodig; die
rijen zijn hun eigen waarde.

De `registries`-reeks van `image-registries` loste dit eerder al voor zichzelf op door de
slug als `WidgetType.HIDDEN` te renderen. Dat kan blijven staan: zo'n rij post haar sleutel
al, dus er komt geen tweede veld bij.

## Waar het staat

| | |
|---|---|
| de sleutel en de koppeling | `opi/forms/editables/sequence_identity.py` |
| het verborgen veld | `_identity_fields` in `opi/forms/widgets/fields.py`, gerenderd in `widgets/sequence_item_card.html.j2` en `widgets/sequence_item_inline.html.j2` |
| de koppeling in de verwerker | `_process_sequence_json` in `opi/forms/editables/processor.py` |
| een selectie terugzetten | `splice_selection` in `opi/forms/wizard/selection.py` |
| toetsen | `tests/forms/test_component_modal_removal_and_selection.py`, `tests/forms/test_wizard_selection_helpers.py` |
