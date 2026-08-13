# Hoeveel troep is er nog: een inventarisatie, en pas daarna opruimen

Status: plan, 13 augustus 2026. Aanleiding: er is de afgelopen weken veel verbouwd, en de vraag is wat er aan restanten is blijven liggen. Dit is **eerst een inventarisatie**. Weggooien zonder te weten wat iets doet is hoe je een stille storing bouwt.

## Wat al gemeten is, als startpunt

Geen van deze getallen is een conclusie; ze zeggen alleen waar het loont te kijken.

* **`opi/templates_lotc/project-details/` telt 26 bestanden.** Die map is vervangen door `bg/`. Sommige daarvan zijn aantoonbaar dood (van `_argocd-deployment-card.html.j2` is vastgesteld dat alleen de `bg/`-versie gerenderd wordt), maar dat geldt niet automatisch voor alle 26.
* **Zes bestanden noemen `copyToClipboard`.** Dat kopieerknopje is vervangen door `<c-secret-field show-copy>`; er staan nog `{% set kopieer = ... %}`-regels die niets meer aanroepen.
* **`lotc-rvo` is geïnstalleerd maar staat niet in `DESIGN_SYSTEMS`.** Het wordt dus geladen als pakket en nooit gebruikt. Er staan al drie toelichtingen in de code die dat vermelden.
* **Elf CSS-bestanden onder `static/css/`.** Bij een eerdere ronde bleken er honderden verwijzingen naar `--rvo-*`-variabelen te staan die nergens meer bestaan. Of dat nu nog zo is, is niet gemeten.

## Hoe dit aangepakt hoort te worden

**Eerst inventariseren, dan pas voorstellen, en weggooien in een aparte commit per categorie.**

Per gevonden restant vastleggen: wat het is, waaruit blijkt dat het dood is, en wat er kapot gaat als het toch niet dood was. Dat derde is het belangrijkste. "Niemand importeert dit" is zwakker bewijs dan het lijkt: een sjabloon kan via een naam uit gegevens geladen worden, een CSS-klasse kan als haak in een test staan, en een functie kan via een hook worden aangeroepen.

Twee dingen die deze week hebben bewezen dat de eenvoudige toets tekortschiet:

* Een **klasse** die eruitzag als opmaak (`deployment-section`, `.is-hidden`) bleek de haak waar JavaScript aan hing. Een klasse zonder CSS is niet vanzelf dood.
* Een **test** die groen stond zei niets, omdat hij de verkeerde plek maat. Groen na het weggooien is dus geen bewijs; de vraag is of er een test IS die het zou merken.

Sorteer de vondsten in drie bakken, en behandel ze verschillend:

1. **Aantoonbaar dood**, met bewijs. Weg, in een eigen commit met het bewijs in de boodschap.
2. **Waarschijnlijk dood, niet te bewijzen.** Niet weggooien. Opschrijven in `TODO.md` met wat er nodig is om het wel te kunnen vaststellen.
3. **Leeft nog, maar is dubbel.** Dat is geen opruimwerk maar een keuze over welke van de twee blijft, en die hoort apart.

## Wat er NIET in deze taak zit

**Geen refactor.** Dit gaat over weghalen wat niets meer doet, niet over anders inrichten wat nog wel werkt.

**Geen `lotc-rvo` uit de dependencies halen** zonder te meten of iets er nog uit importeert. Het pakket levert ook sjablonen die via de kale bouwlijn geladen kunnen worden; dat is een eigen vraag.

**De map `project-details/` niet in één keer weg.** Ga per bestand na of het gerenderd wordt, ook via een naam die uit gegevens komt.

## De toets

- er is een lijst van gevonden restanten, per stuk met wat het is en waaruit blijkt dat het dood is;
- alles in bak 1 is weg, in een eigen commit per categorie, en de suite is groen;
- alles in bak 2 staat in `TODO.md` met wat er nodig is om het vast te stellen;
- alles in bak 3 staat benoemd als een keuze, niet als opruimwerk;
- er is niets weggehaald waarvan alleen "niemand importeert het" bekend was.

## Waar op te letten

**Een opruiming die iets stils breekt is duurder dan de troep.** Bij twijfel: bak 2.

**Kijk ook naar het scherm.** `scripts/kijk_sandbox.py <pad>` na afloop op een handvol pagina's is goedkoop, en een verdwenen stylesheet is op een screenshot meteen te zien en in een test niet.
