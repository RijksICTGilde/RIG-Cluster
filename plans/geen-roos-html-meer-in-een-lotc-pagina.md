# Geen roos-HTML meer in een LOTC-pagina

Status: plan, 10 augustus 2026. Gemeten op `operations-manager/python` na de merge van RC-60, RC-61 en RC-62.

## Wat een gebruiker ziet

Op `zad.sandbox.rijksapp.dev/projects/details/hwt-nqi?tab=project` staat de sectie Bijlagen als kale, ongestileerde HTML: een kop, een regel tekst, een bestandsnaam, `id: test`. Geen kaart, geen opmaak. En in een wizard staat een knop met RVO-vormgeving tussen componenten die dat niet hebben:

```html
<button class="utrecht-button utrecht-button--rvo-md utrecht-button--primary-action"
        data-roos-component="button" type="button" onclick="location.reload()">Sluiten</button>
```

Dat `data-roos-component` is het bewijs van de oorzaak: LOTC zet dat attribuut niet. Deze HTML is door **jinja-roos** gerenderd en daarna in een LOTC-pagina geplakt.

## De oorzaak, en waarom het geen slordigheid is

Er zijn twee plekken waar een LOTC-pagina bewust ROOS-HTML injecteert, en allebei zijn ze gedocumenteerd als een afweging.

**1. De detailblokken die diensten zelf leveren.** `bg/project-tabs.html.j2:269` doet:

```jinja
{% for section in service_detail_sections | default([]) %}
    {{ render_roos(section.template, request=request, section=section) }}
{% endfor %}
```

`render_roos()` (`core/templates_lotc.py:90`) rendert een sjabloon uit de ROOS-omgeving en zet het resultaat in de pagina. De reden staat erbij en is goed: die sjablonen staan in `opi/services/catalog/` en zijn in roos-componenten geschreven, en twee componentsystemen kunnen niet in één Jinja-omgeving, want de eerst geregistreerde voorbewerker eist elke `<c-*>`-tag op. Het alternatief, het blok weglaten, zou functionaliteit stil laten verdwijnen.

Drie diensten hebben zo'n blok: **attachments, invite en keycloak**, samen 37 roos-componenttags en 16 rvo-klassen.

**2. Het voortgangsfragment.** `web/task_progress.py:85` rendert onvoorwaardelijk uit de ROOS-omgeving:

```python
return get_templates().get_template("partials/task_progress_fragment.html.j2").render(context)
```

Aangeroepen vanuit `task_progress.py:71`, `router.py:266` en `router.py:3334`. Er bestáát een LOTC-tegenhanger, `templates_lotc/partials/task_progress_fragment.html.j2`, maar geen enkele aanroep kiest hem. Dat is de Sluiten-knop hierboven.

## Waar de afweging stukloopt

Het commentaar bij de eerste plek zegt:

> Zo'n blok draagt rvo-klassen en ziet er dus anders uit dan de rest van deze pagina. Dat is zichtbaar onaf, en dat is precies goed.

Dat klopte toen het geschreven werd en klopt nu niet meer. "Ziet er anders uit" veronderstelt dat die rvo-klassen nog iets doen. RC-62 heeft gemeten dat dat niet zo is: de LOTC-omgeving laadt `["lotc-layout", "nldd", "lotc-forms"]`, en `lotc_rvo` (7568 `.rvo-`-selectors) staat er niet bij. Op een LOTC-pagina is er dus geen stijlblad dat die klassen opmaakt.

Het resultaat is niet "zichtbaar anders" maar **volledig onopgemaakt**. De afweging was: liever zichtbaar onaf dan stilzwijgend weg. De uitkomst in de praktijk is een derde ding dat niemand koos, namelijk kale HTML midden op de projectpagina.

## Wat er moet gebeuren

**Fase 1: het voortgangsfragment door de schakelaar.** `render_progress_fragment()` krijgt het `request` mee en kiest via `lotc_switch.wants_lotc()` tussen de twee sjablonen, net als `render()` dat al doet voor 36 andere plekken. De drie aanroepplekken zijn alle drie routehandlers, dus het request is er.

**Let op de volgorde: de LOTC-tegenhanger is nu zelf kapot.** `templates_lotc/partials/task_progress_fragment.html.j2` regel 53 en 57 doen dit:

```jinja
{% set lotc_onclick_1 %}{{ on_complete }}{% endset %}<c-button label="{{ ... }}"/>
```

De variabele wordt gezet en nergens aan de knop gehangen. Overschakelen zonder dat te repareren ruilt een lelijke werkende knop in voor een mooie dode knop. De juiste vorm staat al elders in de boom, bijvoorbeeld `bg/_modal-wizard-success.html.j2:25`:

```jinja
<c-button type="primary" label="Sluiten" :attrs="{'onclick': sluiten_js}" />
```

Verifieerbaar: klik op Sluiten sluit de modal, en de gerenderde HTML van dat fragment bevat geen `data-roos-component`.

**Fase 2: LOTC-versies van de drie dienstblokken.** Attachments, invite en keycloak krijgen naast hun `section-detail.html.j2` een LOTC-variant, zodat `bg/project-tabs.html.j2` ze uit de eigen omgeving kan renderen in plaats van via `render_roos()`.

Het bezwaar uit de docstring van `render_roos` is echt en moet geadresseerd worden, niet genegeerd: een tweede kopie loopt uit de pas zodra een dienst zijn sjabloon wijzigt, en diensten zijn juist het deel dat blijft groeien. Vang dat met een test die faalt zodra een dienst een `section-detail.html.j2` heeft zonder LOTC-tegenhanger. Dan kan een nieuwe dienst het niet vergeten, en is de kopie zichtbaar in plaats van stil.

Verifieerbaar: de projectdetailpagina bevat na afloop geen `rvo-`-klasse en geen `data-roos-component` meer, gemeten op de gerenderde HTML van een echt project.

**Fase 3: een guard op de uitkomst.** Eén test die elke LOTC-route rendert en faalt zodra het antwoord `data-roos-component` of een `rvo-`-klasse bevat. Dat is de enige controle die dit soort terugval echt tegenhoudt: RC-62 heeft de klassen uit de templates gehaald, maar kon niet zien dat ze via een tweede renderomgeving alsnog binnenkwamen. Deze test ziet dat wel, want hij kijkt naar wat de gebruiker krijgt.

`keycloak/otp-code.html.j2` wordt ook direct uit de ROOS-omgeving gerenderd en heeft geen LOTC-tegenhanger. Zoek uit of die route op een LOTC-pagina uitkomt; zo ja, dan hoort hij bij fase 2, zo nee, dan hoort hij op de uitzonderingslijst van de guard met de reden erbij.

## Wat hier niet in hoort, maar wel gemeld moet

Er staan **56 `lotc_onclick_N`-variabelen in 33 templates** die gezet worden en nooit aan hun knop gehangen worden. Die knoppen renderen zonder klikafhandeling.

Geen van die 33 bestanden is bereikbaar vanaf een echte gebruikersroute: het zijn de eerste generatie automatisch omgezette sjablonen, die hangen aan de `/lotc/pagina/`-demoroute of aan niets. De echte pagina's gebruiken de handgemaakte `bg/`-set, die het wel goed doet. Er zijn dus geen kapotte knoppen voor gebruikers.

Wel is het een valstrik: wie de volgende pagina omzet en zo'n bestand als voorbeeld pakt, kopieert een dode knop. Ze horen opgeruimd of gerepareerd, maar dat is een eigen taak en geen onderdeel van deze. Uitzondering: `partials/task_progress_fragment.html.j2` staat op die lijst én wordt in fase 1 aangezet, dus die ene gaat hier wel mee.

## Waar op te letten

**Dit is geen zoek-en-vervang van klassen.** RC-62 heeft dat gedaan en het was juist; het probleem dat overbleef is een renderpad, niet een klasse. Wie hier weer op `rvo-` gaat grepen in templates vindt niets en concludeert ten onrechte dat het klaar is.

**Twee componentsystemen kunnen niet in één omgeving.** Dat is gemeten en vastgelegd in `docs/lotc-samenleven-met-jinja-roos.md`. De oplossing is dus niet "laad roos er ook bij", maar een LOTC-versie van het sjabloon.

**Meet op de gerenderde pagina, niet op de bron.** Elke bevinding in dit plan komt uit HTML die een gebruiker krijgt, en geen enkele was zichtbaar in een grep over de templates. Fase 3 bestaat om precies die reden.
