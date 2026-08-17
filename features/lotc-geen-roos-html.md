# Geen roos-HTML in een LOTC-pagina

> **RC-67: dit stuk beschrijft de tijd dat er TWEE componentsystemen waren.** Roos is weg,
> en daarmee de `-lotc`-tegenhangers en `lotc_counterpart()`: elke dienst heeft nog EEN
> sjabloon, onder zijn eigen naam. Wat hier staat is de weg ernaartoe en de reden dat het
> zo gelopen is; de eindstand staat in `features/roos-eruit.md`.


Wat er gebeurde als een LOTC-pagina HTML uit de andere bouwlijn binnenkreeg, waar dat
vandaan kwam, en hoe het nu dichtgezet is. Gemeten en uitgevoerd in RC-64, boven op de
klassenopruiming van RC-62 (`features/lotc-rvo-opruiming.md`).

## Wat de gebruiker zag

Op de projectdetailpagina stond de sectie **Bijlagen** als kale, ongestileerde HTML: een
kop, een regel tekst, een bestandsnaam, `id: test`. Geen kaart, geen opmaak. En in een
dialoog stond een knop met RVO-vormgeving tussen componenten die dat niet hadden:

```html
<button class="utrecht-button utrecht-button--rvo-md utrecht-button--primary-action"
        data-roos-component="button" type="button" onclick="location.reload()">Sluiten</button>
```

Dat `data-roos-component` is het bewijs: LOTC zet dat attribuut niet, LOTC zet
`data-lotc-component`. Die HTML was door jinja-roos gerenderd en daarna in een LOTC-pagina
geplakt.

## Waarom dat niet zichtbaar was in de templates

RC-62 heeft de rvo-klassen uit `opi/templates_lotc/` gehaald en dat was juist. Wat een
grep over templates niet kan zien: de klassen kwamen daarna alsnog binnen, via een TWEEDE
renderomgeving.

`render_roos()` (`opi/core/templates_lotc.py`) rendert een sjabloon met de roos-omgeving en
zet het resultaat als HTML in de LOTC-pagina. In de bron van die pagina staat dan geen
enkele `rvo-`; in wat de gebruiker krijgt stonden er tachtig.

Dat was een bewuste afweging, met een verantwoording die op het moment van schrijven
klopte: liever een blok dat er zichtbaar anders uitziet dan een blok dat stilzwijgend van
de pagina verdwijnt. De helft die niet meer klopte is "zichtbaar anders". Dat veronderstelt
dat de rvo-klassen nog iets DOEN, en dat doen ze niet: de LOTC-omgeving laadt
`["lotc-layout", "nldd", "lotc-forms"]` en `lotc_rvo` staat daar niet bij. De uitkomst was
dus een derde ding dat niemand koos: volledig onopgemaakte HTML midden op de pagina.

## Wat er nu gebeurt

### Een dienst levert twee sjablonen

Een dienst die een leesblok op de projectpagina levert (`UIEvent.PROJECT_SECTIONS`) legt
naast zijn `section-detail.html.j2` een `section-detail-lotc.html.j2`, in dezelfde
dienstmap - een dienst draagt alles wat hij is in zijn eigen map (RC-36).

```
opi/services/catalog/keycloak/
  section-detail.html.j2        # roos-componenten
  section-detail-lotc.html.j2   # LOTC-componenten
```

`lotc_counterpart(naam)` zoekt de tegenhanger op naam - een afspraak (`-lotc.html.j2`) en
geen tabel, zodat een nieuwe dienst niets hoeft bij te werken buiten zijn eigen map. De
catalogusmap staat sinds RC-64 ook op het zoekpad van de LOTC-omgeving, achteraan, zodat
een dienstsjabloon nooit een bestand in `templates_lotc/` kan overschaduwen.

`bg/project-tabs.html.j2` kiest:

```jinja
{% set lotc_sjabloon = lotc_counterpart(section.template) %}
{% if lotc_sjabloon %}{% include lotc_sjabloon %}{% endif %}
```

Hier stond een `{% else %}` die met `render_roos()` op de roos-omgeving terugviel. Die
terugval is weg (RC-65), en niet omdat de regel erachter vervalt maar omdat er niets meer
is om op terug te vallen: de roos-omgeving zelf verdwijnt. De regel blijft in twee vormen
overeind. Ten eerste slaat de pagina een blok zonder tegenhanger OVER in plaats van om te
vallen - een dienst die morgen iets aankondigt neemt de projectpagina niet mee in zijn val.
Ten tweede maakt `tests/test_lotc_dienstblokken.py` dat overslaan onbereikbaar: het toetst
ELK sjabloon in de catalogus op zijn tegenhanger, niet alleen de projectblokken.

Die poort stond eerst alleen op `*/section-detail.html.j2`, en dat was precies te smal: het
deploymentblok van metrics_scraper, het backupblok en de twee dialogen (job,
databaseconsole) vielen erbuiten. De twee dialogen kozen hun sjabloon zelfs helemaal niet -
`jobs.py` en `db_console.py` deden een kale `TemplateResponse` op het roos-sjabloon - dus
op een NLDD-pagina kwam de dialoog onopgemaakt binnen. Ze gaan nu allebei via
`opi.web.lotc_switch.render()`.

### Waarom er niet één gedeeld sjabloon kan zijn

Twee componentsystemen kunnen niet in een Jinja-omgeving: beide registreren een extensie
die de bron voorbewerkt en elke `<c-*>`-tag opeist, en er is geen doorlaatstand. Gemeten in
`docs/lotc-samenleven-met-jinja-roos.md`. De oplossing is dus niet "laad roos er ook bij"
maar een tweede sjabloon.

Het bezwaar daartegen is echt - een tweede kopie loopt uit de pas zodra een dienst zijn
sjabloon wijzigt, en diensten zijn juist het deel van dit platform dat blijft groeien. Dat
is met tests opgevangen, niet met een belofte; zie "De poorten".

### De fragmenten volgen hun pagina

Twee fragmenten kwamen na een klik in een LOTC-pagina terecht en renderden onvoorwaardelijk
uit de roos-omgeving. Ze gaan nu allebei door `lotc_switch`:

| fragment | roos | lotc |
|---|---|---|
| voortgang van een taak | `partials/task_progress_fragment.html.j2` | idem, in `templates_lotc/` |
| OTP-code van een realm | `keycloak/otp-code.html.j2` | `keycloak/otp-code-lotc.html.j2` |

De LOTC-tegenhanger van het voortgangsfragment was zelf kapot en is meegerepareerd:

- de afsluitknoppen zetten `on_complete` in een variabele en hingen hem nergens aan, dus de
  knop kwam zonder klikafhandeling op het scherm (de dialoog ging niet dicht);
- de statusiconen droegen ROOS-namen (`vinkje`, `kruis`), die NLDD niet kent en stil leeg
  rendert;
- de foutmelding sloot een automatisch omgezette partial in, zonder de suggestie en met een
  lege, dode logboeklink.

De juiste vorm voor een knop met een aanroep is `:attrs`; `<c-button>` laat geen losse
`onclick` toe:

```jinja
{% set sluiten_js %}{{ on_complete }}{% endset %}
<c-button type="primary" label="Sluiten" :attrs="{'onclick': sluiten_js}" />
```

## De poorten

| test | wat hij tegenhoudt |
|---|---|
| `tests/test_lotc_dienstblokken.py` | een dienst met een `section-detail.html.j2` zonder LOTC-tegenhanger; en de twee die uit elkaar lopen (bestemmingen, htmx-adressen, aangeroepen JS-functies, id's - gemeten met dezelfde meetlat als `scripts/lotc_compare_behaviour.py`) |
| `tests/test_lotc_geen_roos_html_in_het_antwoord.py` | elk LOTC-antwoord dat `data-roos-component` of een `rvo-`-klasse bevat, gemeten op de gerenderde pagina |
| `tests/test_lotc_voortgangsfragment.py` | het voortgangsfragment dat de verkeerde omgeving kiest, of een afsluitknop zonder klikafhandeling |

De tweede is de belangrijkste, en de reden staat in de kop van deze pagina: dit soort
terugval is in de BRON niet te zien. Een klassenopruiming kan hem niet vinden; een test die
naar het antwoord kijkt wel.

## De proefopstelling toont de blokken nu ook

`/lotc/bg/project-tabs` gaf een lege lijst dienstsecties mee, waardoor juist de secties die
het langst scheef stonden niet op de proefopstelling verschenen. Het voorbeeldproject
(`opi/web/lotc_fixtures/voorbeeld-volledig.yaml`) heeft nu een realm, een uitnodiging en een
bijlage, en `build_details_context` bouwt de secties met de echte registry
(`collect_detail_page_sections`). Zonder dat zou de poort hierboven "geen roos-HTML" melden
om de verkeerde reden: op een pagina waar de blokken niet stonden.

## Wat hiermee NIET is opgelost

Er staan 54 `lotc_onclick_N`-variabelen in 32 templates die gezet worden en nooit aan hun
knop gehangen worden - die knoppen renderen zonder klikafhandeling. Geen van die bestanden
is vanaf een gebruikersroute bereikbaar: het is de eerste generatie automatisch omgezette
sjablonen, die aan de `/lotc/pagina/`-demoroute hangt of aan niets. De echte pagina's
gebruiken de handgemaakte `bg/`-set, die het wel goed doet.

Het is wel een valstrik: wie de volgende pagina omzet en zo'n bestand als voorbeeld pakt,
kopieert een dode knop. Ze horen opgeruimd of gerepareerd, maar dat is een eigen taak.
`opi/templates_lotc/partials/_component_failures.html.j2` hoort in dat rijtje thuis en is
sinds RC-64 door niets meer ingesloten.
