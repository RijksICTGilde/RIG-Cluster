# Knoppen op één maat, en een vinkje dat aan staat terwijl het uit hoort

Status: plan, 12 augustus 2026. Twee dingen die los van elkaar staan maar allebei klein zijn en allebei over hetzelfde gaan: wat de gebruiker ziet klopt niet met wat er bedoeld is.

## 1. De knoppen verschillen in maat

### Wat er nu is, gemeten

In `opi/templates_lotc/` en `opi/services/` staan **202** `<c-button>`-aanroepen. Daarvan dragen er **110 helemaal geen `size`**, dus die vallen terug op wat het component standaard doet. De rest is verdeeld over vijf maten:

| maat | aantal |
|---|---|
| `sm` | 223 |
| `md` | 79 |
| `lg` | 62 |
| `xl` | 58 |
| `xs` | 6 |

(Die telling loopt over alle componenten, niet alleen knoppen; het beeld is dat er vijf maten door elkaar lopen en dat meer dan de helft van de knoppen niets zegt.)

Het gevolg is zichtbaar: knoppen die naast elkaar staan zijn niet even hoog, en dezelfde soort actie ziet er op twee pagina's anders uit.

### Wat er moet gebeuren

**Scan alle knoppen en breng ze op één lijn.** Niet één maat voor alles, want een knop in een tabelrij is niet hetzelfde als de hoofdactie van een pagina, maar wel een uitgesproken regel met hooguit een paar maten en een reden per geval.

Leg die regel ergens vast waar de volgende bouwer hem tegenkomt, en toets hem: een test die telt hoeveel knoppen buiten de afgesproken maten vallen is beter dan een afspraak in iemands hoofd. Zo'n bewaker bestaat al voor andere dingen in deze code.

Let er bij het langslopen op dat een knop ook echt een `<c-button>` is. Vandaag bleek een annuleerknop een **kale `<button>`** te zijn, met een verouderde toelichting erboven dat `<c-button>` geen `onclick` toeliet (dat kan wel, via `:attrs`). Die knop kreeg daardoor geen enkele stijlklasse. Zoek dus niet alleen op `<c-button`, maar ook op `<button`.

Zelfde categorie: een `type` die geen bestaande variant is. `type="button"` stond op zes plekken, en `button` is geen variant (dat zijn primary, secondary, tertiary, quaternary, warning, subtle), dus die knoppen kregen geen stijl. Dat faalt stil. Een bewaker die een onbekende variant opmerkt vangt dat voortaan.

## 2. Het vinkje "marked for deletion" bij een databaseschema

### Wat er is, en wat er niet klopt

Het bestaat wél, en het heeft een goede reden. Uit `config_model.py:88`: een schema uit de lijst halen **markeert** het in plaats van het te droppen, zodat een schema en zijn data nooit stilzwijgend verdwijnen bij een gewone opslag. De provisioner laat een gemarkeerd schema staan en stopt met het aanbieden van zijn variabele. De standaard in het model is `False`.

Er zijn dus twee dingen aan de hand, en alleen het eerste is zeker een fout:

**a. In de wizard staat het vinkje aan terwijl het model `False` zegt.** Dat is een echte fout: er wordt een waarde getoond die niet uit de gegevens komt. Meet waar die `True` vandaan komt. Let daarbij op het bekende geval van deze week: een aanvinkvakje van het thema is een form-associated web-component, en htmx bouwt zijn parameters uit `form.elements` met een uitzondering die alleen op `type="checkbox"` slaat. Een vinkje dat altijd meegaat is precies dat patroon.

**b. Hoort het überhaupt in het formulier?** Als markeren de manier is waarop het systeem een verwijdering veilig afhandelt, dan is het een **gevolg** van een handeling en geen keuze die je aanvinkt. Dan hoort er in de wizard geen vinkje maar hooguit een weergave dat een schema gemarkeerd is, met de manier om dat ongedaan te maken.

Beantwoord b voordat je a repareert, want als het vinkje er niet hoort te staan is a vanzelf weg. Zeg het antwoord expliciet, met de reden.

## De toets

- er is een uitgesproken regel voor knopmaten, met een reden per maat, en een bewaker die telt wat erbuiten valt;
- er staat geen kale `<button>` meer waar een `<c-button>` hoort;
- een `type` die geen bestaande variant is, faalt of wordt opgemerkt in plaats van stil geen stijl te geven;
- in de schemawizard staat het vinkje niet meer aan bij een schema dat niet gemarkeerd is, of het vinkje staat er niet meer;
- een bestaand gemarkeerd schema blijft gemarkeerd na opslaan, en een ongemarkeerd schema raakt niet per ongeluk gemarkeerd;
- de veiligheid uit RC-17 blijft: een schema uit de lijst halen dropt hem nog steeds niet.

## Waar op te letten

**Verander de betekenis van markeren niet.** Dit plan gaat over het formulier, niet over wat de provisioner doet. Een gemarkeerd schema blijft staan en zijn data blijft; dat is de hele reden dat het bestaat.

**Knopmaten zijn geen CSS-taak.** De maat hoort uit het component te komen via `size`, niet uit een eigen regel in onze stylesheet.

**Doe de twee onderwerpen los.** Ze zitten in één taak omdat ze klein zijn, niet omdat ze samenhangen. Een commit per onderwerp.
