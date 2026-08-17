# De rvo-resten uit de LOTC-templates

Status: plan, 10 augustus 2026. Gemeten op `operations-manager/python` op branch `naar-het-nieuwe-componentensysteem`.
Uitgevoerd in RC-62 (PR #61); de uitkomst en de correcties op dit plan staan in `features/lotc-rvo-opruiming.md`.

## Wat er ligt

```
572 voorkomens van rvo- in opi/templates_lotc/, over 213 bestanden
```

Uitgesplitst naar waar ze staan:

```
354  in een class="..." attribuut
 87  als CSS-variabele: var(--rvo-...)
 61  in een style="..." of <style>-blok
  5  als CSS-selector (.rvo-...)
```

En naar of een route ze ooit rendert, via de invoerpunten (`render(lotc=...)`, de directe `TemplateResponse`-aanroepen en de `PREVIEWABLE_PAGES`-allowlist) met de include- en extends-keten erachteraan:

```
144 bestanden bereikbaar vanaf een route : 361 voorkomens
 69 bestanden onbereikbaar               : 211 voorkomens
```

Eén bestand draagt 240 van de 572, dus 42 procent: `architecture-overview.html.j2`. Dat is dezelfde pagina die in het LOTC-plan al een eigen besluit kreeg toegewezen (1509 regels in één blok, 85 inline styles). Buiten dat bestand blijven er in bereikbare templates nog 121 over, netjes verdeeld over tientallen bestanden met er telkens een handvol.

## Het belangrijkste: verreweg de meeste doen niets

Dit is geen vermoeden maar te controleren. De LOTC-omgeving laadt drie design systems:

```python
DESIGN_SYSTEMS = ["lotc-layout", "nldd", "lotc-forms"]   # opi/core/templates_lotc.py:55
```

`lotc_rvo` staat wel geïnstalleerd en definieert **7568** verschillende `.rvo-`-selectors, maar staat niet in die lijst en wordt dus niet geladen. Van de wél geladen pakketten definieert alleen `lotc-forms` er drie: `.rvo-checkbox__group`, `.rvo-form-field__control` en `.rvo-radio-button__group`. Onze templates gebruiken die drie geen enkele keer.

**Geen enkele `class="rvo-..."` in `templates_lotc` wordt door een geladen stylesheet opgemaakt.** Die 354 klassen staan in de HTML, worden meegestuurd naar de browser en doen daar niets. Ze weghalen kan geen pixel verschuiven, en dat maakt het een verifieerbare wijziging in plaats van een smaakoordeel.

## Maar 87 ervan doen wél iets, en dat is de interessante

De variabelen liggen anders. Aan het eind van `static/css/lotc-app.css` staat, op regel 80 tot 110, een met de hand geschreven blok dat de RVO-ontwerptokens opnieuw definieert:

```css
--rvo-color-hemelblauw: #007BC7;
--rvo-color-grijs-500: #64748B;
--rvo-space-md: 16px;
...
```

Dat is geen restant maar een steunbalk: hij houdt de `var(--rvo-...)`-verwijzingen in de templates werkend nu het echte RVO-stijlblad niet meer geladen wordt. Wie die 87 verwijzingen weghaalt zonder vervanging, verandert kleuren en witruimte. Wie het shimblok weghaalt zonder eerst de verwijzingen om te zetten, ook.

Dat verschil bepaalt de opzet van dit plan: **de klassen zijn een opruiming, de variabelen zijn een omzetting.** Ze in één beweging doen is precies hoe een veilige schoonmaak een onbedoelde herstyling wordt.

## Nog twee losse resten

`templates_lotc/base.html.j2` is een meegekopieerde ROOS-schil die niets uitbreidt en die niemand rendert, met `body-class="rvo-theme rvo-responsive"` erin. De echte schil is `base_lotc.html.j2`.

En 69 bestanden zijn vanaf geen enkele route bereikbaar, samen goed voor 211 voorkomens. De zwaarste zijn `project-details/_argocd-deployment-card.html.j2` (27), `wizard/partials/approval_items.html.j2` (13), `formulier-template.html.j2` (13) en `roos-form-improved.html.j2` (12).

## De fasering

**Fase 1: de dode klassen eruit, in bereikbare templates.** Alle `rvo-*` uit `class`-attributen halen in de 144 bereikbare bestanden, met `architecture-overview.html.j2` er expliciet buiten (zie fase 4). Waar een klasse het enige is dat in het attribuut staat, gaat het hele `class`-attribuut weg. Verifieerbaar op de uitkomst en niet op de diff: render elke bereikbare pagina voor en na, en vergelijk de HTML met de `class`-attributen genormaliseerd weg. Verschilt er iets anders, dan is er meer geraakt dan de bedoeling was.

**Fase 2: een guard-test.** Er staan al negen LOTC-guards in `tests/` (`test_lotc_component_names.py`, `test_lotc_schrijfwijze.py`, en zo verder); dit volgt dat patroon. De test faalt zodra er een `rvo-` klasse terugkomt in `templates_lotc`, met een expliciete uitzonderingslijst voor wat fase 3 en 4 nog niet gedaan hebben. Uitzonderingen horen te verdwijnen naarmate die fases landen, dus de test hoort ook te falen zodra een uitzondering overbodig is geworden. Dat is dezelfde vorm als de guard uit fase 1 van het LOTC-plan.

**Fase 3: de variabelen omzetten.** De 87 `var(--rvo-...)`-verwijzingen vervangen door de tokens van het geladen design system, en pas daarna het shimblok uit `static/css/lotc-app.css:80-110` weghalen. In die volgorde, want andersom is er een moment waarop de pagina's zonder waarden zitten. Dit is de enige fase met een zichtbaar resultaat, dus hier hoort een visuele vergelijking bij en geen HTML-diff. Zoek eerst uit welke NLDD-tokens er zijn: als er geen tegenhanger bestaat voor een kleur die wij gebruiken, is dat een ontwerpvraag en geen zoek-en-vervang, en die hoort dan benoemd te worden in plaats van met een dichtstbijzijnde kleur ingevuld.

**Fase 4: `architecture-overview.html.j2` apart beoordelen.** 240 van de 572, in de pagina die het LOTC-plan al aanwees als "geen pagina om mee te nemen in een omzetting, maar er een om apart te beoordelen, en misschien om te vervangen in plaats van om te zetten". Dat besluit staat nog open, en er 240 klassen uit poetsen vooruitlopend op een mogelijke vervanging is verspilde moeite. Uitkomst van deze fase is een besluit, niet per se een wijziging.

**Fase 5: `base.html.j2` uit `templates_lotc` weg.** Losse, kleine opruiming. Verifieerbaar: alle 62 invoerpunten renderen nog en geen enkele template verwijst ernaar.

## Wat bewust buiten scope blijft

**De 69 onbereikbare bestanden opschonen.** Daar de klassen uithalen is poetsen aan iets wat niemand rendert. Ze horen op een lijst, en of ze weg moeten is een eigen besluit over dode templates, geen onderdeel van een rvo-opruiming. Deze taak levert die lijst op en raakt de bestanden verder niet.

**`opi/templates/`, de ROOS-boom.** Daar staan er 603, en die horen daar: dat is het stijlsysteem dat die templates gebruiken. De schakelaar (`opi/web/lotc_switch.py`) houdt beide wegen open met `?layout=roos`, en zolang die terugvalweg bestaat blijft de ROOS-boom staan zoals hij is.

## Waar op te letten

**De nieuwe vormgeving is de standaard.** `DEFAULT_LAYOUT = LAYOUT_LOTC` in `lotc_switch.py:39`, dus dit is wat gebruikers zien tenzij ze om de oude vragen. Een fout hier is meteen zichtbaar, en dat is een reden voor de HTML-vergelijking in fase 1 en niet voor voorzichtigheid achteraf.

**Zoek niet op `rvo-` alleen.** Dat woord komt ook voor in commentaar dat de herkomst uitlegt, en dat commentaar is nuttig en mag blijven staan. De wijziging gaat over attribuutwaarden en CSS-verwijzingen, niet over elk voorkomen van vier letters.

**Een lege `class=""` is ook een rest.** Wie alleen de tokens uit het attribuut haalt, laat overal lege attributen achter. Dat is geen opruiming maar een tweede ronde werk.
