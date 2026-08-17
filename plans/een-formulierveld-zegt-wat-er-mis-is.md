# Een formulierveld zegt wat er mis is

Status: plan, 11 augustus 2026. Afgesplitst van RC-70, die te vol liep. Twee dingen die allebei over de **tekst bij een formulierveld** gaan: de foutmelding die je niet ziet, en het label "Optioneel" dat staat waar het niet hoort.

## 1. De foutmelding is onzichtbaar

Je krijgt een rood kader en verder niets, dus je weet niet wat er mis is.

**Gemeten** in de aanmaakwizard, na een lege submit:

| | |
|---|---|
| `<nldd-form-field-error-text>` in de DOM | **2**, met de juiste tekst ("Dit veld is verplicht") |
| ouder | `<nldd-form-field>`, met `slot="errors"` |
| `display` | **none**, hoogte 0 |
| elementen met `aria-invalid="true"` | **0** |

De keten klopt dus tot aan het component: `lotc_forms/_forms.j2` (macro `nldd_field`, regel 58) rendert dat element zodra er een `error` is, en de tekst komt er goed in. Wat ontbreekt is wat het component nodig heeft om die slot te **tonen** - waarschijnlijk een `invalid`- of `error`-attribuut op de `nldd-form-field` zelf en niet alleen op het invoerveld eronder.

Die laatste rij is een tweede bevinding: met `aria-invalid` op nul elementen is de fout ook voor een schermlezer onzichtbaar. Een rood kader is dan het enige signaal, en dat is voor niemand een boodschap.

**Kan het component het niet, dan is dat de uitkomst.** Schrijf op wat er precies niet kan en kaart het aan bij het thema. Geen eigen foutmelding ernaast bouwen: dat is hoe de codebase eerder twee tegenstrijdige aanpakken voor een aanvinkvakje kreeg.

## 2. "Optioneel" staat waar het niet hoort

**Gemeten waar het vandaan komt, en het is niet van ons.** `lotc_forms/_forms.j2` regel 55 zet `optional` op **elk** veld dat niet `required` is:

```jinja
<nldd-form-field label="{{ label }}"{% if not required %} optional{% endif %} ...>
```

Met als onderbouwing: de rijksconventie markeert optioneel, niet verplicht. Voor een invoerveld klopt dat.

Voor iets dat geen invoer is niet. Twee gemelde gevallen:

- de **deploymentkiezer** op het tabblad Deployments: een lijst om tussen deployments te wisselen, er staat er altijd één geselecteerd, en "Optioneel" slaat nergens op;
- **"URI Optioneel"** bij de redirect-URI's van een extra Keycloak-client.

De kiezer is inmiddels gerepareerd door hem `required="true"` te noemen. **Dat is een omweg en geen oplossing**: het veld is niet verplicht, er wordt alleen een label onderdrukt. Werkt op één plek, zegt niets over de rest.

**Wat er moet gebeuren:** loop de formulieren langs op velden waar "Optioneel" onzin is, en bedenk een vorm die zegt wat je bedoelt in plaats van `required` te misbruiken. Als `lotc-forms` daar niets voor heeft, is dat een bevinding voor dat pakket; de conventie klopt, de toepassing op niet-invoervelden niet.

## De toets

**Voor de foutmelding:** een browsertest die een veld leeg indient en vaststelt dat de tekst **zichtbaar** is, niet dat hij in de DOM staat. Dat verschil is precies wat dit maandenlang verborgen hield, en het is vandaag vaker de dader geweest.

Toets ook `aria-invalid`, want anders repareer je het alleen voor wie kijkt.

**Voor "Optioneel":** een test die vaststelt dat het label niet op de deploymentkiezer staat. Kies daarnaast bewust of er meer plekken zijn die dat verdienen.

## Waar op te letten

**Dit gaat niet over vormgeving.** Een foutmelding die je niet ziet is een functioneel gebrek, geen esthetisch. Behandel het zo: de vraag is niet of het er mooier uit kan, maar of de gebruiker weet wat hij moet doen.

**Repareer het bij de bron.** Beide gevallen komen uit `lotc-forms`, dus een oplossing per sjabloon is per definitie de verkeerde plek. Als het pakket aangepast moet worden, is dat de uitkomst.
