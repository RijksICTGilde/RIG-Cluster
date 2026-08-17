# Templates opdelen en opschonen

Status: plan, 7 augustus 2026. Niet gebouwd. Dit is fase 1 uit `plans/naar-het-nieuwe-componentensysteem.md`, apart geshipt omdat het aan geen enkele openstaande vraag hangt.

**Wat dit niet is.** Geen omzetting naar LOTC, geen hernoeming van componenten, geen nieuwe vormgeving. Die stappen wachten op antwoorden van het LOTC-project. Dit plan maakt alleen de templates verplaatsbaar, en dat is winst ook als de omzetting niet doorgaat.

## Waarom nu

De vormgeving en de indeling gaan veranderen; uitgangspunt wordt de bg.rijks.app-app. Wat je dan wilt verplaatsen zijn blokken. Wat we hebben zijn pagina's van honderden regels in een enkel `content`-blok, met vormgeving die in de markup vastzit.

```
110 templates, 12.179 regels
 20 templates breiden base.html.j2 uit
  5 soorten blokken in totaal (content, page_title, additional_styles,
    additional_scripts, title)
 21 content-blokken, het grootste 1.509 regels
 35 includes over 26 deeltemplates
260 inline style= attributen
 17 <style>-blokken in templates
 71 herhaalde blokken van 6+ regels over meerdere bestanden
```

Vijf bloksoorten voor twintig pagina's betekent dat vrijwel alles in een enkel `content`-blok zit. Een pagina is daarmee een lap tekst en geen samenstelling: je kunt niets verplaatsen zonder markup te verhuizen.

## Voorstel

1. **Elke pagina wordt een samenstelling.** Een `content`-blok van meer dan pakweg vijftig regels wordt opgedeeld in benoemde deeltemplates die de pagina samenstelt. De grens is een richtlijn, geen wet: het gaat erom dat de eenheden betekenis hebben (een kaart, een lijst, een kop met acties), niet dat ze klein zijn.

2. **Inline styling eruit.** 260 `style=`-attributen en 17 `<style>`-blokken. Wat echt eigen vormgeving is gaat naar de bestaande CSS-bestanden; wat een component hoort te doen, laat je aan het component. De zwaarste zijn `_argocd-deployment-card.html.j2` (28) en de twee restore-partials (12 en 11).

3. **Duplicatie naar macro's.** De zwaarste paren zijn `task_progress_fragment` en `modal_wizard_progress_fragment` (17 gedeelde blokken van zes regels of meer) en `project-creation-partial` en `project-creation-success` (10). Er staan al 15 macro's in `widgets/_macros.html.j2`; die plek is de juiste.

4. **Een test die de terugval tegenhoudt.** Faalt op een nieuw `style=`-attribuut, een nieuw `<style>`-blok, en op een `content`-blok boven de gekozen grens. Zonder dat loopt dit binnen een half jaar terug.

## Wat er buiten valt

**`architecture-overview.html.j2` niet aanraken.** 1.509 regels in een blok, 85 inline styles, een eigen `<style>`. Dat is geen pagina om op te delen maar een om apart te beoordelen, en misschien te vervangen in plaats van te verbouwen. Neem hem op in de test met een uitzondering en de reden erbij, zodat de uitzondering zichtbaar is en niet stilzwijgend.

**Componentnamen ongemoeid.** `c-p` blijft `c-p`. De hernoeming naar de LOTC-woordenschat is een eigen stap en hangt af van antwoorden die er nog niet zijn.

**Geen markup verfraaien.** Alles wat straks een LOTC-component wordt, hoeft alleen op de goede plek te staan. Grenzen trekken loont, inhoud herschrijven niet.

## Volgorde

1. De test eerst, met de huidige aantallen als vertrekpunt. Die lijst is meteen de werklijst.
2. Duplicatie naar macro's; dat is de veiligste stap en verkleint het werk daarna.
3. Inline styling, per bestand, zwaarste eerst.
4. De pagina's opdelen, de grootste eerst (dashboard 283 regels, projects-overview 160, approvals 117, users 102).
5. De test aanzetten op de nieuwe waarden.

## Waar op te letten

**Dit mag niets veranderen aan wat er op het scherm staat.** Elke stap is een verplaatsing. De e2e-tests moeten groen blijven zonder aanpassing; moet je een test aanpassen, dan heb je gedrag veranderd en niet opgedeeld.

**Een screenshot-vergelijking is hier meer waard dan een unittest.** We hebben geen visuele tests. Voor dit werk is een screenshot per pagina voor en na de goedkoopste manier om te bewijzen dat er niets verschoven is. Dat is meteen de opzet die de latere omzetting nodig heeft, dus die investering is niet voor een keer.

**Deel op naar betekenis, niet naar regelaantal.** Een deeltemplate die halverwege een kaart begint, is erger dan een lang blok. Als een stuk geen naam heeft die je zonder aarzelen opschrijft, is het geen eenheid.

**De wizard-templates zitten in een verbouwing.** RC-43 en RC-44 raken de wizardlaag. Blijf uit `wizard/` tot die binnen zijn, of stem af; de rest van de templates is vrij.
