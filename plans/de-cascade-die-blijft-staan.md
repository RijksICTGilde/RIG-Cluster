# De cascade die blijft staan: een geldige keuze, en daarna een lege lijst die niet meer bijtrekt

**Dit is vermoedelijk geen testprobleem maar een productbug**, en dat is de reden dat deze taak bestaat. Drie keer op rij is dit verschijnsel aan de test toegeschreven: eerst als isolatie tussen twee tests (de `xfail`-reden), toen als machinebelasting (RC-125), en daarna heb ik dat laatste nagepraat. Geen van die drie heeft gekeken naar de keuzelijst zelf op het moment dat het misgaat.

## Wat er gemeten is

In de aanmaakwizard, stap "Cross-domain toegang", `tests/e2e/test_wizard_cross_domain_policy.py`. De cascade is: kies een peer-project, dan verschijnt de deploymentlijst, dan de componentlijst. Elke keuze is een serverrender van de rij.

Bij een falende run, op het moment dat de wachtregel afloopt:

```
de keuzelijst biedt nu ['']
de rij staat op {
  .../from/project':    'test-project',
  .../from/deployment': '',
  .../from/component':  '',
  .../to/component':    '',
  .../to/port':         ''
}
```

Wat daaruit volgt, en wat de eerdere verklaringen uitsluit:

- **De keuze is niet verloren gegaan.** `from/project` staat gewoon op `test-project`. De verklaring "je kiest in een lijst die net vervangen wordt, en dan raakt de waarde weg" past hier niet.
- **Het is geen traagheid.** De lijst is na 30 seconden nog steeds leeg, niet laat. Gemeten load op dat moment: 3,32 op 14 kernen, dus ~24% bezet. Bij traagheid zou de lijst na een tijdje alsnog vullen; dat gebeurt niet.
- **`test-project` bestaat altijd**; het is een fixture met deployment `default`, en het stond ook als optie in de projectlijst, dus de server kent het.

De cascade staat dus stil terwijl er een geldige keuze in staat, en trekt niet meer bij.

Frequentie op één machine, vijf runs achter elkaar van hetzelfde bestand met hetzelfde commando: 3 groen / 2 rood, met 28 seconden voor een groene run en 52 tot 78 voor een rode.

## Waarom dit ernstig is

Als dit reproduceert buiten de test, kan een gebruiker de stap niet invullen: hij kiest een project, de deploymentlijst blijft leeg, en er gebeurt niets meer. Het veld is verplicht, dus de stap is dan dood.

Dit bestand is er juist gekomen omdat precies die fout eerder is gebeurd — drie verplichte velden hingen aan keuzelijsten die leeg waren, en alle onderliggende tests bleven groen omdat geen van hen bij de pagina begon. Zie de kop van het testbestand.

## Waar te beginnen

**Eerst: reproduceert het met de hand?** Open de wizard in een browser, ga naar de stap, kies het peer-project en kijk of de deploymentlijst vult. Herhaal het een aantal keer, en probeer het ook door snel achter elkaar te kiezen. Als het daar niet reproduceert, is het een testprobleem en zeg dat dan met de meting erbij — dan is deze taak klaar en is de conclusie het waard.

**De eerste verdachte staat al opgeschreven, in `static/js/htmx-formgedrag.js`:**

> htmx gooit een tweede verzoek weg als hetzelfde element nog bezig is, dus die klik verdwijnt geruisloos - de gebruiker ziet niets gebeuren en klikt nog eens.

In een cascade waar elke keuze een serverronde is, overlappen die rondes. Wordt de render na de projectkeuze weggegooid omdat de vorige nog liep, dan komt er geen nieuwe en blijft de lijst leeg — precies het gemeten beeld, en het verklaart ook waarom het soms wél goed gaat.

Meet dat voordat je het aanneemt: tel in de browser de htmx-verzoeken en hun antwoorden rond de projectkeuze, en kijk of er een verzoek is geweigerd of afgebroken (`htmx:beforeRequest`, `htmx:afterRequest`, `htmx:abort`, en de netwerklaag van Playwright).

**Andere richtingen, niet uitgesloten:** de `change`-gebeurtenis die de render zou moeten aanjagen vuurt niet bij een programmatische keuze; de server rendert de rij met een oudere staat; of de nieuwe lijst komt wel binnen maar landt niet omdat het doel intussen vervangen is.

## Wat er moet uitkomen

1. Een oorzaak met een meting eronder, niet een categorie ("belasting", "isolatie", "flaky").
2. Als het de code is: de reparatie, met een test die omvalt als je hem terugdraait.
3. Als het de test is: waarom, en waarom de drie eerdere verklaringen niet klopten.
4. Een antwoord op de vraag of het buiten deze ene stap ook speelt. Elke andere afhankelijke keuzelijst in de wizard loopt langs hetzelfde mechanisme; als dit een echte bug is, is dit niet de enige plek.

## Verifieerbaar

- Tien opeenvolgende runs van `tests/e2e/test_wizard_cross_domain_policy.py` groen, met de uitkomsten in de PR. Draai ze ook een keer terwijl er iets anders op de machine loopt.
- De handmatige controle in de browser, met wat je zag.
- `uv run pytest tests/ -q` groen, plus `ruff check`, `ruff format`, `pyright`.

## Wat er buiten valt

- De wachtregels in de test verder oprekken. Een hoger vangnet is hier aantoonbaar niet de oplossing: de lijst is leeg, niet laat.
- Nieuwe functionaliteit in de cross-domain-dienst.
