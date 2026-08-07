# Templates: samenstellingen, en vormgeving in CSS-bestanden

Een pagina in het portaal is een samenstelling van benoemde deeltemplates, en de
vormgeving staat in een CSS-bestand. Dit document beschrijft hoe dat eruitziet, waar wat
hoort, en welke valkuilen er zitten in het samenspel met de ROOS-componenten.

De reden is de aanstaande verbouwing van vormgeving en indeling: wat je dan wilt
verplaatsen zijn blokken. Een pagina van honderden regels in een enkel `content`-blok met
vormgeving in de markup is niet verplaatsbaar zonder markup te verhuizen.

## Hoe een pagina eruitziet

Het `content`-blok stelt deeltemplates samen en bevat verder niets:

```jinja
{% block content %}
<c-layout-flow gap="xl">

    {% include "dashboard/_kop.html.j2" %}

    {% include "dashboard/_kerncijfers.html.j2" %}

    {% include "dashboard/_projecten.html.j2" %}

</c-layout-flow>
{% endblock %}
```

De deeltemplates staan in een map met de naam van de pagina (`opi/templates/dashboard/`),
beginnen met een onderstrepingsteken en openen met een regel commentaar die zegt wat het
blok is. De grens ligt bij vijftig regels, maar dat is een hulpmiddel: deel op naar
betekenis. Een deeltemplate die halverwege een kaart begint, is erger dan een lang blok.
Als een stuk geen naam heeft die je zonder aarzelen opschrijft, is het geen eenheid.

## Waar de vormgeving staat

| Bestand | Waarvoor |
|---|---|
| `static/css/base.css` | Wat op elke pagina geldt, plus de klassen die pagina's delen (`.page-header-row`, `.table-empty-cell`, `.empty-state-text`, `.flash-success`, `.section-heading`, `.is-hidden`) |
| `static/css/<pagina>.css` | Vormgeving van een pagina; `dashboard.css`, `projects-overview.css`, `project-details.css`, ... |
| `static/css/wizard.css` | De wizard en zijn fragmenten |
| `static/css/modal.css` | De bewerkmodal en de voortgangsweergave |

`base.css` komt binnen via het `additionalCss`-attribuut van `c-page` in `base.html.j2` en
staat daarmee in de `<head>`, na de ROOS-stylesheets en voor de stylesheet van de pagina
zelf. Een pagina linkt zijn eigen stylesheet in `{% block additional_styles %}`.

Een pagina die `base.html.j2` niet uitbreidt maar zijn eigen `c-page` bouwt (`tools.html.j2`)
laadt `base.css` niet en heeft wat het nodig heeft in zijn eigen bestand staan.

## Wat er niet in de markup hoort

`tests/test_template_structure.py` houdt de terugval tegen. De test faalt op:

- een `style=`-attribuut in een template dat niet in `INLINE_STYLE_BUDGET` staat;
- een `<style>`-blok in een template;
- een `content`-blok boven de vijftig regels;
- Jinja in een bestand onder `static/css/`;
- een element met twee `class`-attributen.

Elke uitzondering staat in de test met de reden erbij, zodat hij zichtbaar is en niet
stilzwijgend. Er zijn er nu twee soorten: `architecture-overview.html.j2`, dat in zijn
geheel apart beoordeeld wordt, en zeven `style=`-attributen met een waarde die de template
zelf uitrekent (de breedte van een voortgangsbalk, een kleur uit een servicedefinitie).

## Valkuilen bij ROOS-componenten

Deze drie kosten stille regressies als je ze niet kent.

**Een ROOS-component dat zelf een `style=` zet, negeert een meegegeven `style=`.** Het
component schrijft zijn eigen attribuut eerst en zet dat van jou erachter als tweede
`style=`; een browser houdt bij een dubbel attribuut alleen het eerste aan. Dat geldt onder
meer voor `c-icon` (altijd) en voor `c-card` met `backgroundColor`. Zulke attributen deden
dus nooit iets. Overzetten naar een klasse *verandert* het scherm, en hoort in een eigen
wijziging thuis, niet in een verplaatsing.

**Twee `class`-attributen op een element gaan net zo mis.** Zet een voorwaardelijke klasse
in de bestaande `class`, niet ernaast:

```jinja
{# fout: de browser negeert de tweede #}
<div class="deployment-section" {% if not loop.first %}class="is-hidden"{% endif %}>

{# goed #}
<div class="deployment-section{% if not loop.first %} is-hidden{% endif %}">
```

Als `style=` werkte dit wel, omdat het naast de `class` stond. Bij het omzetten is dit de
val; de test vangt hem.

**Een `<style>`- of `<script>`-openingstag in Jinja-commentaar breekt de hele template.**
De componentverwerking leest de bron als HTML voordat Jinja het commentaar weghaalt, en zo'n
tag begint een blok ruwe tekst dat pas bij de sluittag eindigt. Staat die er niet, dan is de
rest van het bestand tekst en komt `c-page` onverwerkt in de uitvoer - de pagina toont dan
letterlijk `<c-page ...>`. Schrijf in commentaar dus `style-blok`, niet `<style>`.

## JavaScript dat iets toont of verbergt

Gebruik de klasse `.is-hidden`, niet `element.style.display`:

```javascript
element.classList.add('is-hidden');     // verbergen
element.classList.remove('is-hidden');  // tonen
```

Dan kan de verborgen begintoestand als klasse in de markup staan in plaats van als
`style="display: none"`. Let op wanneer het element in getoonde toestand geen `block` is:
geef de klasse van het element zelf dan de goede `display` (zoals
`.metrics-controls__filter { display: flex; }`), want na het weghalen van `.is-hidden` valt
het element terug op wat de CSS zegt.

## Een verplaatsing bewijzen

`scripts/template_snapshot.py` draait de E2E-testapp, loopt elke pagina langs en legt per
pagina een screenshot en een genormaliseerde DOM-afdruk vast.

```bash
git worktree add /tmp/basis <basis-commit>
(cd /tmp/basis/operations-manager/python && uv run python scripts/template_snapshot.py /tmp/snap-basis)
uv run python scripts/template_snapshot.py /tmp/snap-nu
diff -ru /tmp/snap-basis /tmp/snap-nu
```

Vergelijk drie dingen, en gebruik de door de browser geparste DOM als oordeel - niet de
ruwe uitvoer van Jinja, want die laat dubbele attributen zien die de browser weggooit:

1. de opeenvolging van tags en tekst (bij een verplaatsing identiek);
2. de verzameling CSS-regels die de pagina laadt (blokken plus gelinkte bestanden);
3. per element de verzameling vormgevingsdeclaraties, waarbij je elke nieuwe klasse
   terugrekent naar wat er in de CSS staat.

## Wat hier bewust buiten valt

`architecture-overview.html.j2` is niet aangeraakt: 1.500 regels in een blok met 85 inline
styles en een eigen `<style>`. Dat is geen pagina om op te delen maar een om apart te
beoordelen, en misschien te vervangen in plaats van te verbouwen. De uitzondering staat met
die reden in de test.

Componentnamen zijn ongemoeid gelaten. `c-p` blijft `c-p`; de hernoeming naar de
LOTC-woordenschat is een eigen stap.
